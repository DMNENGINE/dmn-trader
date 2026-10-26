"""PILOTO DE FUTUROS: opera NinjaTrader a través del AddOn DmnPuente (ninjatrader/DmnPuente.cs, http://localhost:36990).

- CUENTA SIM (Sim101): opera TODAS las candidatas de la fábrica de futuros (manuales + salón): es su prueba en vivo con
  ejecuciones reales de NinjaTrader.
- CUENTA REAL: solo las estrategias PROMOVIDAS (ganan en vivo) y con freno diario: si las pérdidas del día llegan a
  `real_perdida_dia` $ se para hasta el día siguiente, y ninguna entrada arriesga más de lo que queda de ese margen.
  Además la cuenta tiene que estar en `cuentas_orden` de Documents/NinjaTrader 8/dmn/puente.json (el AddOn rechaza las demás).
Toda entrada lleva stop y objetivo (OCO) que pone el AddOn al llenarse; el piloto cierra por tiempo al pasar el `hold`.
PROTECCIÓN (solo Sim, `proteger`): cuando la operación ha recorrido parte del camino al objetivo el piloto pone un stop
virtual (breakeven y luego trailing) y cierra a mercado si el precio vuelve a él. Cada cierre protegido deja una «sombra»
que sigue el precio con el stop y el objetivo originales: así se compara el resultado CON protección y SIN ella."""
import hashlib
import urllib.parse
import json, os, threading, time, urllib.request, math
from . import db, ejecutor

PUENTE = os.environ.get('DMN_NT_PUENTE', 'http://localhost:36990')
TOKEN = os.path.join(os.path.expanduser('~'), 'Documents', 'NinjaTrader 8', 'dmn', 'token.txt')
EST = 'piloto_fut'
DEFECTO = {'activo': True, 'cuenta_sim': 'Sim101', 'cuenta_real': None, 'real_activo': False, 'real_perdida_dia': 20.0, 'qty': 1, 'cada': 60,
           'cambiado': 0, 'cerrar_todo': 0, 'proteger': True}
# breakeven al recorrer el 50 % del objetivo (+2 ticks de colchón); desde el 75 % el stop guarda la mitad de lo máximo ganado
PROTECCION = {'activar': 0.5, 'colchon_ticks': 2, 'trailing_desde': 0.75, 'trailing_guarda': 0.5}
# se controla desde el panel (en la Pi o en el PC): la configuración más reciente gana (real.py la sincroniza con el nodo)
_hilo = {'t': None}


class SinPuente(Exception):
    pass


def riesgo_total(por_contrato, qty):
    if isinstance(por_contrato, bool) or not isinstance(por_contrato, (int, float)) or not math.isfinite(por_contrato) or por_contrato <= 0 or isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
        raise ValueError('riesgo o cantidad invalidos')
    return por_contrato * qty


def puente(ruta, cuerpo=None, timeout=70):
    try:
        tok = open(TOKEN, encoding='utf-8').read().strip()
    except OSError:
        raise SinPuente('el AddOn DmnPuente no está instalado o no ha arrancado (falta su token)')
    rq = urllib.request.Request(PUENTE + ruta, data=json.dumps(cuerpo).encode() if cuerpo is not None else None,
                                method='POST' if cuerpo is not None else 'GET', headers={'x-dmn-token': tok, 'content-type': 'application/json'})
    try:
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SinPuente(json.loads(e.read() or b'{}').get('error') or str(e))
    except OSError as e:
        raise SinPuente(f'NinjaTrader no responde ({e})')


def cfg():
    c = {**DEFECTO, **(db.ajuste(EST + '_cfg') or {})}
    if any(not isinstance(c[k], bool) for k in ('activo', 'real_activo', 'proteger')) or isinstance(c['qty'], bool) or not isinstance(c['qty'], int) or not 1 <= c['qty'] <= 2 or isinstance(c['real_perdida_dia'], bool) or not isinstance(c['real_perdida_dia'], (int, float)) or not math.isfinite(c['real_perdida_dia']) or c['real_perdida_dia'] < 0 or (c['real_activo'] and not c['real_perdida_dia']):
        return {**DEFECTO, 'activo': False, 'real_activo': False, 'error_cfg': 'configuracion persistida invalida; piloto bloqueado'}
    return c


def poner_cfg(c):
    if not isinstance(c, dict):
        raise ValueError('configuracion debe ser un objeto')
    for k in ('activo', 'real_activo', 'proteger'):
        if k in c and not isinstance(c[k], bool):
            raise ValueError(k + ' debe ser booleano')
    for k in ('real_perdida_dia', 'qty', 'cerrar_todo'):
        if k in c and (isinstance(c[k], bool) or not isinstance(c[k], (int, float)) or not math.isfinite(c[k]) or c[k] < 0):
            raise ValueError(k + ' debe ser numerico, finito y no negativo')
    if 'qty' in c and (not isinstance(c['qty'], int) or not 1 <= c['qty'] <= 2):
        raise ValueError('qty debe ser 1 o 2')
    nuevo = {**cfg(), **{k: v for k, v in (c or {}).items() if k in DEFECTO and k not in ('cambiado', 'cada')}}
    nuevo['real_perdida_dia'] = max(0.0, min(float(nuevo['real_perdida_dia']), 500.0))
    nuevo['qty'] = max(1, min(int(nuevo['qty']), 2))
    if nuevo['real_activo'] and nuevo['real_perdida_dia'] <= 0:
        raise ValueError('activar real requiere un limite diario positivo')
    nuevo['cambiado'] = int(time.time())
    db.poner_ajuste(EST + '_cfg', nuevo)
    return nuevo


def cfg_mas_nueva(otra):
    from .sincronia import fecha
    if not isinstance(otra, dict):
        return
    ts = fecha(otra)
    with db.transaccion():
        if ts > fecha(cfg()):
            validada = poner_cfg(otra)
            validada['cambiado'] = ts
            db.poner_ajuste(EST + '_cfg', validada)


def _est():
    return db.ajuste(EST) or {'ops': [], 'log': [], 'abiertas': {}}


def _log(e, txt):
    e['log'] = (e.get('log') or [])[-149:] + [[int(time.time()), txt]]


def estado():
    from . import fabrica_fut as ff
    try:
        res = ff.resumen()
    except Exception:
        res = None
    e = _est()
    return {**e, 'cfg': cfg(), 'fabrica': res, 'comparativa': comparativa(e)}


def _barras(s):
    """Velas de 5 min en tiempo real del puente. NinjaTrader marca la vela con su hora de CIERRE: se pasa a hora de apertura
    y se quita la que aún se está formando."""
    import numpy as np
    from . import fabrica_fut as ff
    d = puente('/barras?inst=' + urllib.request.quote(ff.FUT[s]['nt']) + '&min=5&dias=4')
    ahora = time.time()
    v = [x for x in d['velas'] if x[0] <= ahora]
    if not v:
        return None
    a = np.array(v, dtype=float)
    return {'t': a[:, 0] - 300, 'o': a[:, 1], 'h': a[:, 2], 'l': a[:, 3], 'c': a[:, 4], 'v': a[:, 5]}


def reenviar_velas():
    """Velas de 1 y 5 min de NinjaTrader → la central (Pi), para que el enjambre opere futuros en tiempo real."""
    import urllib.parse
    from . import agente, fabrica_fut as ff
    if not agente.CENTRAL:
        return 0
    paquete = {}
    for s, info in ff.FUT.items():
        x = {}
        for m, dias in ((1, 1), (5, 3)):
            try:
                d = puente(f'/barras?inst={urllib.parse.quote(info["nt"])}&min={m}&dias={dias}')
            except SinPuente:
                continue
            # NinjaTrader marca la vela con su hora de CIERRE; el resto del sistema usa la de apertura
            x[f'{m}m'] = [{'t': int(v[0]) - 60 * m, 'o': v[1], 'h': v[2], 'l': v[3], 'c': v[4], 'v': v[5]} for v in d['velas'][-600:]]
        if x:
            paquete[s] = x
    if paquete:
        rq = urllib.request.Request(agente.CENTRAL + '/api/futuros/velas', data=json.dumps(paquete).encode(), method='POST',
                                    headers={'content-type': 'application/json', **({'x-token': agente.CENTRAL_TOKEN} if agente.CENTRAL_TOKEN else {})})
        urllib.request.urlopen(rq, timeout=20).read()
    return len(paquete)


def _pnl_hoy(e):
    hoy = time.strftime('%Y-%m-%d')
    _preparar_resultados()
    for o in e.get('ops') or []:
        if o.get('orden') and o.get('cuenta') and o.get('cerrada') and o.get('pnl') is not None:
            _registrar_resultado(o)
    cuenta = cfg().get('cuenta_real')
    fila = db.uno('SELECT COALESCE(SUM(pnl),0) pnl FROM fut_resultados WHERE dia=? AND real=1' + (' AND cuenta=?' if cuenta else ''), (hoy, cuenta) if cuenta else (hoy,))
    return fila['pnl']


def _preparar_resultados():
    db.ex('''CREATE TABLE IF NOT EXISTS fut_resultados (
        cuenta TEXT NOT NULL, orden TEXT NOT NULL, inst TEXT NOT NULL, cerrado INTEGER NOT NULL,
        dia TEXT NOT NULL, real INTEGER NOT NULL, pnl REAL NOT NULL, PRIMARY KEY(cuenta,orden))''')


def _registrar_resultado(op):
    pnl = op['pnl']
    if isinstance(pnl, bool) or not isinstance(pnl, (int, float)) or not math.isfinite(pnl):
        raise SinPuente('PnL no verificable para el libro durable')
    _preparar_resultados()
    dia = time.strftime('%Y-%m-%d', time.localtime(op['cerrada']))
    valores = (op['cuenta'], op['orden'], op.get('full') or op['nt'], op['cerrada'], dia, 1 if op.get('real') else 0, pnl)
    with db.transaccion():
        previo = db.uno('SELECT pnl,inst,real FROM fut_resultados WHERE cuenta=? AND orden=?', valores[:2])
        if previo and (previo['pnl'] != pnl or previo['inst'] != valores[2] or previo['real'] != valores[5]):
            raise SinPuente('resultado corregido: requiere conciliacion del libro')
        db.ex('INSERT OR IGNORE INTO fut_resultados(cuenta,orden,inst,cerrado,dia,real,pnl) VALUES(?,?,?,?,?,?,?)', valores)


def _autorizar_envio(c, e, cuenta, real, riesgo):
    actual = cfg()
    campos = ('activo','real_activo','cuenta_sim','cuenta_real','qty','real_perdida_dia','cambiado')
    if not actual['activo'] or any(actual.get(k) != c.get(k) for k in campos):
        raise SinPuente('piloto desactivado o configuracion modificada antes del envio')
    if real and (not actual['real_activo'] or actual.get('cuenta_real') != cuenta):
        raise SinPuente('cuenta real no autorizada en la configuracion actual')
    if not real and actual.get('cuenta_sim') != cuenta:
        raise SinPuente('cuenta simulada distinta de la configurada')
    estado_actual = puente('/estado')
    if estado_actual.get('contrato') != 'dmn-puente/2' or estado_actual.get('libro_sano') is False or estado_actual.get('riesgo_bloqueado') is True:
        raise SinPuente('puente no conciliado antes del envio')
    cu = next((x for x in estado_actual.get('cuentas', []) if x.get('nombre') == cuenta), None)
    if not cu or cu.get('conectada') is not True or cu.get('simulada') is not (not real):
        raise SinPuente('naturaleza o conexion de cuenta no confirmada')
    if real:
        valores = [cu.get('realizado'), cu.get('no_realizado')]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in valores):
            raise SinPuente('riesgo de cuenta no verificable antes del envio')
        clave = time.strftime('%Y-%m-%d') + '|' + cuenta
        base = e.get('riesgo_cuentas', {}).get(clave)
        if base is None:
            raise SinPuente('sesion de riesgo sin base persistida')
        pnl = min(cu['realizado'] + cu['no_realizado'], cu['realizado'] - base + cu['no_realizado'], _pnl_hoy(e))
        if riesgo_total(riesgo, actual['qty']) > actual['real_perdida_dia'] + min(0, pnl):
            raise SinPuente('presupuesto diario agotado antes del envio')


def _conciliar_orden(o):
    q = urllib.parse.urlencode({'cuenta': o['cuenta'], 'nombre': o['orden']})
    n = puente('/orden/estado?' + q)
    if not isinstance(n, dict) or n.get('cuenta') != o['cuenta'] or n.get('nombre') != o['orden']:
        raise SinPuente('respuesta de conciliacion fuera de ambito')
    estado = n.get('estado')
    if estado not in ('abierta_protegida','abierta_sin_proteccion','cerrada','sin_ejecucion','pendiente','desconocida'):
        raise SinPuente('estado de conciliacion no reconocido')
    if estado not in ('desconocida',) and n.get('inst') != o.get('full', o['nt']):
        raise SinPuente('instrumento de conciliacion distinto')
    if estado in ('abierta_protegida','abierta_sin_proteccion'):
        cantidad, prevista = n.get('cantidad'), o.get('qty')
        if isinstance(cantidad, bool) or not isinstance(cantidad, int) or isinstance(prevista, bool) or not isinstance(prevista, int) or not 0 < cantidad <= prevista:
            raise SinPuente('cantidad propia no verificable')
        if n.get('lado') != ('buy' if o['lado'] == 1 else 'sell'):
            raise SinPuente('lado de conciliacion distinto')
    if estado == 'cerrada':
        cierre = n.get('cierre')
        if not isinstance(cierre, dict) or any(cierre.get(k) != v for k,v in [('cuenta',o['cuenta']),('nombre',o['orden']),('inst',o.get('full',o['nt']))]):
            raise SinPuente('cierre de conciliacion fuera de ambito')
        cantidad, prevista = cierre.get('qty'), o.get('qty')
        if isinstance(cantidad, bool) or not isinstance(cantidad, int) or isinstance(prevista, bool) or not isinstance(prevista, int) or not 0 < cantidad <= prevista:
            raise SinPuente('cantidad cerrada no verificable')
    if estado == 'sin_ejecucion' and (type(n.get('cantidad')) is not int or n['cantidad'] != 0):
        raise SinPuente('cancelacion sin ejecucion no confirmada')
    return n


def paso():
    from . import conciencia, fabrica_fut as ff
    c, e = cfg(), _est()
    try:
        est = puente('/estado')
    except SinPuente as ex:
        if e.get('error') != str(ex):
            _log(e, f'⚠️ {ex}')
        e['error'] = str(ex)
        db.poner_ajuste(EST, e)
        return
    e['error'] = None
    if not isinstance(est, dict) or not isinstance(est.get('cuentas'), list):
        raise SinPuente('estado del puente incompleto')
    for cuenta in est['cuentas']:
        for campo in ('saldo', 'realizado', 'no_realizado'):
            valor = cuenta.get(campo)
            if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not math.isfinite(valor):
                raise SinPuente('riesgo de cuenta no verificable')
    cuentas = {x['nombre']: x for x in est['cuentas']}
    e['cuentas'] = [{k: x[k] for k in ('nombre', 'conexion', 'conectada', 'simulada', 'saldo', 'realizado', 'no_realizado')} for x in est['cuentas']]
    e['permitidas'] = est.get('permitidas')
    e['riesgo_bloqueado'] = est.get('riesgo_bloqueado') is True
    e['motivo_riesgo'] = est.get('motivo_riesgo')
    ab = e.setdefault('abiertas', {})
    libro = None
    if c.get('cerrar_todo', 0) > e.get('cerrar_hecho', 0):
        cierres_confirmados = True
        for o in ab.values():
            try:
                puente('/cerrar', {'cuenta': o['cuenta'], 'inst': o['nt']})
            except SinPuente as ex:
                cierres_confirmados = False
                _log(e, f"⚠️ No pude cerrar {o['nt']}: {ex}")
        if cierres_confirmados:
            e['cerrar_hecho'] = c['cerrar_todo']
        _log(e, f'🧹 Cerrar todo (desde el panel): {len(ab)} posiciones')
    # 1) cierres: la posición ya no está (stop/objetivo) o se pasó del tiempo
    for k, o in list(ab.items()):
        cu = cuentas.get(o['cuenta'])
        if not cu:
            continue
        cerrado_confirmado = False
        if 'dmn-orden-estado/1' in est.get('capacidades', []):
            try:
                conciliada = _conciliar_orden(o)
            except SinPuente as ex:
                o['estado'] = 'pendiente_conciliacion'
                e['error'] = str(ex)
                continue
            estado_orden = conciliada['estado']
            if estado_orden == 'sin_ejecucion':
                del ab[k]
                _log(e, 'orden no ejecutada confirmada por el puente: ' + o['orden'])
                db.poner_ajuste(EST, e)
                continue
            if estado_orden == 'abierta_protegida':
                o['estado'] = 'enviada'
                o.pop('sin_proteccion_desde', None)
                o['qty_ejecutada'] = conciliada['cantidad']
                viva = True
            elif estado_orden == 'abierta_sin_proteccion':
                o['estado'] = 'pendiente_conciliacion'
                ahora = time.time()
                o.setdefault('sin_proteccion_desde', ahora)
                if est.get('riesgo_bloqueado') is True or ahora - o['sin_proteccion_desde'] >= 5:
                    try:
                        puente('/cerrar', {'cuenta':o['cuenta'],'inst':o['nt']})
                    except SinPuente as ex:
                        e['error'] = 'cierre de posicion propia sin proteccion pendiente: ' + str(ex)[:120]
                continue
            elif estado_orden == 'cerrada':
                viva = False
                cerrado_confirmado = True
                registro = conciliada['cierre']
                if libro is None: libro = {}
                libro[(o['cuenta'],o.get('full',o['nt']),o['orden'])] = registro
            else:
                o['estado'] = 'pendiente_conciliacion'
                continue
        else:
            viva = any(p['inst'] == o.get('full', o['nt']) for p in cu['posiciones'])
        if not viva and (cerrado_confirmado or time.time() - o['ts'] > 20):
            if libro is None:
                try:
                    libro = {(x['cuenta'], x['inst'], x['nombre']): x for x in puente('/cerradas')['cerradas']}
                except SinPuente:
                    libro = {}
            clave_orden = (o['cuenta'], o.get('full', o['nt']), o.get('orden'))
            if clave_orden in libro:
                pnl = libro[clave_orden]['pnl']
                if isinstance(pnl, bool) or not isinstance(pnl, (int, float)) or not math.isfinite(pnl):
                    o['estado'] = 'pendiente_conciliacion'
                    continue
                # Resultado exclusivo de esta cuenta, instrumento y orden.
            elif time.time() - o['ts'] < 120:
                continue  # el libro aún no la tiene
            else:
                o['estado'] = 'pendiente_conciliacion'
                continue  # no atribuir operaciones manuales al piloto
            op = {**o, 'cerrada': int(time.time()), 'pnl': pnl}
            with db.transaccion():
                _registrar_resultado(op)
                e['ops'] = (e.get('ops') or [])[-299:] + [op]
                del ab[k]
                db.poner_ajuste(EST, e)
            _log(e, f"{'🎯' if pnl > 0 else '🛑'} {o['cuenta']} {o['nt']} ({o['estrategia']}): {pnl:+.2f} $")
            try:
                conciencia.operacion_real({'id': f"fut-{o['ts']}", 'simbolo': o['nt'], 'token': o['inst'], 'resultado': 'gana' if pnl > 0 else 'pierde',
                                           'pnl_sol': 0, 'estrategia': ('REAL · ' if o['real'] else 'Sim · ') + o['estrategia'], 'est_id': o['est_id'], 'cerrada': op['cerrada']})
            except Exception:
                pass
        elif viva and time.time() - o['ts'] > o['hold'] * 300:
            try:
                puente('/cerrar', {'cuenta': o['cuenta'], 'inst': o['nt']})
                _log(e, f"⌛ {o['cuenta']} {o['nt']}: cierre por tiempo ({o['hold'] * 5} min)")
            except SinPuente as ex:
                _log(e, f"⚠️ No pude cerrar {o['nt']}: {ex}")
    # 1b) protección (breakeven/trailing virtual) de las posiciones Sim y sombras para comparar con/sin
    if c.get('proteger'):
        for o in ab.values():
            if o.get('estado') == 'enviada' and o['cuenta'] in cuentas:
                try:
                    _proteger(e, o, cuentas[o['cuenta']])
                except SinPuente as ex:
                    _log(e, f"⚠️ Protección de {o['nt']}: {ex}")
    _sombras(e)
    if not c['activo']:
        db.poner_ajuste(EST, e)
        return
    if est.get('riesgo_bloqueado') is True:
        e['error'] = 'NinjaTrader: entradas bloqueadas por riesgo; ' + str(est.get('motivo_riesgo') or 'conciliacion necesaria')[:160]
        db.poner_ajuste(EST, e)
        return
    if est.get('libro_sano') is False:
        e['error'] = 'libro del puente pendiente de conciliacion; entradas bloqueadas'
        db.poner_ajuste(EST, e)
        return
    if est.get('contrato') != 'dmn-puente/2':
        e['error'] = 'puente sin contrato de ejecuciones parciales; compila la version nueva'
        db.poner_ajuste(EST, e)
        return
    if any(o.get('estado') == 'pendiente_conciliacion' for o in ab.values()):
        db.poner_ajuste(EST, e)
        return
    # 2) entradas, una vez por vela de 5 min
    vela = int(time.time() // 300)
    if e.get('ultima_vela') == vela:
        db.poner_ajuste(EST, e)
        return
    e['ultima_vela'] = vela
    barras = {}
    for s in ff.FUT:
        try:
            b = _barras(s)
            if b is not None:
                barras[s] = b
        except SinPuente:
            continue
    ocupado = {(o['cuenta'], o['nt']) for o in ab.values()}
    candidatas = ff._candidatas()
    promovidas = ff.promovidas()
    planes = [(c['cuenta_sim'], False, candidatas)]
    cu_real = cuentas.get(c.get('cuenta_real'))
    pnl_cuenta = None
    if cu_real:
        dia = time.strftime('%Y-%m-%d')
        clave = dia + '|' + cu_real['nombre']
        bases = e.setdefault('riesgo_cuentas', {})
        if clave not in bases:
            bases[clave] = cu_real['realizado']
            db.poner_ajuste(EST, e)
        pnl_cuenta = cu_real['realizado'] - bases[clave] + cu_real['no_realizado']
        # El libro local solo puede endurecer el presupuesto, nunca ampliarlo.
        pnl_cuenta = min(pnl_cuenta, cu_real['realizado'] + cu_real['no_realizado'], _pnl_hoy(e))
    if c.get('cuenta_real') and c.get('real_activo'):
        if pnl_cuenta is None or pnl_cuenta <= -c['real_perdida_dia']:
            if e.get('freno') != time.strftime('%Y-%m-%d'):
                e['freno'] = time.strftime('%Y-%m-%d')
                _log(e, f"⏸️ Cuenta real: pérdida del día {_pnl_hoy(e):+.2f} $ llegó al límite de {c['real_perdida_dia']} $: parada hasta mañana")
        elif promovidas:
            planes.append((c['cuenta_real'], True, promovidas))
    for cuenta, real, ests in planes:
        cu = cuentas.get(cuenta)
        if not cu or not cu['conectada']:
            continue
        if not real and cu.get('simulada') is not True:
            _log(e, 'Bloqueada cuenta no simulada en el plan de simulacion')
            continue
        for x in ff.senales_ahora(ests, barras):
            if (cuenta, x['nt']) in ocupado or any(p['inst'] == x['full'] for p in cu['posiciones']):
                continue
            if real:
                if any(o['real'] for o in ab.values()):
                    break  # en la real, una posición a la vez
                margen = c['real_perdida_dia'] + min(0, pnl_cuenta)
                if riesgo_total(x['riesgo_usd'], c['qty']) > margen:
                    _log(e, f"🚫 REAL {x['nt']}: el riesgo total supera el margen diario")
                    continue
            solicitud = f"{x['est_id']}-{vela}"
            try:
                _autorizar_envio(c, e, cuenta, real, x['riesgo_usd'])
            except SinPuente as ex:
                _log(e, f'entrada bloqueada: {ex}')
                continue
            nombre = 'DMN-E-' + hashlib.sha256((cuenta + '|' + x['full'] + '|' + solicitud).encode()).hexdigest().upper()[:32]
            ab[f"{cuenta}|{x['nt']}"] = {'cuenta': cuenta, 'real': real, 'nt': x['nt'], 'full': x['full'], 'inst': x['inst'], 'lado': x['lado'], 'ts': int(time.time()),
                                        'hold': x['hold'], 'qty': c['qty'] if real else 1, 'estrategia': x['estrategia'], 'est_id': x['est_id'], 'orden': nombre, 'estado': 'pendiente_conciliacion',
                                        'sl_ticks': x['sl_ticks'], 'tp_ticks': x['tp_ticks'], 'riesgo_usd': riesgo_total(x['riesgo_usd'], c['qty'] if real else 1), 'realizado_antes': cu['realizado']}
            db.poner_ajuste(EST, e)  # intencion durable antes del envio, tambien si se pierde la respuesta
            try:
                cuerpo = {'cuenta': cuenta, 'inst': x['nt'], 'lado': 'buy' if x['lado'] == 1 else 'sell', 'qty': c['qty'] if real else 1,
                                      'sl_ticks': x['sl_ticks'], 'tp_ticks': x['tp_ticks'], 'id': solicitud}
                if real:
                    r = ejecutor.ejecutar('ninjatrader', cuenta, lambda: puente('/orden', cuerpo))
                else:
                    r = puente('/orden', cuerpo)
                ab[f"{cuenta}|{x['nt']}"]['orden'] = r.get('nombre') or nombre
                ab[f"{cuenta}|{x['nt']}"]['estado'] = 'enviada'
            except ejecutor.Bloqueado as ex:
                ab.pop(f"{cuenta}|{x['nt']}", None)
                _log(e, f'{cuenta}: entrada no enviada; {ex}')
                db.poner_ajuste(EST, e)
                continue
            except (SinPuente, ValueError) as ex:
                _log(e, f"{cuenta} {x['nt']}: respuesta desconocida; conciliar antes de repetir")
                db.poner_ajuste(EST, e)
                continue
            ocupado.add((cuenta, x['nt']))
            _log(e, f"{'💵 REAL' if real else '🧪 Sim'} {'compra' if x['lado'] == 1 else 'vende en corto'} {x['nt']} por «{x['estrategia']}»: "
                    f"stop {x['sl_ticks']} ticks / objetivo {x['tp_ticks']} ticks (arriesga {riesgo_total(x['riesgo_usd'], c['qty'] if real else 1)} $), máx. {x['hold'] * 5} min")
    db.poner_ajuste(EST, e)


def _velas_1m(info):
    """Velas de 1 min desde NinjaTrader como (apertura, h, l, c), incluida la que se está formando (su cierre = último precio)."""
    d = puente('/barras?inst=' + urllib.parse.quote(info['nt']) + '&min=1&dias=1')
    return [(v[0] - 60, v[2], v[3], v[4]) for v in d.get('velas') or []]


def _proteger(e, o, cu):
    """Stop virtual de una posición Sim abierta. Devuelve True si ha mandado cerrarla."""
    from . import fabrica_fut as ff
    info = ff.FUT.get(o.get('inst'))
    if o.get('real') or not info:
        return False
    if o.get('proteccion'):  # ya decidió cerrar: reintenta hasta que el puente la confirme cerrada
        puente('/cerrar', {'cuenta': o['cuenta'], 'inst': o['nt']})
        return True
    if o.get('entrada') is None:
        p = next((x for x in cu.get('posiciones', []) if x.get('inst') == o.get('full', o['nt'])), None)
        precio = p and p.get('precio')
        if isinstance(precio, bool) or not isinstance(precio, (int, float)) or not math.isfinite(precio) or precio <= 0:
            return False
        o['entrada'] = precio
    o['protegida'] = True
    velas = [v for v in _velas_1m(info) if v[0] >= o['ts']]
    if not velas:
        return False
    t = info['tick']
    favor = [((h - o['entrada']) if o['lado'] == 1 else (o['entrada'] - l)) / t for _, h, l, _ in velas]
    o['mfe_ticks'] = max(o.get('mfe_ticks') or 0, round(max(favor), 1))
    stop = None
    if o['mfe_ticks'] >= PROTECCION['activar'] * o['tp_ticks']:
        stop = PROTECCION['colchon_ticks']
    if o['mfe_ticks'] >= PROTECCION['trailing_desde'] * o['tp_ticks']:
        stop = max(stop, math.floor(o['mfe_ticks'] * PROTECCION['trailing_guarda']))
    if stop is not None and stop != o.get('stop_virtual'):
        o['stop_virtual'] = stop
        _log(e, f"🛡️ {o['nt']}: stop virtual en +{stop} ticks (llegó a +{o['mfe_ticks']:g} de {o['tp_ticks']})")
    ahora = (velas[-1][3] - o['entrada']) * o['lado'] / t
    if stop is None or ahora > stop:
        return False
    o['proteccion'] = {'ts': int(time.time()), 'ticks': round(ahora, 1), 'mfe_ticks': o['mfe_ticks'], 'stop': stop}
    e.setdefault('sombras', []).append({'orden': o['orden'], 'inst': o['inst'], 'nt': o['nt'], 'lado': o['lado'], 'entrada': o['entrada'],
                                        'sl_ticks': o['sl_ticks'], 'tp_ticks': o['tp_ticks'], 'desde': int(time.time()),
                                        'hasta': o['ts'] + o['hold'] * 300, 'qty': o.get('qty_ejecutada') or o.get('qty') or 1, 'pnl': None})
    _log(e, f"🛡️ Protección: cierro {o['nt']} en {ahora:+.0f} ticks (llegó a +{o['mfe_ticks']:g}); sigo su sombra sin protección")
    db.poner_ajuste(EST, e)  # la sombra queda guardada antes de mandar el cierre
    puente('/cerrar', {'cuenta': o['cuenta'], 'inst': o['nt']})
    return True


def resolver_sombra(s, velas, info, ahora=None):
    """Lo que habría pasado sin protección: stop u objetivo originales (si los dos caben en la misma vela cuenta el stop)
    o, al pasar el `hold`, el último precio. None mientras no se sabe."""
    ahora = ahora or time.time()
    t, pv, q = info['tick'], info['pv'], s['qty']
    sl = s['entrada'] - s['lado'] * s['sl_ticks'] * t
    tp = s['entrada'] + s['lado'] * s['tp_ticks'] * t
    ultima = None
    for ap, h, l, c in velas:
        if ap < s['desde'] - 60:
            continue
        if ap >= s['hasta']:
            break
        if (l <= sl) if s['lado'] == 1 else (h >= sl):
            return round(-s['sl_ticks'] * t * pv * q, 2)
        if (h >= tp) if s['lado'] == 1 else (l <= tp):
            return round(s['tp_ticks'] * t * pv * q, 2)
        ultima = c
    if ahora >= s['hasta'] + 60 and ultima is not None:
        return round((ultima - s['entrada']) * s['lado'] * pv * q, 2)
    return None


def _sombras(e):
    from . import fabrica_fut as ff
    quedan = []
    for s in e.get('sombras') or []:
        if s['pnl'] is None:
            try:
                s['pnl'] = resolver_sombra(s, _velas_1m(ff.FUT[s['inst']]), ff.FUT[s['inst']])
            except SinPuente:
                pass
            if s['pnl'] is not None:
                _log(e, f"👻 Sombra de {s['nt']}: sin protección habría hecho {s['pnl']:+.2f} $")
        op = next((x for x in reversed(e.get('ops') or []) if x.get('orden') == s['orden']), None)
        if s['pnl'] is not None and op is not None:
            op['pnl_sin'] = s['pnl']
        else:
            quedan.append(s)
    e['sombras'] = quedan


def comparativa(e):
    """Operaciones Sim que pasaron por la protección: total CON (lo que hizo NinjaTrader) y SIN (la sombra o, si la
    protección no llegó a cerrar, lo mismo)."""
    ops = [o for o in e.get('ops') or [] if o.get('protegida') and isinstance(o.get('pnl'), (int, float))]
    hechas = [o for o in ops if not o.get('proteccion') or 'pnl_sin' in o]
    actuo = [o for o in hechas if o.get('proteccion')]
    con = round(sum(o['pnl'] for o in hechas), 2)
    sin = round(sum(o.get('pnl_sin', o['pnl']) for o in hechas), 2)
    return {'ops': len(hechas), 'actuo': len(actuo), 'pendientes': len(ops) - len(hechas) + sum(1 for s in e.get('sombras') or [] if s['pnl'] is None),
            'con': con, 'sin': sin, 'diferencia': round(con - sin, 2),
            'salvadas': sum(1 for o in actuo if o['pnl'] > o['pnl_sin']), 'recortadas': sum(1 for o in actuo if o['pnl'] < o['pnl_sin']),
            'detalle': [{'nt': o['nt'], 'estrategia': o['estrategia'], 'con': o['pnl'], 'sin': o['pnl_sin'],
                         'mfe_ticks': o['proteccion']['mfe_ticks'], 'cerrada': o['cerrada']} for o in actuo][-20:]}


def _bucle():
    while True:
        try:
            reenviar_velas()
        except Exception:
            pass
        if time.time() - _hilo.get('grafo', 0) > 300:
            _hilo['grafo'] = time.time()
            try:
                from . import conciencia
                conciencia.enviar_grafo()
            except Exception:
                pass
        try:
            paso()
        except Exception as ex:
            e = _est()
            _log(e, f'⚠️ Error del piloto de futuros: {str(ex)[:120]}')
            db.poner_ajuste(EST, e)
        time.sleep(cfg()['cada'])


def arrancar():
    from . import fabrica_fut as ff
    ff.arrancar()
    if not (_hilo['t'] and _hilo['t'].is_alive()):
        _hilo['t'] = threading.Thread(target=_bucle, daemon=True)
        _hilo['t'].start()
