"""PILOTO DE FUTUROS: opera NinjaTrader a través del AddOn DmnPuente (ninjatrader/DmnPuente.cs, http://localhost:36990).

- CUENTA SIM (Sim101): opera TODAS las candidatas de la fábrica de futuros (manuales + salón): es su prueba en vivo con
  ejecuciones reales de NinjaTrader.
- CUENTA REAL: solo las estrategias PROMOVIDAS (ganan en vivo) y con freno diario: si las pérdidas del día llegan a
  `real_perdida_dia` $ se para hasta el día siguiente, y ninguna entrada arriesga más de lo que queda de ese margen.
  Además la cuenta tiene que estar en `cuentas_orden` de Documents/NinjaTrader 8/dmn/puente.json (el AddOn rechaza las demás).
Toda entrada lleva stop y objetivo (OCO) que pone el AddOn al llenarse; el piloto cierra por tiempo al pasar el `hold`."""
import json, os, threading, time, urllib.request
from . import db

PUENTE = os.environ.get('DMN_NT_PUENTE', 'http://localhost:36990')
TOKEN = os.path.join(os.path.expanduser('~'), 'Documents', 'NinjaTrader 8', 'dmn', 'token.txt')
EST = 'piloto_fut'
DEFECTO = {'activo': True, 'cuenta_sim': 'Sim101', 'cuenta_real': None, 'real_activo': False, 'real_perdida_dia': 20.0, 'qty': 1, 'cada': 60,
           'cambiado': 0, 'cerrar_todo': 0}
# se controla desde el panel (en la Pi o en el PC): la configuración más reciente gana (real.py la sincroniza con el nodo)
_hilo = {'t': None}


class SinPuente(Exception):
    pass


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
    return {**DEFECTO, **(db.ajuste(EST + '_cfg') or {})}


def poner_cfg(c):
    nuevo = {**cfg(), **{k: v for k, v in (c or {}).items() if k in DEFECTO and k not in ('cambiado', 'cada')}}
    nuevo['real_perdida_dia'] = max(0.0, min(float(nuevo['real_perdida_dia']), 500.0))
    nuevo['qty'] = max(1, min(int(nuevo['qty']), 2))
    nuevo['cambiado'] = int(time.time())
    db.poner_ajuste(EST + '_cfg', nuevo)
    return nuevo


def cfg_mas_nueva(otra):
    if isinstance(otra, dict) and otra.get('cambiado', 0) > cfg().get('cambiado', 0):
        db.poner_ajuste(EST + '_cfg', otra)


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
    return {**_est(), 'cfg': cfg(), 'fabrica': res}


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


def _pnl_hoy(e):
    hoy = time.strftime('%Y-%m-%d')
    return sum(o.get('pnl') or 0 for o in e.get('ops') or [] if o.get('real') and time.strftime('%Y-%m-%d', time.localtime(o['cerrada'])) == hoy)


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
    cuentas = {x['nombre']: x for x in est['cuentas']}
    e['cuentas'] = [{k: x[k] for k in ('nombre', 'conexion', 'conectada', 'simulada', 'saldo', 'realizado', 'no_realizado')} for x in est['cuentas']]
    e['permitidas'] = est.get('permitidas')
    ab = e.setdefault('abiertas', {})
    libro = None
    if c.get('cerrar_todo', 0) > e.get('cerrar_hecho', 0):
        e['cerrar_hecho'] = c['cerrar_todo']
        for o in ab.values():
            try:
                puente('/cerrar', {'cuenta': o['cuenta'], 'inst': o['nt']})
            except SinPuente as ex:
                _log(e, f"⚠️ No pude cerrar {o['nt']}: {ex}")
        _log(e, f'🧹 Cerrar todo (desde el panel): {len(ab)} posiciones')
    # 1) cierres: la posición ya no está (stop/objetivo) o se pasó del tiempo
    for k, o in list(ab.items()):
        cu = cuentas.get(o['cuenta'])
        if not cu:
            continue
        viva = any(p['inst'] == o.get('full', o['nt']) for p in cu['posiciones'])  # NinjaTrader las nombra 'MES DEC26'
        if not viva and time.time() - o['ts'] > 20:
            if libro is None:
                try:
                    libro = {x['nombre']: x for x in puente('/cerradas')['cerradas']}
                except SinPuente:
                    libro = {}
            if o.get('orden') in libro:
                pnl = libro[o['orden']]['pnl']  # exacto: precio de entrada y salida y comisión de NinjaTrader
            elif time.time() - o['ts'] < 120:
                continue  # el libro aún no la tiene
            else:
                pnl = round(cu['realizado'] - o['realizado_antes'], 2)
            op = {**o, 'cerrada': int(time.time()), 'pnl': pnl}
            e['ops'] = (e.get('ops') or [])[-299:] + [op]
            del ab[k]
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
    if not c['activo']:
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
    if c.get('cuenta_real') and c.get('real_activo'):
        if _pnl_hoy(e) <= -c['real_perdida_dia']:
            if e.get('freno') != time.strftime('%Y-%m-%d'):
                e['freno'] = time.strftime('%Y-%m-%d')
                _log(e, f"⏸️ Cuenta real: pérdida del día {_pnl_hoy(e):+.2f} $ llegó al límite de {c['real_perdida_dia']} $: parada hasta mañana")
        elif promovidas:
            planes.append((c['cuenta_real'], True, promovidas))
    for cuenta, real, ests in planes:
        cu = cuentas.get(cuenta)
        if not cu or not cu['conectada']:
            continue
        for x in ff.senales_ahora(ests, barras):
            if (cuenta, x['nt']) in ocupado or any(p['inst'] == x['full'] for p in cu['posiciones']):
                continue
            if real:
                if any(o['real'] for o in ab.values()):
                    break  # en la real, una posición a la vez
                margen = c['real_perdida_dia'] + _pnl_hoy(e)
                if x['riesgo_usd'] > margen:
                    _log(e, f"🚫 REAL {x['nt']} ({x['estrategia']}): arriesga {x['riesgo_usd']} $ y solo quedan {margen:.2f} $ del día")
                    continue
            try:
                r = puente('/orden', {'cuenta': cuenta, 'inst': x['nt'], 'lado': 'buy' if x['lado'] == 1 else 'sell', 'qty': c['qty'] if real else 1,
                                      'sl_ticks': x['sl_ticks'], 'tp_ticks': x['tp_ticks'], 'id': f"{x['est_id']}-{vela}"})
            except SinPuente as ex:
                _log(e, f"⚠️ {cuenta} {x['nt']}: orden rechazada: {ex}")
                continue
            ab[f"{cuenta}|{x['nt']}"] = {'cuenta': cuenta, 'real': real, 'nt': x['nt'], 'full': x['full'], 'inst': x['inst'], 'lado': x['lado'], 'ts': int(time.time()),
                                        'hold': x['hold'], 'estrategia': x['estrategia'], 'est_id': x['est_id'], 'orden': r.get('nombre'),
                                        'sl_ticks': x['sl_ticks'], 'tp_ticks': x['tp_ticks'], 'riesgo_usd': x['riesgo_usd'], 'realizado_antes': cu['realizado']}
            ocupado.add((cuenta, x['nt']))
            _log(e, f"{'💵 REAL' if real else '🧪 Sim'} {'compra' if x['lado'] == 1 else 'vende en corto'} {x['nt']} por «{x['estrategia']}»: "
                    f"stop {x['sl_ticks']} ticks / objetivo {x['tp_ticks']} ticks (arriesga {x['riesgo_usd']} $), máx. {x['hold'] * 5} min")
    db.poner_ajuste(EST, e)


def _bucle():
    while True:
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
