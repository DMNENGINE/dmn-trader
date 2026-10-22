"""Agente autónomo de APOLO: el «reto» (p. ej. 1 $ → 100 $) con microoperaciones simuladas en velas de 1 minuto.
Ciclo: opera con su «genoma» (estrategia + parámetros + filtros) → cada operación perdida pasa una AUTOPSIA (¿por qué?)
→ cada causa trae un ARREGLO concreto → si quiebra, busca la solución: genera candidatos (arreglos, mutaciones, el mejor
histórico) y los PRUEBA en datos recientes reales antes de elegir con cuál empieza el siguiente intento.
Todo queda escrito (lecciones, intentos, genomas) para que APOLO lo cuente y lo use."""
import copy, json, math, random, statistics, threading, time, traceback
from . import datos, db, indicadores as ind, motor

ESQUEMA = """
CREATE TABLE IF NOT EXISTS ag_intentos (id INTEGER PRIMARY KEY, inicio INTEGER, fin INTEGER, capital REAL, meta REAL, max_equity REAL,
  final REAL, trades INTEGER DEFAULT 0, ganados INTEGER DEFAULT 0, resultado TEXT, causa TEXT, genoma TEXT);
CREATE TABLE IF NOT EXISTS ag_trades (trade_id INTEGER PRIMARY KEY, intento INTEGER, simbolo TEXT, direccion TEXT, estrategia TEXT,
  genoma TEXT, rasgos TEXT, abierto INTEGER, revisado INTEGER DEFAULT 0, r REAL, causa TEXT);
CREATE TABLE IF NOT EXISTS ag_lecciones (id INTEGER PRIMARY KEY, ts INTEGER, intento INTEGER, trade_id INTEGER, tipo TEXT, texto TEXT, cambio TEXT);
"""
CRIPTO = ['BTC-USD', 'ETH-USD', 'SOL-USD', 'XRP-USD', 'DOGE-USD', 'AVAX-USD', 'LINK-USD', 'ADA-USD']
ACCIONES = ['SPY', 'QQQ', 'TSLA', 'NVDA', 'AAPL', 'AMD', 'META', 'AMZN']
MARCOS = {'1m': 1, '5m': 5, '15m': 15}
CUENTA_CFG = {'comision_por_tipo': {'CRYPTOCURRENCY': 0.1, 'EQUITY': 0.0, 'ETF': 0.0}, 'comision_pct': 0.1, 'comision_fija': 0.0, 'deslizamiento_pct': 0.01, 'riesgo_max_pct': 100, 'riesgo_estricto': False,
              'perdida_diaria_max_pct': 0, 'max_posiciones': 3, 'apalancamiento': 1.0, 'exigir_stop': True, 'permitir_cortos': True}
ESTRATEGIAS = {
    'ema_cruce': {'r': 9, 'l': 21}, 'rsi_rev': {'n': 7, 'bajo': 25, 'alto': 75}, 'ruptura': {'n': 20},
    'bollinger': {'n': 20, 'k': 2.0}, 'impulso': {'n': 5, 'umbral': 0.25},
}
GENOMA_BASE = {'marco': '1m', 'estrategia': 'ema_cruce', 'p': dict(ESTRATEGIAS['ema_cruce']), 'stop_atr': 1.5, 'rr': 1.5, 'filtro_tendencia': False,
               'vol_min_pct': 0.0, 'vol_max_pct': 3.0, 'riesgo_pct': 20.0, 'max_barras': 30, 'be_en_r': 0.0, 'min_ventaja': 2.0,
               'cortos': True, 'horas_bloqueadas': [], 'simbolos_bloqueados': []}
LOG = []
_hilo = None
_parar = threading.Event()


def log(t):
    LOG.append(f"{time.strftime('%H:%M:%S')} {t}")
    del LOG[:-200]


def preparar():
    db.con().executescript(ESQUEMA)


def estado():
    return db.ajuste('agente', None)


def guardar(e):
    db.poner_ajuste('agente', e)

# ---------------- señales (compartidas por el agente en vivo y por el simulador de soluciones) ----------------


def senales(g, v):
    """Lista de señales (1, -1, 0) por vela cerrada según el genoma, con filtros aplicados. También devuelve ATR y tendencia."""
    c, h, l = [b['c'] for b in v], [b['h'] for b in v], [b['l'] for b in v]
    p, e = g['p'], g['estrategia']
    n = len(c)
    s = [0] * n
    if e == 'ema_cruce':
        a, b = ind.ema(c, int(p['r'])), ind.ema(c, int(p['l']))
        for i in range(1, n):
            if None not in (a[i], b[i], a[i - 1], b[i - 1]):
                s[i] = 1 if a[i - 1] <= b[i - 1] and a[i] > b[i] else -1 if a[i - 1] >= b[i - 1] and a[i] < b[i] else 0
    elif e == 'rsi_rev':
        r = ind.rsi(c, int(p['n']))
        for i in range(1, n):
            if r[i] is not None and r[i - 1] is not None:
                s[i] = 1 if r[i - 1] < p['bajo'] <= r[i] else -1 if r[i - 1] > p['alto'] >= r[i] else 0
    elif e == 'ruptura':
        sup, inf = ind.donchian(h, l, int(p['n']))
        for i in range(n):
            if sup[i] is not None:
                s[i] = 1 if c[i] > sup[i] else -1 if c[i] < inf[i] else 0
    elif e == 'bollinger':
        m, bs, bi = ind.bollinger(c, int(p['n']), float(p['k']))
        for i in range(1, n):
            if bi[i] is not None and bi[i - 1] is not None:
                s[i] = 1 if c[i - 1] < bi[i - 1] and c[i] >= bi[i] else -1 if c[i - 1] > bs[i - 1] and c[i] <= bs[i] else 0
    elif e == 'impulso':
        k = int(p['n'])
        for i in range(k, n):
            ret = (c[i] / c[i - k] - 1) * 100
            s[i] = 1 if ret > p['umbral'] and c[i] > c[i - 1] else -1 if ret < -p['umbral'] and c[i] < c[i - 1] else 0
    atr = ind.atr(h, l, c, 14)
    e50 = ind.ema(c, 50)
    tend = [0] * n
    for i in range(5, n):
        if e50[i] is not None and e50[i - 5] is not None:
            tend[i] = 1 if c[i] > e50[i] and e50[i] > e50[i - 5] else -1 if c[i] < e50[i] and e50[i] < e50[i - 5] else 0
    for i in range(n):
        if s[i] and atr[i]:
            ap = atr[i] / c[i] * 100
            if not g['vol_min_pct'] <= ap <= g['vol_max_pct']:
                s[i] = 0
            elif g['filtro_tendencia'] and tend[i] != s[i]:
                s[i] = 0
            elif s[i] < 0 and not g['cortos']:
                s[i] = 0
            elif time.gmtime(v[i]['t']).tm_hour in g['horas_bloqueadas']:
                s[i] = 0
        elif s[i]:
            s[i] = 0
    return s, atr, tend


def coste_pct(cfg, spread_pct=0.0, tipo=None):
    com = (cfg.get('comision_por_tipo') or {}).get(tipo or '', cfg['comision_pct'])
    return 2 * (com + cfg['deslizamiento_pct']) + spread_pct


def simular(g, series, coste):
    """Prueba un genoma en velas reales recientes: entra en la apertura siguiente a la señal, stop/objetivo dentro de la vela
    (si tocan los dos, cuenta el stop), salida por tiempo y breakeven. Devuelve la lista de resultados en R (netos de costes)."""
    rs = []
    for v in series:
        s, atr, _ = senales(g, v)
        i = 1
        while i < len(v) - 1:
            if not s[i - 1] or not atr[i - 1]:
                i += 1
                continue
            d = s[i - 1]
            ent = v[i]['o']
            dist = atr[i - 1] * g['stop_atr']
            if g['rr'] * dist / ent * 100 < g['min_ventaja'] * coste:
                i += 1
                continue
            stop, obj, be_hecho = ent - d * dist, ent + d * g['rr'] * dist, False
            salida, j = None, i
            while j < len(v) and j - i < g['max_barras']:
                b = v[j]
                if (d > 0 and b['l'] <= stop) or (d < 0 and b['h'] >= stop):
                    salida = stop
                    break
                if (d > 0 and b['h'] >= obj) or (d < 0 and b['l'] <= obj):
                    salida = obj
                    break
                if g['be_en_r'] and not be_hecho and (b['h'] - ent if d > 0 else ent - b['l']) >= g['be_en_r'] * dist:
                    stop, be_hecho = ent, True
                j += 1
            if salida is None:
                j = min(j, len(v) - 1)
                salida = v[j]['c']
            r = (salida - ent) * d / dist - coste / 100 * ent / dist
            rs.append(r)
            i = j + 1
    return rs


def puntuar(rs):
    """Esperanza en R penalizada si hay pocos trades (para no premiar la suerte)."""
    if len(rs) < 5:
        return -9.0
    m = statistics.fmean(rs)
    return m - 0.5 * (statistics.pstdev(rs) / math.sqrt(len(rs)))

# ---------------- arranque / parada ----------------


def iniciar(capital=1.0, meta=100.0, mercados=('cripto', 'acciones'), hasta=None, simbolos=None):
    preparar()
    e = estado() or {}
    if e.get('activo'):
        raise motor.ErrorOrden('el agente ya está en marcha')
    cid = e.get('cuenta_id')
    if not cid or not db.uno('SELECT 1 x FROM cuentas WHERE id=?', (cid,)):
        cid = motor.crear_cuenta('APOLO · Reto', capital, 'USD', CUENTA_CFG)['id']
    motor.configurar(cid, CUENTA_CFG)
    g = e.get('genoma') or mejor_genoma() or copy.deepcopy(GENOMA_BASE)
    e = {'activo': True, 'cuenta_id': cid, 'capital': float(capital), 'meta': float(meta), 'mercados': list(mercados),
         'simbolos': simbolos or {'cripto': CRIPTO, 'acciones': ACCIONES}, 'hasta': hasta, 'genoma': g,
         'bandido': e.get('bandido') or {k: [1, 1] for k in ESTRATEGIAS}, 'sesion_inicio': int(time.time()),
         'metas_logradas': e.get('metas_logradas', 0), 'descartes_coste': 0}
    guardar(e)
    _nuevo_intento(e, g, 'inicio')
    arrancar_hilo()
    return resumen()


def parar(motivo='parado por el usuario'):
    e = estado()
    if not e:
        return None
    e['activo'] = False
    guardar(e)
    for p in motor.posiciones(e['cuenta_id'], False):
        try:
            motor.cerrar(e['cuenta_id'], p['simbolo'], 100, 'agente parado')
        except Exception:
            pass
    it = db.uno('SELECT * FROM ag_intentos WHERE fin IS NULL ORDER BY id DESC LIMIT 1')
    if it:
        eq = motor.resumen(e['cuenta_id'])['equity']
        db.ex('UPDATE ag_intentos SET fin=?, final=?, resultado=? WHERE id=?', (int(time.time()), eq, 'parado', it['id']))
    log('⏹ ' + motivo)
    return resumen()


def arrancar_hilo():
    global _hilo
    if _hilo and _hilo.is_alive():
        return
    _parar.clear()
    _hilo = threading.Thread(target=_bucle, daemon=True, name='agente')
    _hilo.start()


def _bucle():
    while not _parar.is_set():
        try:
            e = estado()
            if not e or not e.get('activo'):
                return
            if e.get('hasta') and time.time() >= e['hasta']:
                parar('hora de fin alcanzada')
                return
            paso(e)
        except Exception:
            log('error: ' + traceback.format_exc().splitlines()[-1])
            traceback.print_exc()
        _parar.wait(20)

# ---------------- un paso del agente ----------------


def _intento_actual():
    return db.uno('SELECT * FROM ag_intentos WHERE fin IS NULL ORDER BY id DESC LIMIT 1')


def _nuevo_intento(e, g, motivo):
    cid = e['cuenta_id']
    for p in motor.posiciones(cid, False):
        try:
            motor.cerrar(cid, p['simbolo'], 100, 'nuevo intento')
        except Exception:
            pass
    db.ex("UPDATE ordenes SET estado='cancelada', motivo='nuevo intento' WHERE cuenta_id=? AND estado IN ('pendiente','espera')", (cid,))
    db.ex('UPDATE cuentas SET efectivo=?, capital_inicial=? WHERE id=?', (e['capital'], e['capital'], cid))
    iid = db.ex('INSERT INTO ag_intentos(inicio,capital,meta,max_equity,genoma) VALUES(?,?,?,?,?)',
                (int(time.time()), e['capital'], e['meta'], e['capital'], json.dumps(g)))
    motor.instantanea(cid)
    e['genoma'] = g
    guardar(e)
    log(f"▶ intento #{iid} ({motivo}) con {g['estrategia']} {g['p']} stop {g['stop_atr']}ATR rr {g['rr']}")
    return iid


def _universo(e):
    sims = []
    if 'cripto' in e['mercados']:
        sims += e['simbolos'].get('cripto', CRIPTO)
    if 'acciones' in e['mercados']:
        sims += e['simbolos'].get('acciones', ACCIONES)
    return [s for s in sims if s not in e['genoma'].get('simbolos_bloqueados', [])]


def paso(e):
    cid, g = e['cuenta_id'], e['genoma']
    it = _intento_actual()
    if not it:
        it = db.uno('SELECT * FROM ag_intentos WHERE id=?', (_nuevo_intento(e, g, 'sin intento abierto'),))
    motor.procesar(cid)
    r = motor.resumen(cid)
    eq = r['equity']
    if eq > it['max_equity']:
        db.ex('UPDATE ag_intentos SET max_equity=? WHERE id=?', (eq, it['id']))
    _registrar_cerrados(e, it)
    autopsias(e)
    # ¿meta o quiebra?
    if eq >= e['meta']:
        db.ex('UPDATE ag_intentos SET fin=?, final=?, resultado=?, causa=? WHERE id=?', (int(time.time()), eq, 'meta', '¡meta alcanzada!', it['id']))
        e['metas_logradas'] = e.get('metas_logradas', 0) + 1
        motor.avisar(motor._fila(cid), f"🏆 APOLO convirtió {e['capital']} $ en {eq:.2f} $ (intento #{it['id']}). Vuelve a empezar para ver si lo repite.")
        _nuevo_intento(e, g, 'meta alcanzada: comprobar que no fue suerte')
        return
    if eq < e['capital'] * 0.1:
        quiebra(e, it, eq)
        return
    _gestionar(e, r)
    pos = motor.posiciones(cid, False)
    if not pos and e.get('descartes_coste', 0) - e.get('descartes_ref', 0) > 300:
        # mucho tiempo sin encontrar operaciones que paguen los costes: buscar otra forma de operar sin esperar a quebrar
        e['descartes_ref'] = e['descartes_coste']
        nuevo, inf = buscar_solucion(e)
        e['genoma'] = nuevo
        guardar(e)
        _leccion(e, it['id'], None, 'inactivo', f"Llevo mucho rato sin operaciones que cubran comisión + spread con mi forma de operar. {inf}", {'genoma': 'cambiado'})
        return
    if len(pos) >= CUENTA_CFG['max_posiciones']:
        return
    abiertos = {p['simbolo'] for p in pos}
    pendientes = {o['simbolo'] for o in motor.ordenes(cid, 'pendiente') if not o['reduce']}
    hora = time.gmtime().tm_hour
    if hora in g['horas_bloqueadas']:
        return
    for sim in _universo(e):
        if sim in abiertos or sim in pendientes:
            continue
        try:
            q = datos.cotizacion(sim)
            if not q['abierto']:
                continue
            v = datos.velas(sim, g.get('marco', '1m'))[:-1]  # solo velas cerradas
        except Exception:
            continue
        if len(v) < 60:
            continue
        s, atr, tend = senales(g, v)
        if not s[-1] or not atr[-1]:
            continue
        d, p = s[-1], q['precio']
        spread = ((q['ask'] - q['bid']) / p * 100) if q.get('ask') and q.get('bid') else 0.0
        coste = coste_pct(motor._fila(cid)['config'], spread, q['tipo'])
        dist = atr[-1] * g['stop_atr']
        if g['rr'] * dist / p * 100 < g['min_ventaja'] * coste:
            e['descartes_coste'] = e.get('descartes_coste', 0) + 1
            guardar(e)
            continue
        equity = motor.resumen(cid)['equity']
        efectivo = motor._fila(cid)['efectivo']
        libres = max(1, CUENTA_CFG['max_posiciones'] - len(motor.posiciones(cid, False)))
        qty = min(equity * g['riesgo_pct'] / 100 / dist, min(efectivo, equity) * 0.97 / libres / p)
        if qty * p < 0.0001:
            continue
        sl, tp = p - d * dist, p + d * g['rr'] * dist
        rasgos = {'atr_pct': round(atr[-1] / p * 100, 4), 'tendencia': tend[-1], 'hora': hora, 'spread_pct': round(spread, 4),
                  'coste_pct': round(coste, 4), 'precio': p, 'dist': dist}
        try:
            o = motor.enviar(cid, sim, 'buy' if d > 0 else 'sell', qty, 'mercado', sl=sl, tp=tp, setup=g['estrategia'],
                             nota=f"{g['estrategia']} {'largo' if d > 0 else 'corto'} · ATR {rasgos['atr_pct']}% · tendencia {tend[-1]:+d}", origen='agente')
        except motor.ErrorOrden as err:
            log(f'orden rechazada {sim}: {err}')
            continue
        if o['estado'] == 'ejecutada':
            pz = db.uno('SELECT trade_id FROM posiciones WHERE cuenta_id=? AND simbolo=?', (cid, sim))
            if pz:
                db.ex('INSERT OR IGNORE INTO ag_trades(trade_id,intento,simbolo,direccion,estrategia,genoma,rasgos,abierto) VALUES(?,?,?,?,?,?,?,?)',
                      (pz['trade_id'], it['id'], sim, 'largo' if d > 0 else 'corto', g['estrategia'], json.dumps(g), json.dumps(rasgos), int(time.time())))
                log(f"{'🟢' if d > 0 else '🔴'} {sim} {'largo' if d > 0 else 'corto'} {qty:.6g} @ {o['precio_ejec']:.6g} (SL {sl:.6g} TP {tp:.6g})")
        if len(motor.posiciones(cid, False)) >= CUENTA_CFG['max_posiciones']:
            break


def _gestionar(e, r):
    """Salida por tiempo y stop a breakeven."""
    cid, g = e['cuenta_id'], e['genoma']
    for p in motor.posiciones(cid):
        t = db.uno('SELECT * FROM trades WHERE id=?', (p['trade_id'],))
        if not t:
            continue
        if (time.time() - t['abierto']) / 60 >= g['max_barras'] * MARCOS.get(g.get('marco', '1m'), 1):
            try:
                motor.cerrar(cid, p['simbolo'], 100, 'tiempo')
                log(f"⏱ {p['simbolo']} cerrado por tiempo")
            except motor.ErrorOrden:
                pass
            continue
        if g['be_en_r'] and t['stop_inicial'] and p['stop'] is not None:
            dist = abs(t['entrada'] - t['stop_inicial'])
            fav = (p['precio'] - t['entrada']) * (1 if p['cantidad'] > 0 else -1)
            if dist and fav >= g['be_en_r'] * dist and abs(p['stop'] - t['entrada']) > dist * 0.05:
                motor.mover_stop(cid, p['simbolo'], stop=t['entrada'])
                log(f"🛡 {p['simbolo']} stop a breakeven")


def _registrar_cerrados(e, it):
    for t in db.q("""SELECT t.id, t.r, t.pnl FROM trades t JOIN ag_trades a ON a.trade_id=t.id
                     WHERE t.estado='cerrado' AND a.r IS NULL AND t.cuenta_id=?""", (e['cuenta_id'],)):
        r = t['r'] if t['r'] is not None else 0.0
        db.ex('UPDATE ag_trades SET r=? WHERE trade_id=?', (r, t['id']))
        db.ex('UPDATE ag_intentos SET trades=trades+1, ganados=ganados+? WHERE id=?', (1 if t['pnl'] > 0 else 0, it['id']))
        est = db.uno('SELECT estrategia FROM ag_trades WHERE trade_id=?', (t['id'],))['estrategia']
        b = e['bandido'].setdefault(est, [1, 1])
        b[0 if t['pnl'] > 0 else 1] += 1
        log(f"{'✅' if t['pnl'] > 0 else '❌'} trade #{t['id']} {r:+.2f}R ({t['pnl']:+.4f} $)")
    guardar(e)

# ---------------- autopsia: por qué se perdió y qué cambiar ----------------


def _leccion(e, intento, tid, tipo, texto, cambio):
    db.ex('INSERT INTO ag_lecciones(ts,intento,trade_id,tipo,texto,cambio) VALUES(?,?,?,?,?,?)',
          (int(time.time()), intento, tid, tipo, texto, json.dumps(cambio, ensure_ascii=False)))
    log('🧠 ' + texto)


def _aplicar(g, cambio):
    for k, v in cambio.items():
        g[k] = v
    return g


def autopsias(e):
    """Revisa los trades cerrados hace ≥ 15 min (para ver qué hizo el precio DESPUÉS de la salida)."""
    g = e['genoma']
    pend = db.q("""SELECT a.*, t.entrada, t.salida, t.stop_inicial, t.objetivo, t.mfe, t.mae, t.pnl, t.comisiones, t.cerrado, t.nota_cierre, t.cantidad
                   FROM ag_trades a JOIN trades t ON t.id=a.trade_id WHERE a.revisado=0 AND t.estado='cerrado' AND t.cerrado < ?""", (int(time.time()) - 900,))
    for a in pend:
        db.ex('UPDATE ag_trades SET revisado=1 WHERE trade_id=?', (a['trade_id'],))
        if a['pnl'] > 0:
            continue
        ras = json.loads(a['rasgos'])
        gt = json.loads(a['genoma'])
        d = 1 if a['direccion'] == 'largo' else -1
        dist = abs(a['entrada'] - (a['stop_inicial'] or a['entrada'])) or ras['dist']
        bruto = (a['salida'] - a['entrada']) * d * a['cantidad']
        causas = []
        # 1) los costes se comieron la operación
        if a['comisiones'] > 0 and (bruto > -a['comisiones'] * 0.5 or ras['coste_pct'] > 0.35 * dist / a['entrada'] * 100):
            nv = round(min(g['min_ventaja'] + 0.5, 6), 2)
            sig = {'1m': '5m', '5m': '15m'}.get(g.get('marco', '1m'))
            if g['min_ventaja'] >= 3 and sig:
                causas.append(('costes', f"{a['simbolo']}: otra vez los costes ({ras['coste_pct']:.3f} %) se comen la ganancia en velas de {g.get('marco', '1m')}. "
                               f"Solución: subir a velas de {sig}, donde cada movimiento es más grande que la comisión.", {'marco': sig, 'min_ventaja': 2.0}))
            else:
                causas.append(('costes', f"{a['simbolo']}: la comisión y el spread ({ras['coste_pct']:.3f} %) se comieron la operación; el movimiento esperado era demasiado pequeño. "
                           f"Solución: exigir que el objetivo sea {nv}× los costes (antes {g['min_ventaja']}×).", {'min_ventaja': nv}))
        # 2) el precio tocó el stop y luego fue al objetivo → stop dentro del ruido
        despues = []
        try:
            despues = [b for b in datos.velas(a['simbolo'], gt.get('marco', '1m')) if b['t'] > a['cerrado']][:30]
        except Exception:
            pass
        salio_stop = a['stop_inicial'] and abs(a['salida'] - a['stop_inicial']) <= dist * 0.3
        if salio_stop and a['objetivo'] and despues and any((b['h'] >= a['objetivo']) if d > 0 else (b['l'] <= a['objetivo']) for b in despues):
            nv = round(min(g['stop_atr'] * 1.2, 4.0), 2)
            causas.append(('stop_ajustado', f"{a['simbolo']}: el stop saltó y DESPUÉS el precio llegó al objetivo: el stop estaba dentro del ruido normal. "
                           f"Solución: stop más holgado, de {g['stop_atr']} a {nv} ATR (con menos tamaño para arriesgar lo mismo).", {'stop_atr': nv}))
        # 3) fue ganando ≥1R y acabó perdiendo
        if a['mfe'] and a['mfe'] >= dist * 0.9:
            if not g['be_en_r']:
                causas.append(('devolvio', f"{a['simbolo']}: llegó a ir ganando {a['mfe'] / dist:.1f}R y terminó en pérdida. Solución: mover el stop a breakeven al llegar a +1R.", {'be_en_r': 1.0}))
            else:
                nv = round(max(g['rr'] * 0.85, 1.0), 2)
                causas.append(('devolvio', f"{a['simbolo']}: devolvió la ganancia aun con breakeven; el objetivo está lejos. Solución: objetivo de {g['rr']}R a {nv}R.", {'rr': nv}))
        # 4) contra la tendencia
        if ras['tendencia'] == -d:
            if not g['filtro_tendencia']:
                causas.append(('contra_tendencia', f"{a['simbolo']}: entró {'largo' if d > 0 else 'corto'} contra la tendencia de la EMA50. Solución: solo operar a favor de la tendencia.", {'filtro_tendencia': True}))
        # 5) volatilidad fuera de lo normal
        ganadores = [json.loads(x['rasgos'])['atr_pct'] for x in db.q('SELECT rasgos FROM ag_trades WHERE r > 0 ORDER BY trade_id DESC LIMIT 40')]
        if len(ganadores) >= 5:
            med = statistics.median(ganadores)
            if ras['atr_pct'] > med * 2.2:
                nv = round(max(med * 2, 0.02), 4)
                causas.append(('volatilidad', f"{a['simbolo']}: entró con volatilidad extrema (ATR {ras['atr_pct']} % vs {med:.3f} % habitual en las ganadoras). Solución: no operar por encima de {nv} %.", {'vol_max_pct': nv}))
            elif ras['atr_pct'] < med * 0.4:
                nv = round(ras['atr_pct'] * 1.1, 4)
                causas.append(('sin_movimiento', f"{a['simbolo']}: mercado demasiado quieto (ATR {ras['atr_pct']} %), no hay recorrido. Solución: volatilidad mínima {nv} %.", {'vol_min_pct': nv}))
        # 6) salida por tiempo
        if (a['nota_cierre'] or '') == 'tiempo' and not causas:
            nv = max(int(g['max_barras'] * 0.75), 5)
            causas.append(('sin_continuidad', f"{a['simbolo']}: la señal no tuvo continuidad y salió por tiempo. Solución: salir antes ({nv} min) si no arranca.", {'max_barras': nv}))
        if not causas:
            causas.append(('ruido', f"{a['simbolo']}: pérdida normal sin causa clara (la estrategia {gt['estrategia']} falla a veces). No cambio nada por un solo trade.", {}))
        db.ex('UPDATE ag_trades SET causa=? WHERE trade_id=?', (causas[0][0], a['trade_id']))
        for tipo, texto, cambio in causas[:2]:
            _leccion(e, a['intento'], a['trade_id'], tipo, texto, cambio)
            if cambio:
                e['genoma'] = _aplicar(e['genoma'], cambio)
        _patrones(e)
        guardar(e)


def _patrones(e):
    """Patrones con suficientes datos: horas y símbolos que pierden de forma sistemática → se bloquean."""
    g = e['genoma']
    filas = db.q('SELECT simbolo, rasgos, r FROM ag_trades WHERE r IS NOT NULL ORDER BY trade_id DESC LIMIT 400')
    por_sim, por_hora = {}, {}
    for f in filas:
        por_sim.setdefault(f['simbolo'], []).append(f['r'])
        por_hora.setdefault(json.loads(f['rasgos'])['hora'], []).append(f['r'])
    for s, rs in por_sim.items():
        if len(rs) >= 10 and statistics.fmean(rs) < -0.3 and s not in g['simbolos_bloqueados']:
            g['simbolos_bloqueados'].append(s)
            _leccion(e, None, None, 'simbolo', f"{s} pierde de forma sistemática ({len(rs)} trades, {statistics.fmean(rs):+.2f}R de media). Lo dejo de operar.", {'simbolos_bloqueados': g['simbolos_bloqueados']})
    for h, rs in por_hora.items():
        if len(rs) >= 10 and statistics.fmean(rs) < -0.3 and h not in g['horas_bloqueadas']:
            g['horas_bloqueadas'].append(h)
            _leccion(e, None, None, 'hora', f"A las {h}:00 UTC pierdo de forma sistemática ({len(rs)} trades, {statistics.fmean(rs):+.2f}R). Dejo de operar a esa hora.", {'horas_bloqueadas': g['horas_bloqueadas']})

# ---------------- quiebra: buscar la solución ----------------


def _mutar(g, rnd, fuerte=False):
    g = copy.deepcopy(g)
    f = 0.35 if fuerte else 0.15
    for k in g['p']:
        if isinstance(g['p'][k], (int, float)):
            nv = g['p'][k] * rnd.uniform(1 - f, 1 + f)
            g['p'][k] = max(2, round(nv)) if isinstance(ESTRATEGIAS[g['estrategia']][k], int) else round(nv, 3)
    if g['estrategia'] == 'ema_cruce' and g['p']['r'] >= g['p']['l']:
        g['p']['l'] = g['p']['r'] + 3
    g['stop_atr'] = round(min(max(g['stop_atr'] * rnd.uniform(1 - f, 1 + f), 0.6), 4), 2)
    g['rr'] = round(min(max(g['rr'] * rnd.uniform(1 - f, 1 + f), 0.8), 5), 2)
    g['max_barras'] = int(min(max(g['max_barras'] * rnd.uniform(1 - f, 1 + f), 5), 120))
    if rnd.random() < 0.2:
        g['filtro_tendencia'] = not g['filtro_tendencia']
    if rnd.random() < (0.4 if fuerte else 0.15):
        g['marco'] = rnd.choice(list(MARCOS))
    return g


def mejor_genoma():
    """El genoma con mejor esperanza real (≥ 10 trades) de todos los intentos."""
    mejor, puntos = None, -9
    for it in db.q('SELECT genoma FROM ag_intentos WHERE genoma IS NOT NULL') if db.uno("SELECT name FROM sqlite_master WHERE name='ag_trades'") else []:
        rs = [x['r'] for x in db.q('SELECT r FROM ag_trades WHERE genoma=? AND r IS NOT NULL', (it['genoma'],))]
        p = puntuar(rs) if len(rs) >= 10 else -9
        if p > puntos:
            mejor, puntos = json.loads(it['genoma']), p
    return mejor


def quiebra(e, it, eq):
    cid = e['cuenta_id']
    causas = db.q('SELECT tipo, COUNT(*) n FROM ag_lecciones WHERE intento=? GROUP BY tipo ORDER BY n DESC', (it['id'],))
    causa = ', '.join(f"{c['tipo']} ×{c['n']}" for c in causas[:3]) or 'sin autopsias aún'
    db.ex('UPDATE ag_intentos SET fin=?, final=?, resultado=?, causa=? WHERE id=?', (int(time.time()), eq, 'quiebra', causa, it['id']))
    log(f"💀 intento #{it['id']} quebró ({eq:.4f} $, máx {it['max_equity']:.4f} $). Causas: {causa}")
    nuevo, informe = buscar_solucion(e)
    _leccion(e, it['id'], None, 'quiebra', f"Intento #{it['id']} perdió todo (llegó a {it['max_equity']:.3f} $). Causas principales: {causa}. {informe}", {})
    motor.avisar(motor._fila(cid), f"💀 Intento #{it['id']} quebró. {informe}")
    _nuevo_intento(e, nuevo, 'tras quiebra')


def buscar_solucion(e):
    """Genera candidatos y los prueba en las últimas ~5 h de velas de 1 min reales (Coinbase/Yahoo). Se queda con el mejor."""
    rnd = random.Random()
    actual = e['genoma']
    cand = [('genoma actual con las lecciones aplicadas', actual)]
    hist = mejor_genoma()
    if hist:
        cand.append(('el mejor genoma de la historia', hist))
    for k in range(4):
        cand.append((f'mutación {k + 1} del actual', _mutar(actual, rnd, k >= 2)))
    b = e['bandido']
    for _ in range(3):  # estrategias elegidas por muestreo de Thompson (más éxito → más probable)
        est = max(ESTRATEGIAS, key=lambda s: rnd.betavariate(*b.get(s, [1, 1])))
        g = copy.deepcopy(actual)
        g['estrategia'], g['p'] = est, dict(ESTRATEGIAS[est])
        cand.append((f'cambiar a {est}', _mutar(g, rnd)))
    for m in MARCOS:  # el mismo genoma en otros marcos temporales
        if m != actual.get('marco', '1m'):
            g = copy.deepcopy(actual)
            g['marco'] = m
            cand.append((f'mismo sistema en velas de {m}', g))
    cfg = motor._fila(e['cuenta_id'])['config']
    datos_m, costes = {}, []
    for s in _universo(e)[:8]:
        try:
            q = datos.cotizacion(s)
        except Exception:
            continue
        if not q['abierto']:
            continue
        sp = ((q['ask'] - q['bid']) / q['precio'] * 100) if q.get('ask') and q.get('bid') else 0.01
        costes.append(coste_pct(cfg, sp, q['tipo']))
        for m in MARCOS:
            try:
                datos_m.setdefault(m, []).append(datos.velas(s, m)[:-1])
            except Exception:
                pass
    if not datos_m:
        return _mutar(actual, rnd), 'Sin datos para probar soluciones: sigo con una mutación.'
    coste = statistics.fmean(costes) if costes else coste_pct(cfg, 0.01)
    res = []
    for nombre, g in cand:
        rs = simular(g, datos_m.get(g.get('marco', '1m'), []), coste)
        res.append((puntuar(rs), nombre, g, len(rs), statistics.fmean(rs) if rs else 0))
    res.sort(key=lambda x: x[0], reverse=True)
    p, nombre, g, n, m = res[0]
    series = datos_m.get('1m', [])
    tabla = '; '.join(f"{x[1]}: {x[4]:+.2f}R en {x[3]} trades" for x in res[:4])
    if p <= 0:
        g = copy.deepcopy(g)
        g['riesgo_pct'] = max(round(g['riesgo_pct'] * 0.7, 1), 5.0)
        extra = f' Ninguna prueba sale positiva en datos recientes: arriesgo menos por operación ({g["riesgo_pct"]} %).'
    else:
        extra = ''
    return g, f"Probé {len(res)} soluciones en {len(series)} mercados (últimas horas reales). Elegida: {nombre} ({m:+.2f}R por trade en {n} trades). Ranking: {tabla}.{extra}"

# ---------------- consultas ----------------


def resumen():
    preparar()
    e = estado() or {}
    it = _intento_actual()
    r = motor.resumen(e['cuenta_id']) if e.get('cuenta_id') and db.uno('SELECT 1 x FROM cuentas WHERE id=?', (e['cuenta_id'],)) else None
    tot = db.uno('SELECT COUNT(*) n, SUM(r>0) g, AVG(r) m FROM ag_trades WHERE r IS NOT NULL')
    return {'activo': bool(e.get('activo')) and bool(_hilo and _hilo.is_alive()), 'configurado': bool(e), 'cuenta_id': e.get('cuenta_id'),
            'capital': e.get('capital'), 'meta': e.get('meta'), 'mercados': e.get('mercados'), 'hasta': e.get('hasta'),
            'equity': r['equity'] if r else None, 'intento': it, 'genoma': e.get('genoma'), 'metas_logradas': e.get('metas_logradas', 0),
            'intentos': db.uno('SELECT COUNT(*) n FROM ag_intentos')['n'], 'trades': tot['n'], 'ganados': tot['g'] or 0,
            'r_medio': round(tot['m'], 3) if tot['m'] is not None else None, 'descartes_coste': e.get('descartes_coste', 0),
            'bandido': e.get('bandido'), 'log': LOG[-40:], 'posiciones': motor.posiciones(e['cuenta_id']) if r else []}


def lecciones(limite=50):
    preparar()
    return db.q('SELECT * FROM ag_lecciones ORDER BY id DESC LIMIT ?', (limite,))


def intentos(limite=50):
    preparar()
    return db.q('SELECT id,inicio,fin,capital,meta,max_equity,final,trades,ganados,resultado,causa,genoma FROM ag_intentos ORDER BY id DESC LIMIT ?', (limite,))


def informe(desde=None):
    """Resumen en texto de lo que pasó (por defecto, desde el inicio de la sesión): para que APOLO lo cuente por la mañana."""
    preparar()
    e = estado() or {}
    desde = int(desde or e.get('sesion_inicio') or 0)
    its = db.q('SELECT * FROM ag_intentos WHERE inicio>=? ORDER BY id', (desde,))
    tr = db.q('SELECT a.*, t.pnl FROM ag_trades a JOIN trades t ON t.id=a.trade_id WHERE a.abierto>=? AND a.r IS NOT NULL', (desde,))
    lec = db.q("SELECT tipo, COUNT(*) n FROM ag_lecciones WHERE ts>=? AND tipo NOT IN ('quiebra','ruido') GROUP BY tipo ORDER BY n DESC", (desde,))
    ult = db.q("SELECT texto FROM ag_lecciones WHERE ts>=? AND tipo NOT IN ('ruido') ORDER BY id DESC LIMIT 6", (desde,))
    if not its:
        return 'El agente no ha hecho intentos todavía.'
    mx = max(its, key=lambda x: x['max_equity'] or 0)
    g = sum(1 for t in tr if t['pnl'] > 0)
    por_est = {}
    for t in tr:
        por_est.setdefault(t['estrategia'], []).append(t['r'])
    est_txt = ', '.join(f"{k} {statistics.fmean(v):+.2f}R ({len(v)})" for k, v in sorted(por_est.items(), key=lambda kv: -statistics.fmean(kv[1])))
    lineas = [
        f"Sesión desde {time.strftime('%d/%m %H:%M', time.localtime(desde))}: {len(its)} intento(s), {len(tr)} operaciones, {g} ganadas ({(g / len(tr) * 100) if tr else 0:.0f} %).",
        f"Mejor momento: intento #{mx['id']} llegó a {mx['max_equity']:.3f} $ (meta {mx['meta']:.0f} $). Quiebras: {sum(1 for i in its if i['resultado'] == 'quiebra')}. Metas logradas: {sum(1 for i in its if i['resultado'] == 'meta')}.",
        f"Por estrategia: {est_txt or '—'}.",
        'Causas de pérdida más repetidas: ' + (', '.join(f"{x['tipo']} ×{x['n']}" for x in lec) or '—') + '.',
        'Últimas lecciones: ' + ' | '.join(x['texto'] for x in ult),
        f"Genoma actual: {json.dumps(e.get('genoma'), ensure_ascii=False)}",
    ]
    return '\n'.join(lineas)
