"""Motor de trading simulado (paper trading) con precios REALES.
Órdenes: mercado, límite, stop, stop-límite y trailing stop; brackets (stop-loss + take-profit OCO); largos y cortos.
Gestión de riesgo: % de riesgo por operación, pérdida diaria máxima (bloquea la cuenta hasta mañana), máximo de posiciones,
apalancamiento, stop obligatorio opcional. Cada operación cerrada queda en el diario con su R, MFE/MAE y notas.
Cuentas "replay": practicar sobre velas históricas avanzando vela a vela, sin ver el futuro."""
import json, threading, time, uuid, math
from . import datos, db, metricas, onchain

_cerrojos, _cerrojo_g = {}, threading.Lock()


def _cerrojo(cid):
    """Un cerrojo por cuenta: el bucle del servidor y el agente nunca ejecutan la misma orden dos veces."""
    with _cerrojo_g:
        return _cerrojos.setdefault(cid, threading.RLock())

CONFIG_DEFECTO = {
    'comision_fija': 0.0, 'comision_pct': 0.0, 'deslizamiento_pct': 0.02, 'riesgo_max_pct': 1.0,
    'perdida_diaria_max_pct': 3.0, 'max_posiciones': 8, 'apalancamiento': 1.0, 'exigir_stop': False,
    'riesgo_estricto': True, 'permitir_cortos': True, 'webhook': '', 'comision_por_tipo': {},
}
TIPOS = ('mercado', 'limite', 'stop', 'stop_limite', 'trailing')
EPS = 1e-9


class ErrorOrden(Exception):
    pass


def _numero(valor, nombre, cero=False):
    if isinstance(valor, bool):
        raise ErrorOrden(nombre + ' debe ser un numero finito')
    try:
        n = float(valor)
    except (ValueError, TypeError, OverflowError):
        raise ErrorOrden(nombre + ' debe ser un numero finito')
    if not math.isfinite(n) or n < 0 or (not cero and n == 0):
        raise ErrorOrden(nombre + ' fuera de rango')
    return n


def _validar_config(cfg):
    for k, defecto in CONFIG_DEFECTO.items():
        valor = cfg[k]
        if isinstance(defecto, bool):
            if not isinstance(valor, bool):
                raise ErrorOrden(k + ' debe ser booleano')
        elif isinstance(defecto, (int, float)):
            cfg[k] = _numero(valor, k, cero=k in ('comision_fija', 'comision_pct', 'deslizamiento_pct', 'perdida_diaria_max_pct'))
            if isinstance(defecto, int) and cfg[k] != int(cfg[k]):
                raise ErrorOrden(k + ' debe ser entero')
        elif k == 'comision_por_tipo':
            if not isinstance(valor, dict):
                raise ErrorOrden(k + ' debe ser objeto')
            cfg[k] = {t: _numero(n, 'comision', cero=True) for t, n in valor.items()}
    return cfg


def _reservado(cid, excluir=None):
    total = 0.0
    c = _fila(cid)
    for o in db.q("SELECT * FROM ordenes WHERE cuenta_id=? AND reduce=0 AND padre_id IS NULL AND estado='pendiente'", (cid,)):
        if o['id'] == excluir:
            continue
        # Se persiste el precio de referencia en trail_ref para las entradas sin limite.
        ref = o['limite'] or o['stop'] or o['trail_ref']
        if not ref:
            raise ErrorOrden('orden antigua sin reserva verificable: cancela o concilia antes de abrir')
        b = barra(c, o['simbolo'])
        fx = _fx(c, b.get('moneda'))
        cfg = c['config']
        total += o['cantidad'] * ref * fx * (1 + max([cfg['comision_pct'], onchain.COMISION_PCT if b.get('tipo_activo') == 'ONCHAIN' else 0, *(cfg.get('comision_por_tipo') or {}).values()]) / 100) + cfg['comision_fija']
    return total


def ahora():
    return int(time.time())


def hoy():
    return time.strftime('%Y-%m-%d', time.gmtime())

# ---------------- cuentas ----------------


def crear_cuenta(nombre, capital=10000, moneda='USD', config=None, tipo='simulada', replay=None):
    cfg = _validar_config({**CONFIG_DEFECTO, **(config or {})})
    tipo = 'replay' if replay else tipo
    capital = _numero(capital, 'capital')
    if capital <= 0:
        raise ErrorOrden('el capital tiene que ser positivo')
    cid = db.ex('INSERT INTO cuentas(nombre,tipo,moneda,capital_inicial,efectivo,creada,config) VALUES(?,?,?,?,?,?,?)',
                (nombre, tipo, moneda.upper(), capital, capital, ahora(), json.dumps(cfg)))
    if replay:
        v = datos.velas(replay['simbolo'], replay.get('intervalo', '1d'), replay.get('rango'), replay.get('inicio'), replay.get('fin'))
        if len(v) < 30:
            raise ErrorOrden('muy pocas velas para practicar')
        idx = min(int(replay.get('calentamiento', 100)), len(v) - 2)
        if replay.get('azar') and len(v) - 60 > idx:  # empezar en un punto al azar: no sabes qué viene
            import random
            idx = random.randint(idx, len(v) - 60)
        db.ex('UPDATE cuentas SET replay_simbolo=?, replay_intervalo=?, replay_idx=?, replay_velas=?, creada=? WHERE id=?',
              (replay['simbolo'].upper(), replay.get('intervalo', '1d'), idx, json.dumps(v), v[idx]['t'], cid))
    instantanea(cid)
    return cuenta(cid)


def _fila(cid):
    c = db.uno('SELECT * FROM cuentas WHERE id=?', (cid,))
    if not c:
        raise ErrorOrden(f'no existe la cuenta {cid}')
    c['config'] = _validar_config({**CONFIG_DEFECTO, **json.loads(c['config'] or '{}')})
    return c


def configurar(cid, cambios):
    c = _fila(cid)
    cfg = _validar_config({**c['config'], **{k: v for k, v in cambios.items() if k in CONFIG_DEFECTO}})
    db.ex('UPDATE cuentas SET config=? WHERE id=?', (json.dumps(cfg), cid))
    if 'nombre' in cambios:
        db.ex('UPDATE cuentas SET nombre=? WHERE id=?', (cambios['nombre'], cid))
    return cuenta(cid)


def reiniciar(cid):
    c = _fila(cid)
    for t in ('ordenes', 'posiciones', 'ejecuciones', 'trades', 'equity'):
        db.ex(f'DELETE FROM {t} WHERE cuenta_id=?', (cid,))
    db.ex('UPDATE cuentas SET efectivo=capital_inicial, inicio_dia=NULL, equity_inicio_dia=NULL, bloqueada_hasta=NULL WHERE id=?', (cid,))
    instantanea(cid)
    return cuenta(cid)


def borrar(cid):
    _fila(cid)
    for t in ('ordenes', 'posiciones', 'ejecuciones', 'trades', 'equity'):
        db.ex(f'DELETE FROM {t} WHERE cuenta_id=?', (cid,))
    db.ex('DELETE FROM cuentas WHERE id=?', (cid,))


def lista_cuentas():
    return [resumen(c['id']) for c in db.q('SELECT id FROM cuentas WHERE archivada=0 ORDER BY id')]

# ---------------- precios ----------------


def _velas_replay(c):
    return json.loads(c['replay_velas'])


def barra(c, simbolo):
    """Barra contra la que se ejecutan órdenes: en vivo, el último precio; en replay, la vela actual."""
    if c['tipo'] == 'replay':
        if simbolo != c['replay_simbolo']:
            raise ErrorOrden(f'esta cuenta de práctica solo opera {c["replay_simbolo"]}')
        b = _velas_replay(c)[c['replay_idx']]
        return {**b, 'moneda': None, 'abierto': True}
    q = datos.cotizacion(simbolo)
    p = q['precio']
    return {'t': q['ts'] or ahora(), 'o': p, 'h': p, 'l': p, 'c': p, 'moneda': q['moneda'], 'abierto': q['abierto'], 'tipo_activo': q['tipo'],
            'bid': q.get('bid'), 'ask': q.get('ask')}


def precio(c, simbolo):
    return barra(c, simbolo)['c']


def _fx(c, moneda):
    if c['tipo'] == 'replay' or not moneda:
        return 1.0
    return _numero(datos.fx(moneda, c['moneda']), 'tipo de cambio')


def _reloj(c):
    return _velas_replay(c)[c['replay_idx']]['t'] if c['tipo'] == 'replay' else ahora()

# ---------------- estado ----------------


def posiciones(cid, con_precio=True):
    c = _fila(cid)
    out = []
    for p in db.q('SELECT * FROM posiciones WHERE cuenta_id=? ORDER BY abierta', (cid,)):
        if con_precio:
            try:
                b = barra(c, p['simbolo'])
                px, fx = b['c'], _fx(c, p['moneda'])
            except Exception:
                px, fx = p['precio_medio'], _fx(c, p['moneda'])
            p['precio'] = px
            p['valor'] = round(p['cantidad'] * px * fx, 2)
            p['pnl'] = round(p['cantidad'] * (px - p['precio_medio']) * fx, 2)
            p['pnl_pct'] = round((px / p['precio_medio'] - 1) * 100 * (1 if p['cantidad'] > 0 else -1), 2) if p['precio_medio'] else 0
        hijos = db.q("SELECT id,tipo,stop,limite,trail_pct FROM ordenes WHERE cuenta_id=? AND simbolo=? AND estado='pendiente' AND reduce=1",
                     (cid, p['simbolo']))
        p['stop'] = next((o['stop'] for o in hijos if o['tipo'] in ('stop', 'trailing')), None)
        p['objetivo'] = next((o['limite'] for o in hijos if o['tipo'] == 'limite'), None)
        p['lado'] = 'largo' if p['cantidad'] > 0 else 'corto'
        out.append(p)
    return out


def resumen(cid, pos=None):
    c = _fila(cid)
    pos = posiciones(cid) if pos is None else pos
    valor = sum(p.get('valor', 0) for p in pos)
    equity = c['efectivo'] + valor
    bruto = sum(abs(p.get('valor', 0)) for p in pos)
    eq_dia = c['equity_inicio_dia'] if c['inicio_dia'] == hoy() else None
    r = {
        'id': c['id'], 'nombre': c['nombre'], 'tipo': c['tipo'], 'moneda': c['moneda'], 'capital_inicial': c['capital_inicial'],
        'efectivo': round(c['efectivo'], 2), 'valor_posiciones': round(valor, 2), 'equity': round(equity, 2),
        'pnl_total': round(equity - c['capital_inicial'], 2), 'pnl_total_pct': round((equity / c['capital_inicial'] - 1) * 100, 2),
        'pnl_dia': round(equity - eq_dia, 2) if eq_dia else 0.0,
        'pnl_dia_pct': round((equity / eq_dia - 1) * 100, 2) if eq_dia else 0.0,
        'exposicion_bruta': round(bruto, 2), 'poder_compra': round(max(0.0, equity * c['config']['apalancamiento'] - bruto), 2),
        'posiciones_abiertas': len(pos), 'bloqueada': c['bloqueada_hasta'] == hoy(), 'config': c['config'],
    }
    if c['tipo'] == 'replay':
        v = _velas_replay(c)
        r.update(replay={'simbolo': c['replay_simbolo'], 'intervalo': c['replay_intervalo'], 'idx': c['replay_idx'],
                         'total': len(v), 't': v[c['replay_idx']]['t']})
    return r


def cuenta(cid):
    pos = posiciones(cid)
    return {**resumen(cid, pos), 'posiciones': pos, 'ordenes': ordenes(cid, 'pendiente')}


def ordenes(cid, estado=None, limite=200):
    if estado:
        return db.q("SELECT * FROM ordenes WHERE cuenta_id=? AND estado IN ('pendiente','espera') ORDER BY id DESC LIMIT ?", (cid, limite)) \
            if estado == 'pendiente' else db.q('SELECT * FROM ordenes WHERE cuenta_id=? AND estado=? ORDER BY id DESC LIMIT ?', (cid, estado, limite))
    return db.q('SELECT * FROM ordenes WHERE cuenta_id=? ORDER BY id DESC LIMIT ?', (cid, limite))


def instantanea(cid, ts=None):
    c = _fila(cid)
    r = resumen(cid)
    db.ex('INSERT INTO equity(cuenta_id,ts,valor,efectivo) VALUES(?,?,?,?)', (cid, ts or _reloj(c), r['equity'], r['efectivo']))
    return r


def _control_dia(c, eq):
    """Guarda el equity al empezar el día y bloquea la cuenta si se supera la pérdida diaria máxima."""
    if c['tipo'] == 'replay':
        return False
    if c['inicio_dia'] != hoy():
        db.ex('UPDATE cuentas SET inicio_dia=?, equity_inicio_dia=? WHERE id=?', (hoy(), eq, c['id']))
        return False
    lim = c['config']['perdida_diaria_max_pct']
    if lim and c['equity_inicio_dia'] and (eq / c['equity_inicio_dia'] - 1) * 100 <= -lim:
        if c['bloqueada_hasta'] != hoy():
            db.ex('UPDATE cuentas SET bloqueada_hasta=? WHERE id=?', (hoy(), c['id']))
            avisos.append(f'🛑 Cuenta «{c["nombre"]}» bloqueada hasta mañana: pérdida diaria de {lim}% alcanzada.')
        return True
    return c['bloqueada_hasta'] == hoy()

# ---------------- órdenes ----------------


def tamano_por_riesgo(cid, simbolo, entrada, stop, riesgo_pct=None):
    """Cuántas unidades comprar para arriesgar riesgo_pct del equity si salta el stop."""
    c = _fila(cid)
    r = resumen(cid)
    pct = float(riesgo_pct or c['config']['riesgo_max_pct'])
    entrada = float(entrada) if entrada else precio(c, simbolo)
    dist = abs(entrada - float(stop))
    if dist <= 0:
        raise ErrorOrden('el stop no puede ser igual a la entrada')
    try:
        fx = _fx(c, datos.cotizacion(simbolo)['moneda']) if c['tipo'] != 'replay' else 1.0
    except datos.ErrorDatos:
        fx = 1.0
    arriesgar = r['equity'] * pct / 100
    qty = arriesgar / (dist * fx)
    tope = r['poder_compra'] / (entrada * fx) if entrada else qty
    return {'cantidad': round(min(qty, tope), 6), 'cantidad_por_riesgo': round(qty, 6), 'riesgo': round(arriesgar, 2),
            'riesgo_pct': pct, 'distancia_stop': round(dist, 6), 'distancia_stop_pct': round(dist / entrada * 100, 2),
            'valor_posicion': round(min(qty, tope) * entrada * fx, 2), 'limitado_por_poder_compra': tope < qty}


def enviar(cid, *a, **k):
    with _cerrojo(cid):
        return _enviar(cid, *a, **k)


def _enviar(cid, simbolo, lado, cantidad=None, tipo='mercado', limite=None, stop=None, trail_pct=None,
           sl=None, tp=None, importe=None, nota=None, setup=None, origen='manual', oco=None, padre_id=None, reduce=False, _barra=None, _procesar_ahora=True):
    from . import guardia
    try:
        guardia.admitir(cid, reduce)
    except ValueError as e:
        raise ErrorOrden(str(e))
    c = _fila(cid)
    simbolo, lado, tipo = simbolo.strip().upper(), lado.lower(), tipo.lower()
    lado = {'compra': 'buy', 'comprar': 'buy', 'buy': 'buy', 'long': 'buy', 'venta': 'sell', 'vender': 'sell', 'sell': 'sell', 'short': 'sell'}.get(lado)
    if not lado:
        raise ErrorOrden('lado inválido: usa compra o venta')
    if tipo not in TIPOS:
        raise ErrorOrden(f'tipo inválido: {", ".join(TIPOS)}')
    cantidad = _numero(cantidad, 'cantidad') if cantidad not in (None, '') else None
    limite = _numero(limite, 'limite') if limite is not None else None
    stop = _numero(stop, 'stop') if stop is not None else None
    trail_pct = _numero(trail_pct, 'trailing') if trail_pct is not None else None
    sl = _numero(sl, 'stop-loss') if sl is not None else None
    tp = _numero(tp, 'take-profit') if tp is not None else None
    importe = _numero(importe, 'importe') if importe is not None else None
    if trail_pct is not None and trail_pct >= 100:
        raise ErrorOrden('trailing debe ser menor de 100')
    b = _barra if _barra is not None else barra(c, simbolo)
    ref = _numero(limite or stop or b['c'], 'precio')
    if cantidad in (None, '', 0) and importe:
        cantidad = float(importe) / (ref * _fx(c, b.get('moneda')))
    cantidad = float(cantidad or 0)
    if cantidad <= 0:
        raise ErrorOrden('cantidad inválida')
    if tipo in ('limite', 'stop_limite') and not limite:
        raise ErrorOrden('falta el precio límite')
    if tipo in ('stop', 'stop_limite') and not stop:
        raise ErrorOrden('falta el precio stop')
    if tipo == 'trailing' and not trail_pct:
        raise ErrorOrden('falta el % del trailing stop')
    signo = 1 if lado == 'buy' else -1
    pos = db.uno('SELECT * FROM posiciones WHERE cuenta_id=? AND simbolo=?', (cid, simbolo))
    actual = pos['cantidad'] if pos else 0.0
    abre = not reduce and (actual == 0 or (actual > 0) == (signo > 0) or cantidad > abs(actual) + EPS)
    avisos = []
    if abre and not padre_id:
        r = resumen(cid)
        cfg = c['config']
        if _control_dia(c, r['equity']):
            raise ErrorOrden('cuenta bloqueada por pérdida diaria máxima: hoy ya no se abren operaciones (regla de riesgo)')
        if signo < 0 and actual <= 0 and not cfg['permitir_cortos']:
            raise ErrorOrden('los cortos están desactivados en esta cuenta')
        pendientes = {o['simbolo'] for o in db.q("SELECT simbolo FROM ordenes WHERE cuenta_id=? AND reduce=0 AND estado='pendiente'", (cid,))}
        existentes = {p['simbolo'] for p in posiciones(cid, False)}
        if simbolo not in existentes | pendientes and len(existentes | pendientes) >= cfg['max_posiciones']:
            raise ErrorOrden(f'máximo de {cfg["max_posiciones"]} posiciones abiertas')
        protegida = pos and (actual > 0) == (signo > 0) and db.uno(  # ampliar (DCA) una posición que ya tiene su stop puesto
            "SELECT 1 FROM ordenes WHERE cuenta_id=? AND simbolo=? AND reduce=1 AND tipo IN ('stop','trailing') AND estado='pendiente'", (cid, simbolo))
        if cfg['exigir_stop'] and not sl and not protegida:
            raise ErrorOrden('esta cuenta exige stop-loss en cada entrada (pon sl)')
        fx = _fx(c, b.get('moneda'))
        if sl:
            if (signo > 0 and float(sl) >= ref) or (signo < 0 and float(sl) <= ref):
                raise ErrorOrden('el stop-loss está del lado equivocado de la entrada')
            riesgo = cantidad * abs(ref - float(sl)) * fx
            maximo = r['equity'] * cfg['riesgo_max_pct'] / 100
            if riesgo > maximo + 0.01:
                sug = tamano_por_riesgo(cid, simbolo, ref, sl)['cantidad']
                msg = f'riesgo {riesgo:.2f} {c["moneda"]} supera tu máximo del {cfg["riesgo_max_pct"]}% ({maximo:.2f}); tamaño correcto: {sug}'
                if cfg['riesgo_estricto']:
                    raise ErrorOrden(msg)
                avisos.append(msg)
        else:
            avisos.append('entrada sin stop-loss: un profesional siempre sabe dónde se sale')
        reservado = _reservado(cid)
        nuevo = cantidad * ref * fx * (1 + max([cfg['comision_pct'], onchain.COMISION_PCT if b.get('tipo_activo') == 'ONCHAIN' else 0, *(cfg.get('comision_por_tipo') or {}).values()]) / 100) + cfg['comision_fija']
        r['poder_compra'] = max(0, r['poder_compra'] - reservado)
        if nuevo > r['poder_compra'] + 0.01 and not (actual and (actual > 0) != (signo > 0)):
            raise ErrorOrden(f'sin poder de compra: necesitas {nuevo:.2f} y tienes {r["poder_compra"]:.2f} {c["moneda"]}')
        if sl and tp and ((signo > 0 and float(tp) <= ref) or (signo < 0 and float(tp) >= ref)):
            raise ErrorOrden('el take-profit está del lado equivocado')
    t = _reloj(c)
    with db.transaccion():
        oid = db.ex("""INSERT INTO ordenes(cuenta_id,simbolo,lado,tipo,cantidad,limite,stop,trail_pct,trail_ref,estado,creada,padre_id,oco,reduce,nota,setup,origen,sl,tp)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (cid, simbolo, lado, tipo, cantidad, limite, stop, trail_pct, b['c'] if tipo == 'trailing' else ref,
                     'espera' if padre_id else 'pendiente', t, padre_id, oco, 1 if reduce else 0, nota, setup, origen, sl, tp))
        if not padre_id and (sl or tp):
            grupo = uuid.uuid4().hex[:8]
            lado_h = 'sell' if signo > 0 else 'buy'
            if sl:
                enviar(cid, simbolo, lado_h, cantidad, 'stop', stop=float(sl), padre_id=oid, oco=grupo, reduce=True, origen=origen, _barra=b)
            if tp:
                enviar(cid, simbolo, lado_h, cantidad, 'limite', limite=float(tp), padre_id=oid, oco=grupo, reduce=True, origen=origen, _barra=b)
    if not padre_id:
        if c['tipo'] != 'replay' and _procesar_ahora:
            procesar(cid, [simbolo])
        if not b.get('abierto', True) and tipo == 'mercado':
            avisos.append('mercado cerrado: la orden se ejecutará en la apertura')
    o = db.uno('SELECT * FROM ordenes WHERE id=?', (oid,))
    o['avisos'] = avisos
    return o


def cancelar(cid, oid):
    with _cerrojo(cid), db.transaccion():
        return _cancelar(cid, oid)


def _cancelar(cid, oid):
    o = db.uno('SELECT * FROM ordenes WHERE id=? AND cuenta_id=?', (oid, cid))
    if not o or o['estado'] not in ('pendiente', 'espera'):
        raise ErrorOrden('esa orden no está pendiente')
    db.ex("UPDATE ordenes SET estado='cancelada', motivo='cancelada por el usuario' WHERE id=? OR (padre_id=? AND estado='espera')", (oid, oid))
    return db.uno('SELECT * FROM ordenes WHERE id=?', (oid,))


def cerrar(cid, simbolo, porcentaje=100, nota=None):
    with _cerrojo(cid):
        return _cerrar(cid, simbolo, porcentaje, nota)


def _cerrar(cid, simbolo, porcentaje=100, nota=None):
    simbolo = simbolo.upper()
    p = db.uno('SELECT * FROM posiciones WHERE cuenta_id=? AND simbolo=?', (cid, simbolo))
    if not p:
        raise ErrorOrden(f'no hay posición en {simbolo}')
    porcentaje = _numero(porcentaje, 'porcentaje')
    if porcentaje > 100:
        raise ErrorOrden('porcentaje debe ser menor o igual a 100')
    qty = abs(p['cantidad']) * porcentaje / 100
    if float(porcentaje) >= 100:
        pendiente = db.uno("SELECT * FROM ordenes WHERE cuenta_id=? AND simbolo=? AND reduce=1 AND tipo='mercado' AND estado IN ('pendiente','espera') AND cantidad>=? ORDER BY id LIMIT 1", (cid, simbolo, qty - EPS))
        if pendiente:
            return pendiente
    # Las protecciones siguen hasta el cierre efectivo; _llenar cancela las hermanas al quedar plana.
    return enviar(cid, simbolo, 'sell' if p['cantidad'] > 0 else 'buy', qty, 'mercado', reduce=True, nota=nota or 'cierre manual')


def mover_stop(cid, simbolo, stop=None, objetivo=None, trail_pct=None):
    # Preparar datos antes de retirar la proteccion; el resto se revierte ante error.
    with _cerrojo(cid):
        c = _fila(cid)
        b = barra(c, simbolo.upper())
        fx = _fx(c, b.get('moneda'))
        with db.transaccion():
            return _mover_stop(cid, simbolo, stop, objetivo, trail_pct, b, fx)


def _mover_stop(cid, simbolo, stop=None, objetivo=None, trail_pct=None, b=None, fx=1.0):
    """Pone o mueve el stop-loss / objetivo de una posición abierta (p. ej. stop a breakeven)."""
    simbolo = simbolo.upper()
    p = db.uno('SELECT * FROM posiciones WHERE cuenta_id=? AND simbolo=?', (cid, simbolo))
    if not p:
        raise ErrorOrden(f'no hay posición en {simbolo}')
    lado = 'sell' if p['cantidad'] > 0 else 'buy'
    if stop:  # si el trade no tenía stop, este pasa a ser su stop inicial (para medir R)
        t = db.uno('SELECT * FROM trades WHERE id=?', (p['trade_id'],))
        if t and not t['riesgo']:
            db.ex('UPDATE trades SET stop_inicial=?, riesgo=? WHERE id=?',
                  (float(stop), abs(p['cantidad']) * abs(p['precio_medio'] - float(stop)) * fx, t['id']))
    grupo = (db.uno("SELECT oco FROM ordenes WHERE cuenta_id=? AND simbolo=? AND reduce=1 AND estado='pendiente' AND oco IS NOT NULL", (cid, simbolo)) or {}).get('oco') \
        or uuid.uuid4().hex[:8]
    if stop is not None or trail_pct:
        db.ex("UPDATE ordenes SET estado='cancelada', motivo='stop movido' WHERE cuenta_id=? AND simbolo=? AND reduce=1 AND estado='pendiente' AND tipo IN ('stop','trailing')", (cid, simbolo))
        if trail_pct:
            enviar(cid, simbolo, lado, abs(p['cantidad']), 'trailing', trail_pct=float(trail_pct), reduce=True, oco=grupo, _barra=b, _procesar_ahora=False)
        elif stop:
            enviar(cid, simbolo, lado, abs(p['cantidad']), 'stop', stop=float(stop), reduce=True, oco=grupo, _barra=b, _procesar_ahora=False)
    if objetivo is not None:
        db.ex("UPDATE ordenes SET estado='cancelada', motivo='objetivo movido' WHERE cuenta_id=? AND simbolo=? AND reduce=1 AND estado='pendiente' AND tipo='limite'", (cid, simbolo))
        if objetivo:
            enviar(cid, simbolo, lado, abs(p['cantidad']), 'limite', limite=float(objetivo), reduce=True, oco=grupo, _barra=b, _procesar_ahora=False)
    return posiciones(cid, False)

# ---------------- ejecución ----------------


def _precio_ejecucion(o, b, slip):
    """Devuelve el precio de ejecución si la orden se dispara con esta barra, o None. Actualiza trailing/activación."""
    compra = o['lado'] == 'buy'
    if o['tipo'] == 'mercado':  # con libro real (Coinbase) se compra al ask y se vende al bid: el spread se paga
        base = (b.get('ask') if compra else b.get('bid')) or b['o']
        return base * (1 + slip if compra else 1 - slip)
    if o['tipo'] == 'trailing':
        ref = o['trail_ref'] or b['c']
        ref = min(ref, b['l']) if compra else max(ref, b['h'])
        st = ref * (1 + o['trail_pct'] / 100) if compra else ref * (1 - o['trail_pct'] / 100)
        db.ex('UPDATE ordenes SET trail_ref=?, stop=? WHERE id=?', (ref, st, o['id']))
        o['stop'] = st
        if (compra and b['h'] >= st) or (not compra and b['l'] <= st):
            return (max(st, b['o']) * (1 + slip)) if compra else (min(st, b['o']) * (1 - slip))
        return None
    if o['tipo'] in ('stop', 'stop_limite') and not o['activada']:
        if (compra and b['h'] >= o['stop']) or (not compra and b['l'] <= o['stop']):
            if o['tipo'] == 'stop':
                return (max(o['stop'], b['o']) * (1 + slip)) if compra else (min(o['stop'], b['o']) * (1 - slip))
            db.ex('UPDATE ordenes SET activada=1 WHERE id=?', (o['id'],))
            o['activada'] = 1
        else:
            return None
    if o['tipo'] in ('limite', 'stop_limite'):
        L = o['limite']
        if compra and b['l'] <= L:
            return min(L, b['o'])
        if not compra and b['h'] >= L:
            return max(L, b['o'])
    return None


def _llenar(c, o, px, ts, moneda, tipo_activo=None):
    px = _numero(px, 'precio de ejecucion')
    fx = _fx(c, moneda)
    # Cotizaciones y reservas se consultan antes de abrir la transaccion SQLite.
    r = resumen(c['id']) if not o['reduce'] else None
    disponible = r['poder_compra'] - _reservado(c['id'], o['id']) if r else None
    if r and _control_dia(c, r['equity']):
        cancelar(c['id'], o['id'])
        return False
    avisos = []
    with db.transaccion():
        actual = db.uno('SELECT * FROM ordenes WHERE id=?', (o['id'],))
        if not actual or actual['estado'] != 'pendiente':
            return False
        resultado = _llenar_guardado(_fila(c['id']), actual, px, ts, moneda, tipo_activo, fx, disponible, avisos, r['equity'] if r else None)
    for texto in avisos:
        avisar(c, texto)
    return resultado


def _llenar_guardado(c, o, px, ts, moneda, tipo_activo=None, fx=1.0, disponible=None, avisos=None, equity=None):
    cid, sym = c['id'], o['simbolo']
    cfg = c['config']
    p = db.uno('SELECT * FROM posiciones WHERE cuenta_id=? AND simbolo=?', (cid, sym))
    actual = p['cantidad'] if p else 0.0
    q = o['cantidad'] * (1 if o['lado'] == 'buy' else -1)
    if o['reduce']:  # una orden de salida nunca abre en sentido contrario
        if not actual or (actual > 0) == (q > 0):
            db.ex("UPDATE ordenes SET estado='cancelada', motivo='sin posición que cerrar' WHERE id=?", (o['id'],))
            return
        if abs(q) >= abs(actual) - EPS:
            q = -actual
    pct = (cfg.get('comision_por_tipo') or {}).get(tipo_activo or '', onchain.COMISION_PCT if tipo_activo == 'ONCHAIN' else cfg['comision_pct'])
    com = cfg['comision_fija'] + abs(q) * px * fx * pct / 100
    if not o['reduce'] and (not actual or (actual > 0) == (q > 0) or abs(q) > abs(actual)):
        if abs(q) * px * fx + com > disponible + 0.001:
            db.ex("UPDATE ordenes SET estado='cancelada', motivo='capital insuficiente al ejecutar' WHERE id=? OR (padre_id=? AND estado='espera')", (o['id'], o['id']))
            return False
    if not o['reduce'] and o['sl'] and (not actual or (actual > 0) == (q > 0)):
        sl = _numero(o['sl'], 'stop-loss')
        if (q > 0 and sl >= px) or (q < 0 and sl <= px) or (cfg['riesgo_estricto'] and abs(q) * abs(px-sl) * fx > max(0, equity) * cfg['riesgo_max_pct'] / 100 + .001):
            db.ex("UPDATE ordenes SET estado='cancelada', motivo='stop o riesgo invalido al ejecutar' WHERE id=? OR (padre_id=? AND estado='espera')", (o['id'], o['id']))
            return False
    from . import guardia
    try:
        guardia.admitir(cid, o['reduce'])
    except ValueError:
        db.ex("UPDATE ordenes SET estado='cancelada', motivo='guardia del enjambre' WHERE id=?", (o['id'],))
        return
    db.ex('UPDATE cuentas SET efectivo=efectivo-? WHERE id=?', (q * px * fx + com, cid))
    pnl = 0.0
    nuevo = actual + q
    if abs(nuevo) < EPS:
        nuevo = 0.0
    if actual == 0 or (actual > 0) == (q > 0):  # abre o amplía
        medio = ((p['precio_medio'] * actual) + px * q) / nuevo if actual else px
        if p:
            db.ex('UPDATE posiciones SET cantidad=?, precio_medio=? WHERE cuenta_id=? AND simbolo=?', (nuevo, medio, cid, sym))
            db.ex('UPDATE trades SET cantidad=cantidad+?, entrada=?, comisiones=comisiones+? WHERE id=?', (abs(q), medio, com, p['trade_id']))
        else:
            sl = o['sl'] if o['sl'] else None
            riesgo = abs(q) * abs(px - sl) * fx if sl else None
            tid = db.ex("""INSERT INTO trades(cuenta_id,simbolo,direccion,estado,abierto,cantidad,entrada,comisiones,stop_inicial,objetivo,riesgo,setup,nota,mfe,mae)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (cid, sym, 'largo' if q > 0 else 'corto', 'abierto', ts, abs(q), px, com, sl, o['tp'], riesgo, o['setup'], o['nota'], 0, 0))
            db.ex('INSERT INTO posiciones(cuenta_id,simbolo,cantidad,precio_medio,moneda,abierta,trade_id) VALUES(?,?,?,?,?,?,?)',
                  (cid, sym, nuevo, px, moneda, ts, tid))
    else:  # reduce, cierra o da la vuelta
        cerrada = min(abs(q), abs(actual))
        pnl = cerrada * (px - p['precio_medio']) * (1 if actual > 0 else -1) * fx - com
        db.ex('UPDATE trades SET salida_valor=salida_valor+?, salida_cant=salida_cant+?, pnl=pnl+?, comisiones=comisiones+? WHERE id=?',
              (cerrada * px, cerrada, pnl + com, com, p['trade_id']))
        if nuevo == 0 or (nuevo > 0) != (actual > 0):
            t = db.uno('SELECT * FROM trades WHERE id=?', (p['trade_id'],))
            neto = t['pnl'] - t['comisiones']
            db.ex("UPDATE trades SET estado='cerrado', cerrado=?, salida=?, pnl=?, r=?, nota_cierre=? WHERE id=?",
                  (ts, t['salida_valor'] / t['salida_cant'], round(neto, 8), round(neto / t['riesgo'], 3) if t['riesgo'] else None,
                   o['nota'] if o['reduce'] else None, t['id']))
            db.ex('DELETE FROM posiciones WHERE cuenta_id=? AND simbolo=?', (cid, sym))
            db.ex("UPDATE ordenes SET estado='cancelada', motivo='posición cerrada' WHERE cuenta_id=? AND simbolo=? AND reduce=1 AND estado IN ('pendiente','espera') AND id<>?",
                  (cid, sym, o['id']))
            avisos.append(f'{"✅" if neto > 0 else "❌"} {sym} cerrado: {neto:+.2f} {c["moneda"]}' + (f' ({neto / t["riesgo"]:+.2f}R)' if t['riesgo'] else ''))
            if nuevo != 0:  # vuelta: abre la parte sobrante en sentido contrario
                tid = db.ex("INSERT INTO trades(cuenta_id,simbolo,direccion,estado,abierto,cantidad,entrada,comisiones,mfe,mae) VALUES(?,?,?,?,?,?,?,?,0,0)",
                            (cid, sym, 'largo' if nuevo > 0 else 'corto', 'abierto', ts, abs(nuevo), px, 0))
                db.ex('INSERT INTO posiciones(cuenta_id,simbolo,cantidad,precio_medio,moneda,abierta,trade_id) VALUES(?,?,?,?,?,?,?)',
                      (cid, sym, nuevo, px, moneda, ts, tid))
        else:
            db.ex('UPDATE posiciones SET cantidad=? WHERE cuenta_id=? AND simbolo=?', (nuevo, cid, sym))
    db.ex("UPDATE ordenes SET estado='ejecutada', ejecutada=?, precio_ejec=?, comision=? WHERE id=?", (ts, px, com, o['id']))
    db.ex('INSERT INTO ejecuciones(cuenta_id,orden_id,simbolo,lado,cantidad,precio,fx,comision,pnl,ts) VALUES(?,?,?,?,?,?,?,?,?,?)',
          (cid, o['id'], sym, o['lado'], abs(q), px, fx, com, round(pnl, 8), ts))
    # bracket: al llenarse la entrada se activan sus hijos; OCO: al llenarse uno, los hermanos se cancelan
    db.ex("UPDATE ordenes SET estado='pendiente', creada=? WHERE padre_id=? AND estado='espera'", (ts, o['id']))
    if o['oco']:
        db.ex("UPDATE ordenes SET estado='cancelada', motivo='OCO: se ejecutó la otra' WHERE oco=? AND id<>? AND estado='pendiente'", (o['oco'], o['id']))
    pos_tras = db.uno('SELECT cantidad FROM posiciones WHERE cuenta_id=? AND simbolo=?', (cid, sym))
    if pos_tras:  # tras ampliar o reducir, los stops/objetivos cubren la posición entera
        db.ex("UPDATE ordenes SET cantidad=? WHERE cuenta_id=? AND simbolo=? AND reduce=1 AND estado='pendiente'", (abs(pos_tras['cantidad']), cid, sym))
    return True


def procesar(cid, simbolos=None):
    with _cerrojo(cid):
        return _procesar(cid, simbolos)


def _procesar(cid, simbolos=None):
    c = _fila(cid)
    slip = c['config']['deslizamiento_pct'] / 100
    pend = db.q("SELECT * FROM ordenes WHERE cuenta_id=? AND estado='pendiente' ORDER BY reduce DESC, id", (cid,))
    n, barras = 0, {}
    for o in pend:
        if simbolos and o['simbolo'] not in simbolos:
            continue
        o = db.uno('SELECT * FROM ordenes WHERE id=?', (o['id'],))
        if o['estado'] != 'pendiente':
            continue
        try:
            b = barras.get(o['simbolo']) or barra(c, o['simbolo'])
        except Exception:
            continue
        barras[o['simbolo']] = b
        if c['tipo'] == 'replay' and b['t'] <= o['creada']:
            continue  # en replay se ejecuta en la vela SIGUIENTE a la orden
        if not b.get('abierto', True):
            continue
        if c['tipo'] != 'replay' and o['tipo'] != 'mercado' and b['t'] < o['creada']:
            # la vela empezó antes que la orden: su máximo/mínimo pueden ser de antes, así que solo cuenta el precio actual
            b = dict(b, o=b['c'], h=b['c'], l=b['c'])
        px = _precio_ejecucion(o, b, slip)
        if px is not None:
            if _llenar(c, o, px, b['t'], b.get('moneda'), b.get('tipo_activo')):
                n += 1
    for p in db.q('SELECT * FROM posiciones WHERE cuenta_id=?', (cid,)):  # MFE / MAE: lo mejor y lo peor que llegó a ir
        try:
            b = barras.get(p['simbolo']) or barra(c, p['simbolo'])
        except Exception:
            continue
        s = 1 if p['cantidad'] > 0 else -1
        a, z = (b['h'] - p['precio_medio']) * s, (b['l'] - p['precio_medio']) * s
        db.ex('UPDATE trades SET mfe=MAX(COALESCE(mfe,0),?), mae=MIN(COALESCE(mae,0),?) WHERE id=?', (max(a, z), min(a, z), p['trade_id']))
    if n:
        instantanea(cid)
    return n

# ---------------- replay ----------------


def replay_avanzar(cid, pasos=1):
    c = _fila(cid)
    if c['tipo'] != 'replay':
        raise ErrorOrden('no es una cuenta de práctica')
    v = _velas_replay(c)
    ejec = 0
    for _ in range(max(1, int(pasos))):
        if c['replay_idx'] >= len(v) - 1:
            break
        db.ex('UPDATE cuentas SET replay_idx=replay_idx+1 WHERE id=?', (cid,))
        c = _fila(cid)
        ejec += procesar(cid)
        b = v[c['replay_idx']]
        instantanea(cid, b['t'])
    return {'ejecuciones': ejec, **replay_velas(cid)}


def replay_velas(cid):
    c = _fila(cid)
    v = _velas_replay(c)
    return {'simbolo': c['replay_simbolo'], 'intervalo': c['replay_intervalo'], 'idx': c['replay_idx'], 'total': len(v),
            'velas': v[:c['replay_idx'] + 1], 'fin': c['replay_idx'] >= len(v) - 1}

# ---------------- diario y estadísticas ----------------


def diario(cid, estado=None, limite=500):
    sql = 'SELECT * FROM trades WHERE cuenta_id=?' + (' AND estado=?' if estado else '') + ' ORDER BY id DESC LIMIT ?'
    return db.q(sql, (cid, estado, limite) if estado else (cid, limite))


def anotar(cid, tid, campos):
    perm = {k: v for k, v in campos.items() if k in ('setup', 'nota', 'emocion', 'etiquetas', 'leccion', 'nota_cierre', 'respeto_plan')}
    if not perm:
        raise ErrorOrden('nada que anotar (setup, nota, emocion, etiquetas, leccion, respeto_plan)')
    if isinstance(perm.get('etiquetas'), list):
        perm['etiquetas'] = ','.join(perm['etiquetas'])
    if not db.uno('SELECT 1 x FROM trades WHERE id=? AND cuenta_id=?', (tid, cid)):
        raise ErrorOrden(f'no existe el trade #{tid} en esta cuenta (mira el diario)')
    sets = ', '.join(f'{k}=?' for k in perm)
    db.ex(f'UPDATE trades SET {sets} WHERE id=? AND cuenta_id=?', (*perm.values(), tid, cid))
    return db.uno('SELECT * FROM trades WHERE id=?', (tid,))


def curva_equity(cid, puntos=500):
    eq = db.q('SELECT ts,valor FROM equity WHERE cuenta_id=? ORDER BY ts, rowid', (cid,))
    paso = max(1, len(eq) // puntos)
    out = eq[::paso]
    if eq and out[-1] is not eq[-1]:
        out.append(eq[-1])
    return out


def estadisticas(cid):
    c = _fila(cid)
    tr = [t for t in diario(cid, 'cerrado', 100000)][::-1]
    eq = [e['valor'] for e in db.q('SELECT valor FROM equity WHERE cuenta_id=? ORDER BY ts, rowid', (cid,))]
    rets, dd = metricas.curva(eq or [c['capital_inicial']])
    base = metricas.de_trades([t['pnl'] for t in tr], [t['r'] for t in tr])
    por_setup, por_simbolo, por_dia = {}, {}, {}
    for t in tr:
        for clave, d in ((t['setup'] or 'sin setup', por_setup), (t['simbolo'], por_simbolo),
                         (('lun', 'mar', 'mié', 'jue', 'vie', 'sáb', 'dom')[time.gmtime(t['abierto']).tm_wday], por_dia)):
            x = d.setdefault(clave, {'trades': 0, 'pnl': 0.0, 'ganadores': 0})
            x['trades'] += 1
            x['pnl'] = round(x['pnl'] + t['pnl'], 2)
            x['ganadores'] += t['pnl'] > 0
    dur = [t['cerrado'] - t['abierto'] for t in tr if t['cerrado']]
    con_stop = sum(1 for t in tr if t['stop_inicial'])
    anotados = sum(1 for t in tr if t['setup'] and (t['nota'] or t['leccion'] or t['emocion']))
    return {**base, 'max_drawdown_pct': round(dd, 2), 'montecarlo': metricas.montecarlo([t['pnl'] for t in tr], c['capital_inicial']),
            'duracion_media_h': round(sum(dur) / len(dur) / 3600, 1) if dur else None,
            'con_stop_pct': round(con_stop / len(tr) * 100, 1) if tr else None,
            'anotados_pct': round(anotados / len(tr) * 100, 1) if tr else None,
            'por_setup': por_setup, 'por_simbolo': por_simbolo, 'por_dia': por_dia}


def examen(cid):
    """¿Listo para dinero real? Criterios de disciplina y ventaja estadística, no de suerte."""
    c = _fila(cid)
    e = estadisticas(cid)
    tr = diario(cid, 'cerrado', 100000)
    dias = (ahora() - c['creada']) / 86400 if c['tipo'] != 'replay' else None
    crit = [
        ('Al menos 50 operaciones cerradas', e.get('trades', 0) >= 50, f"{e.get('trades', 0)}/50"),
        ('Esperanza positiva (ganas de media por trade)', (e.get('esperanza') or 0) > 0, str(e.get('esperanza'))),
        ('Factor de beneficio ≥ 1,3', (e.get('factor_beneficio') or 0) >= 1.3, str(e.get('factor_beneficio'))),
        ('Drawdown máximo ≤ 15 %', e.get('max_drawdown_pct', 100) <= 15, f"{e.get('max_drawdown_pct')} %"),
        ('Stop-loss en ≥ 90 % de las entradas', (e.get('con_stop_pct') or 0) >= 90, f"{e.get('con_stop_pct')} %"),
        ('Diario rellenado en ≥ 80 % (setup + nota)', (e.get('anotados_pct') or 0) >= 80, f"{e.get('anotados_pct')} %"),
        ('Ninguna pérdida mayor que 2R', all((t['r'] or 0) >= -2.05 for t in tr), 'ok' if all((t['r'] or 0) >= -2.05 for t in tr) else 'hubo pérdidas > 2R'),
    ]
    if dias is not None:
        crit.append(('Al menos 60 días practicando', dias >= 60, f'{dias:.0f}/60 días'))
    ok = sum(1 for _, s, _ in crit if s)
    return {'aprobado': ok == len(crit), 'cumplidos': ok, 'total': len(crit),
            'criterios': [{'criterio': a, 'ok': s, 'valor': v} for a, s, v in crit],
            'nota': 'Aprobar no garantiza ganar en real: el dinero real añade miedo y codicia. Empieza con poco.'}

# ---------------- avisos ----------------


def avisar(c, texto):
    db.ex('INSERT INTO alertas(simbolo,condicion,valor,nota,activa,creada,disparada,vista) VALUES(?,?,?,?,0,?,?,0)',
          ('', 'aviso', None, texto, ahora(), ahora()))
    url = (c.get('config') or {}).get('webhook') or db.ajuste('webhook', '')
    if url:
        _webhook(url, texto)


def _webhook(url, texto):
    import threading, urllib.request

    def ir():
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=json.dumps({'content': texto, 'text': texto}).encode(),
                                                          headers={'content-type': 'application/json', 'user-agent': 'DMN-Trader'}), timeout=8)
        except Exception:
            pass
    threading.Thread(target=ir, daemon=True).start()
