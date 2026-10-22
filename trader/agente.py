"""Enjambre de agentes autónomos de APOLO: N agentes, cada uno con su cuenta de microoperaciones simuladas (p. ej. 1 $ → 100 $).
Cada agente: opera con su genoma → autopsia de cada pérdida con su arreglo → si quiebra, prueba soluciones en datos reales recientes.
El enjambre: comparte lo que aprende (símbolos/horas que pierden para todos), evoluciona cada 2 h (el 20 % peor se reemplaza por hijos
del 10 % mejor) y, si hay varios equipos (Pi = central, PC = nodo), intercambia los mejores genomas cada 30 min (modelo de islas)."""
import copy, json, math, os, random, socket, statistics, sys, threading, time, traceback
from . import datos, db, indicadores as ind, motor

ESQUEMA = """
CREATE TABLE IF NOT EXISTS ag_agentes (id INTEGER PRIMARY KEY, nombre TEXT, cuenta_id INTEGER, genoma TEXT, generacion INTEGER DEFAULT 0,
  padre INTEGER, creado INTEGER, activo INTEGER DEFAULT 1, estado TEXT DEFAULT 'buscando', ultimo TEXT, descartes INTEGER DEFAULT 0,
  descartes_ref INTEGER DEFAULT 0, genoma_desde INTEGER, intentos INTEGER DEFAULT 0, metas INTEGER DEFAULT 0, max_equity REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ag_intentos (id INTEGER PRIMARY KEY, inicio INTEGER, fin INTEGER, capital REAL, meta REAL, max_equity REAL,
  final REAL, trades INTEGER DEFAULT 0, ganados INTEGER DEFAULT 0, resultado TEXT, causa TEXT, genoma TEXT);
CREATE TABLE IF NOT EXISTS ag_trades (trade_id INTEGER PRIMARY KEY, intento INTEGER, simbolo TEXT, direccion TEXT, estrategia TEXT,
  genoma TEXT, rasgos TEXT, abierto INTEGER, revisado INTEGER DEFAULT 0, r REAL, causa TEXT);
CREATE TABLE IF NOT EXISTS ag_lecciones (id INTEGER PRIMARY KEY, ts INTEGER, intento INTEGER, trade_id INTEGER, tipo TEXT, texto TEXT, cambio TEXT);
CREATE TABLE IF NOT EXISTS ag_nodos (nodo TEXT PRIMARY KEY, visto INTEGER, datos TEXT);
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



# ======================= ENJAMBRE =======================
NOMBRES = ['Lince', 'Halcón', 'Lobo', 'Cobra', 'Tiburón', 'Pantera', 'Zorro', 'Búho', 'Águila', 'Toro', 'Oso', 'Puma', 'Cuervo', 'Mantis',
           'Escorpión', 'Orca', 'Jaguar', 'Coyote', 'Hiena', 'Cóndor', 'Barracuda', 'Mamba', 'Tigre', 'León', 'Bisonte', 'Pulpo', 'Dragón', 'Fénix',
           'Kraken', 'Grifo', 'Víbora', 'Gato', 'Murciélago', 'Rinoceronte', 'Avispa', 'Raya']
NODO = os.environ.get('DMN_TRADER_NODO') or ('pi' if sys.platform.startswith('linux') else socket.gethostname().lower())
CENTRAL = os.environ.get('DMN_TRADER_CENTRAL', '').rstrip('/')
CENTRAL_TOKEN = os.environ.get('DMN_TRADER_CENTRAL_TOKEN', '')
TICK, EVOLUCION, PATRONES, SINCRONIA, MIGRACION = 20, 7200, 600, 60, 1800


def preparar():
    c = db.con()
    c.executescript(ESQUEMA)
    for t in ('ag_intentos', 'ag_trades', 'ag_lecciones'):
        cols = {r[1] for r in c.execute(f'PRAGMA table_info({t})')}
        if 'agente_id' not in cols:
            c.execute(f'ALTER TABLE {t} ADD COLUMN agente_id INTEGER')
    viejo = db.ajuste('agente')
    if viejo and viejo.get('cuenta_id'):  # el agente único de la v1 se retira (sus datos se quedan)
        db.ex('UPDATE cuentas SET archivada=1 WHERE id=?', (viejo['cuenta_id'],))
        db.ex("DELETE FROM ajustes WHERE clave='agente'")


def estado():
    return db.ajuste('enjambre', None)


def guardar(e):
    db.poner_ajuste('enjambre', e)


def _g(a):
    return json.loads(a['genoma'])


def agentes(activos=True):
    return db.q('SELECT * FROM ag_agentes' + (' WHERE activo=1' if activos else '') + ' ORDER BY id')


def _poner(aid, **k):
    db.ex(f"UPDATE ag_agentes SET {', '.join(f'{x}=?' for x in k)} WHERE id=?", (*k.values(), aid))


def _nombre(aid, rnd=random):
    return f"{rnd.choice(NOMBRES)}-{aid:03d}"


def genoma_inicial(i, rnd):
    g = copy.deepcopy(GENOMA_BASE)
    est = list(ESTRATEGIAS)[i % len(ESTRATEGIAS)]
    g['estrategia'], g['p'] = est, dict(ESTRATEGIAS[est])
    g['marco'] = ['5m', '15m', '1m'][(i // len(ESTRATEGIAS)) % 3]
    g = _mutar(g, rnd, fuerte=i >= 15)
    g['marco'] = ['5m', '15m', '1m'][(i // len(ESTRATEGIAS)) % 3]
    g['riesgo_pct'] = rnd.choice([5.0, 10.0, 20.0, 35.0])
    g['filtro_tendencia'] = rnd.random() < 0.5
    g['be_en_r'] = rnd.choice([0.0, 0.0, 1.0])
    return g


def crear_agente(e, genoma, padre=None, generacion=0):
    cid = motor.crear_cuenta('agente', e['capital'], 'USD', CUENTA_CFG, tipo='agente')['id']
    db.ex('UPDATE cuentas SET archivada=1 WHERE id=?', (cid,))
    aid = db.ex('INSERT INTO ag_agentes(nombre,cuenta_id,genoma,generacion,padre,creado,genoma_desde) VALUES(?,?,?,?,?,?,?)',
                ('?', cid, json.dumps(genoma), generacion, padre, int(time.time()), int(time.time())))
    _poner(aid, nombre=_nombre(aid))
    a = db.uno('SELECT * FROM ag_agentes WHERE id=?', (aid,))
    _nuevo_intento(e, a, genoma, 'nace')
    return a


def iniciar(n=50, capital=1.0, meta=100.0, mercados=('cripto', 'acciones'), hasta=None):
    preparar()
    e = estado() or {}
    n = max(1, min(int(n), 2000))
    if e.get('activo') and _hilo and _hilo.is_alive():  # ya en marcha: solo cambia el tamaño, sin reiniciar a nadie
        e.update(n=n, hasta=hasta or e.get('hasta'))
        guardar(e)
        vivos, rnd = agentes(), random.Random()
        for a in vivos[n:]:
            _retirar(a, 'sobra (tamaño del enjambre)')
        for i in range(len(vivos), n):
            crear_agente(e, genoma_inicial(i, rnd))
        log(f'↔ enjambre: {len(vivos)} → {n} agentes')
        return resumen()
    e.update({'activo': True, 'n': n, 'capital': float(capital), 'meta': float(meta), 'mercados': list(mercados), 'hasta': hasta,
              'sesion_inicio': int(time.time()), 'ultima_evolucion': time.time(), 'ultimos_patrones': time.time(),
              'generacion': e.get('generacion', 0), 'bandido': e.get('bandido') or {k: [1, 1] for k in ESTRATEGIAS},
              'bloqueos': e.get('bloqueos') or {'simbolos': [], 'horas': []}})
    guardar(e)
    rnd = random.Random()
    vivos = agentes()
    for a in vivos[n:]:
        _retirar(a, 'sobra (tamaño del enjambre)')
    for a in vivos[:n]:
        _nuevo_intento(e, a, _g(a), 'nueva sesión')
    for i in range(len(vivos), n):
        crear_agente(e, genoma_inicial(i, rnd))
    log(f'▶ enjambre de {n} agentes en el nodo {NODO}: {capital} $ → meta {meta} $')
    arrancar_hilo()
    return resumen()


def _retirar(a, motivo):
    for p in motor.posiciones(a['cuenta_id'], False):
        try:
            motor.cerrar(a['cuenta_id'], p['simbolo'], 100, motivo)
        except Exception:
            pass
    it = _intento_actual(a['id'])
    if it:
        db.ex('UPDATE ag_intentos SET fin=?, final=?, resultado=? WHERE id=?', (int(time.time()), motor.resumen(a['cuenta_id'])['equity'], 'parado', it['id']))
    _poner(a['id'], activo=0, estado='retirado', ultimo=motivo)


def parar(motivo='parado por el usuario'):
    e = estado()
    if not e:
        return None
    e['activo'] = False
    guardar(e)
    for a in agentes():
        for p in motor.posiciones(a['cuenta_id'], False):
            try:
                motor.cerrar(a['cuenta_id'], p['simbolo'], 100, 'enjambre parado')
            except Exception:
                pass
        it = _intento_actual(a['id'])
        if it:
            db.ex('UPDATE ag_intentos SET fin=?, final=?, resultado=? WHERE id=?', (int(time.time()), motor.resumen(a['cuenta_id'])['equity'], 'parado', it['id']))
        _poner(a['id'], estado='parado')
    log('⏹ ' + motivo)
    return resumen()


def arrancar_hilo():
    global _hilo
    if _hilo and _hilo.is_alive():
        return
    _parar.clear()
    _hilo = threading.Thread(target=_bucle, daemon=True, name='enjambre')
    _hilo.start()


ULTIMO_TICK = {'dur': 0, 'ts': 0}


def _bucle():
    while not _parar.is_set():
        t0 = time.time()
        try:
            e = estado()
            if not e or not e.get('activo'):
                return
            if e.get('hasta') and time.time() >= e['hasta']:
                parar('hora de fin alcanzada')
                return
            paso_enjambre(e)
        except Exception:
            log('error: ' + traceback.format_exc().splitlines()[-1])
            traceback.print_exc()
        ULTIMO_TICK.update(dur=round(time.time() - t0, 2), ts=int(time.time()))
        _parar.wait(max(2, TICK - (time.time() - t0)))

# ---------------- datos compartidos del tick ----------------


def _universo(e, g=None):
    sims = []
    if 'cripto' in e['mercados']:
        sims += CRIPTO
    if 'acciones' in e['mercados']:
        sims += ACCIONES
    bloq = set(e.get('bloqueos', {}).get('simbolos', [])) | set((g or {}).get('simbolos_bloqueados', []))
    return [s for s in sims if s not in bloq]


def contexto(e, marcos):
    ctx = {'q': {}, 'v': {}, 'coste': {}, 'sen': {}}
    cfg = dict(CUENTA_CFG, **{'comision_pct': CUENTA_CFG['comision_pct']})
    for s in _universo(e):
        try:
            q = datos.cotizacion(s)
        except Exception:
            continue
        if not q['abierto']:
            continue
        ctx['q'][s] = q
        sp = ((q['ask'] - q['bid']) / q['precio'] * 100) if q.get('ask') and q.get('bid') else 0.0
        ctx['coste'][s] = (coste_pct(cfg, sp, q['tipo']), sp)
        for m in marcos:
            try:
                ctx['v'][(s, m)] = datos.velas(s, m)[:-1]
            except Exception:
                pass
    return ctx


def _senales_cache(ctx, g, s):
    clave = (json.dumps({k: g[k] for k in ('marco', 'estrategia', 'p', 'vol_min_pct', 'vol_max_pct', 'filtro_tendencia', 'cortos', 'horas_bloqueadas')}, sort_keys=True), s)
    if clave not in ctx['sen']:
        v = ctx['v'].get((s, g['marco']))
        ctx['sen'][clave] = (senales(g, v), v) if v and len(v) >= 60 else (None, v)
    return ctx['sen'][clave]

# ---------------- un tick de todo el enjambre ----------------


def paso_enjambre(e):
    vivos = agentes()
    marcos = {_g(a).get('marco', '1m') for a in vivos} or {'5m'}
    ctx = contexto(e, marcos)
    for a in vivos:
        try:
            paso_agente(e, a, ctx)
        except Exception:
            log(f"error en {a['nombre']}: " + traceback.format_exc().splitlines()[-1])
    ahora_ = time.time()
    e = estado()
    if ahora_ - e.get('ultimos_patrones', 0) > PATRONES:
        e['ultimos_patrones'] = ahora_
        _patrones_globales(e)
        guardar(e)
    if ahora_ - e.get('ultima_evolucion', 0) > EVOLUCION:
        e['ultima_evolucion'] = ahora_
        evolucionar(e)
    if CENTRAL and ahora_ - e.get('ultima_sincronia', 0) > SINCRONIA:
        e['ultima_sincronia'] = ahora_
        guardar(e)
        sincronizar(e)


def _intento_actual(aid):
    return db.uno('SELECT * FROM ag_intentos WHERE fin IS NULL AND agente_id=? ORDER BY id DESC LIMIT 1', (aid,))


def _nuevo_intento(e, a, g, motivo):
    cid = a['cuenta_id']
    for p in motor.posiciones(cid, False):
        try:
            motor.cerrar(cid, p['simbolo'], 100, 'nuevo intento')
        except Exception:
            pass
    db.ex("UPDATE ordenes SET estado='cancelada', motivo='nuevo intento' WHERE cuenta_id=? AND estado IN ('pendiente','espera')", (cid,))
    viejo = _intento_actual(a['id'])
    if viejo:
        db.ex('UPDATE ag_intentos SET fin=?, final=?, resultado=COALESCE(resultado,?) WHERE id=?',
              (int(time.time()), motor.resumen(cid)['equity'], 'reiniciado', viejo['id']))
    db.ex('UPDATE cuentas SET efectivo=?, capital_inicial=? WHERE id=?', (e['capital'], e['capital'], cid))
    iid = db.ex('INSERT INTO ag_intentos(inicio,capital,meta,max_equity,genoma,agente_id) VALUES(?,?,?,?,?,?)',
                (int(time.time()), e['capital'], e['meta'], e['capital'], json.dumps(g), a['id']))
    motor.instantanea(cid)
    _poner(a['id'], genoma=json.dumps(g), intentos=(a.get('intentos') or 0) + 1, estado='buscando', ultimo=f'intento #{iid} ({motivo})')
    return iid


def _leccion(aid, intento, tid, tipo, texto, cambio):
    db.ex('INSERT INTO ag_lecciones(ts,intento,trade_id,tipo,texto,cambio,agente_id) VALUES(?,?,?,?,?,?,?)',
          (int(time.time()), intento, tid, tipo, texto, json.dumps(cambio, ensure_ascii=False), aid))
    log('🧠 ' + texto[:200])


def paso_agente(e, a, ctx):
    cid, g = a['cuenta_id'], _g(a)
    it = _intento_actual(a['id'])
    if not it:
        _nuevo_intento(e, a, g, 'sin intento abierto')
        return
    motor.procesar(cid)
    eq = motor.resumen(cid)['equity']
    if eq > it['max_equity']:
        db.ex('UPDATE ag_intentos SET max_equity=? WHERE id=?', (eq, it['id']))
    if eq > (a['max_equity'] or 0):
        _poner(a['id'], max_equity=eq)
    _registrar_cerrados(e, a, it)
    _autopsias(a)
    a = db.uno('SELECT * FROM ag_agentes WHERE id=?', (a['id'],))
    g = _g(a)
    if eq >= e['meta']:
        db.ex('UPDATE ag_intentos SET fin=?, final=?, resultado=?, causa=? WHERE id=?', (int(time.time()), eq, 'meta', '¡meta alcanzada!', it['id']))
        _poner(a['id'], metas=(a['metas'] or 0) + 1)
        _leccion(a['id'], it['id'], None, 'meta', f"🏆 {a['nombre']} convirtió {e['capital']} $ en {eq:.2f} $ con {g['estrategia']} en velas de {g['marco']}.", {})
        motor.avisar(motor._fila(cid), f"🏆 {a['nombre']} convirtió {e['capital']} $ en {eq:.2f} $ (intento #{it['id']}).")
        _nuevo_intento(e, a, g, 'meta alcanzada: ¿lo repite?')
        return
    if eq < e['capital'] * 0.1:
        _quiebra(e, a, it, eq, ctx)
        return
    _gestionar(a, g)
    pos = motor.posiciones(cid, False)
    if not pos and (a['descartes'] or 0) - (a['descartes_ref'] or 0) > 150:
        nuevo, inf = buscar_solucion(e, a, ctx)
        _poner(a['id'], genoma=json.dumps(nuevo), descartes_ref=a['descartes'], genoma_desde=int(time.time()))
        _leccion(a['id'], it['id'], None, 'inactivo', f"{a['nombre']}: mucho rato sin operaciones que paguen comisión + spread. {inf}", {})
        return
    if pos:
        _poner(a['id'], estado='en posición', ultimo=' · '.join(f"{p['simbolo']} {'largo' if p['cantidad'] > 0 else 'corto'}" for p in pos))
    else:
        _poner(a['id'], estado='buscando')
    if len(pos) >= CUENTA_CFG['max_posiciones']:
        return
    hora = time.gmtime().tm_hour
    if hora in g['horas_bloqueadas'] or hora in e.get('bloqueos', {}).get('horas', []):
        return
    abiertos = {p['simbolo'] for p in pos}
    for sim in _universo(e, g):
        if sim in abiertos or sim not in ctx['q']:
            continue
        (s_atr, v) = _senales_cache(ctx, g, sim)
        if not s_atr:
            continue
        s, atr, tend = s_atr
        if not s[-1] or not atr[-1]:
            continue
        q, d = ctx['q'][sim], s[-1]
        p = q['precio']
        coste, spread = ctx['coste'][sim]
        dist = atr[-1] * g['stop_atr']
        if g['rr'] * dist / p * 100 < g['min_ventaja'] * coste:
            db.ex('UPDATE ag_agentes SET descartes=descartes+1 WHERE id=?', (a['id'],))
            continue
        r = motor.resumen(cid)
        libres = max(1, CUENTA_CFG['max_posiciones'] - len(motor.posiciones(cid, False)))
        qty = min(r['equity'] * g['riesgo_pct'] / 100 / dist, r['poder_compra'] * 0.97 / libres / p)
        if qty * p < 0.0001:
            continue
        sl, tp = p - d * dist, p + d * g['rr'] * dist
        rasgos = {'atr_pct': round(atr[-1] / p * 100, 4), 'tendencia': tend[-1], 'hora': hora, 'spread_pct': round(spread, 4),
                  'coste_pct': round(coste, 4), 'precio': p, 'dist': dist}
        try:
            o = motor.enviar(cid, sim, 'buy' if d > 0 else 'sell', qty, 'mercado', sl=sl, tp=tp, setup=g['estrategia'],
                             nota=f"{a['nombre']} {g['estrategia']} {g['marco']}", origen='agente')
        except motor.ErrorOrden as err:
            log(f"{a['nombre']}: orden rechazada {sim}: {err}")
            continue
        if o['estado'] == 'ejecutada':
            pz = db.uno('SELECT trade_id FROM posiciones WHERE cuenta_id=? AND simbolo=?', (cid, sim))
            if pz:
                db.ex('INSERT OR IGNORE INTO ag_trades(trade_id,intento,simbolo,direccion,estrategia,genoma,rasgos,abierto,agente_id) VALUES(?,?,?,?,?,?,?,?,?)',
                      (pz['trade_id'], it['id'], sim, 'largo' if d > 0 else 'corto', g['estrategia'], json.dumps(g), json.dumps(rasgos), int(time.time()), a['id']))
                txt = f"{'🟢' if d > 0 else '🔴'} {sim} {'largo' if d > 0 else 'corto'} @ {o['precio_ejec']:.6g}"
                _poner(a['id'], estado='en posición', ultimo=txt)
                log(f"{a['nombre']} {txt}")
        if len(motor.posiciones(cid, False)) >= CUENTA_CFG['max_posiciones']:
            break


def _gestionar(a, g):
    cid = a['cuenta_id']
    for p in motor.posiciones(cid):
        t = db.uno('SELECT * FROM trades WHERE id=?', (p['trade_id'],))
        if not t:
            continue
        if (time.time() - t['abierto']) / 60 >= g['max_barras'] * MARCOS.get(g.get('marco', '1m'), 1):
            try:
                motor.cerrar(cid, p['simbolo'], 100, 'tiempo')
            except motor.ErrorOrden:
                pass
            continue
        if g['be_en_r'] and t['stop_inicial'] and p['stop'] is not None:
            dist = abs(t['entrada'] - t['stop_inicial'])
            fav = (p['precio'] - t['entrada']) * (1 if p['cantidad'] > 0 else -1)
            if dist and fav >= g['be_en_r'] * dist and abs(p['stop'] - t['entrada']) > dist * 0.05:
                motor.mover_stop(cid, p['simbolo'], stop=t['entrada'])


def _registrar_cerrados(e, a, it):
    for t in db.q("""SELECT t.id, t.r, t.pnl, ag.estrategia FROM trades t JOIN ag_trades ag ON ag.trade_id=t.id
                     WHERE t.estado='cerrado' AND ag.r IS NULL AND t.cuenta_id=?""", (a['cuenta_id'],)):
        r = t['r'] if t['r'] is not None else 0.0
        db.ex('UPDATE ag_trades SET r=? WHERE trade_id=?', (r, t['id']))
        db.ex('UPDATE ag_intentos SET trades=trades+1, ganados=ganados+? WHERE id=?', (1 if t['pnl'] > 0 else 0, it['id']))
        e = estado()
        b = e['bandido'].setdefault(t['estrategia'], [1, 1])
        b[0 if t['pnl'] > 0 else 1] += 1
        guardar(e)
        _poner(a['id'], ultimo=f"{'✅' if t['pnl'] > 0 else '❌'} {r:+.2f}R")


def _autopsias(a):
    """Por qué se perdió cada trade (revisado ≥ 15 min después de cerrar) y qué cambiar en ESTE agente."""
    pend = db.q("""SELECT ag.*, t.entrada, t.salida, t.stop_inicial, t.objetivo, t.mfe, t.pnl, t.comisiones, t.cerrado, t.nota_cierre, t.cantidad
                   FROM ag_trades ag JOIN trades t ON t.id=ag.trade_id
                   WHERE ag.revisado=0 AND t.estado='cerrado' AND t.cerrado < ? AND ag.agente_id=?""", (int(time.time()) - 900, a['id']))
    if not pend:
        return
    g = _g(a)
    for x in pend:
        db.ex('UPDATE ag_trades SET revisado=1 WHERE trade_id=?', (x['trade_id'],))
        if x['pnl'] > 0:
            continue
        causas = diagnosticar(x, g)
        db.ex('UPDATE ag_trades SET causa=? WHERE trade_id=?', (causas[0][0], x['trade_id']))
        for tipo, texto, cambio in causas[:2]:
            _leccion(a['id'], x['intento'], x['trade_id'], tipo, f"{a['nombre']} · {texto}", cambio)
            g.update(cambio)
    _poner(a['id'], genoma=json.dumps(g))


def diagnosticar(x, g):
    ras, gt = json.loads(x['rasgos']), json.loads(x['genoma'])
    d = 1 if x['direccion'] == 'largo' else -1
    dist = abs(x['entrada'] - (x['stop_inicial'] or x['entrada'])) or ras['dist']
    bruto = (x['salida'] - x['entrada']) * d * x['cantidad']
    causas = []
    if x['comisiones'] > 0 and (bruto > -x['comisiones'] * 0.5 or ras['coste_pct'] > 0.35 * dist / x['entrada'] * 100):
        sig = {'1m': '5m', '5m': '15m'}.get(g.get('marco', '1m'))
        if g['min_ventaja'] >= 3 and sig:
            causas.append(('costes', f"{x['simbolo']}: otra vez la comisión y el spread ({ras['coste_pct']:.3f} %) se comen la ganancia en velas de {g.get('marco')}. Solución: subir a velas de {sig}.", {'marco': sig, 'min_ventaja': 2.0}))
        else:
            nv = round(min(g['min_ventaja'] + 0.5, 6), 2)
            causas.append(('costes', f"{x['simbolo']}: la comisión y el spread ({ras['coste_pct']:.3f} %) se comieron la operación. Solución: exigir objetivo ≥ {nv}× los costes.", {'min_ventaja': nv}))
    despues = []
    try:
        despues = [b for b in datos.velas(x['simbolo'], gt.get('marco', '1m')) if b['t'] > x['cerrado']][:30]
    except Exception:
        pass
    if x['stop_inicial'] and abs(x['salida'] - x['stop_inicial']) <= dist * 0.3 and x['objetivo'] and despues and \
            any((b['h'] >= x['objetivo']) if d > 0 else (b['l'] <= x['objetivo']) for b in despues):
        nv = round(min(g['stop_atr'] * 1.2, 4.0), 2)
        causas.append(('stop_ajustado', f"{x['simbolo']}: saltó el stop y DESPUÉS el precio llegó al objetivo (stop dentro del ruido). Solución: stop de {g['stop_atr']} a {nv} ATR.", {'stop_atr': nv}))
    if x['mfe'] and x['mfe'] >= dist * 0.9:
        if not g['be_en_r']:
            causas.append(('devolvio', f"{x['simbolo']}: iba +{x['mfe'] / dist:.1f}R y acabó en pérdida. Solución: stop a breakeven en +1R.", {'be_en_r': 1.0}))
        else:
            nv = round(max(g['rr'] * 0.85, 1.0), 2)
            causas.append(('devolvio', f"{x['simbolo']}: devolvió la ganancia aun con breakeven. Solución: objetivo de {g['rr']}R a {nv}R.", {'rr': nv}))
    if ras['tendencia'] == -d and not g['filtro_tendencia']:
        causas.append(('contra_tendencia', f"{x['simbolo']}: entró contra la tendencia de la EMA50. Solución: solo a favor de la tendencia.", {'filtro_tendencia': True}))
    gan = [json.loads(y['rasgos'])['atr_pct'] for y in db.q('SELECT rasgos FROM ag_trades WHERE r > 0 ORDER BY trade_id DESC LIMIT 60')]
    if len(gan) >= 8:
        med = statistics.median(gan)
        if ras['atr_pct'] > med * 2.2:
            nv = round(max(med * 2, 0.02), 4)
            causas.append(('volatilidad', f"{x['simbolo']}: volatilidad extrema (ATR {ras['atr_pct']} % vs {med:.3f} % en las ganadoras). Solución: máximo {nv} %.", {'vol_max_pct': nv}))
        elif ras['atr_pct'] < med * 0.4:
            nv = round(ras['atr_pct'] * 1.1, 4)
            causas.append(('sin_movimiento', f"{x['simbolo']}: mercado demasiado quieto (ATR {ras['atr_pct']} %). Solución: mínimo {nv} %.", {'vol_min_pct': nv}))
    if (x['nota_cierre'] or '') == 'tiempo' and not causas:
        nv = max(int(g['max_barras'] * 0.75), 5)
        causas.append(('sin_continuidad', f"{x['simbolo']}: la señal no tuvo continuidad. Solución: salir antes ({nv} velas).", {'max_barras': nv}))
    if not causas:
        causas.append(('ruido', f"{x['simbolo']}: pérdida normal de {gt['estrategia']}, sin causa clara. No cambio nada por un solo trade.", {}))
    return causas


def _patrones_globales(e):
    """Conocimiento COMPARTIDO: lo que pierde en todo el enjambre se bloquea para todos."""
    filas = db.q('SELECT simbolo, rasgos, r FROM ag_trades WHERE r IS NOT NULL ORDER BY trade_id DESC LIMIT 1500')
    por_s, por_h = {}, {}
    for f in filas:
        por_s.setdefault(f['simbolo'], []).append(f['r'])
        por_h.setdefault(json.loads(f['rasgos'])['hora'], []).append(f['r'])
    b = e.setdefault('bloqueos', {'simbolos': [], 'horas': []})
    for s, rs in por_s.items():
        if len(rs) >= 25 and statistics.fmean(rs) < -0.35 and s not in b['simbolos'] and len(b['simbolos']) < len(CRIPTO + ACCIONES) - 4:
            b['simbolos'].append(s)
            _leccion(None, None, None, 'compartida', f"Enjambre: {s} pierde en {len(rs)} operaciones de varios agentes ({statistics.fmean(rs):+.2f}R de media). Nadie lo opera más.", {'simbolo': s})
    for h, rs in por_h.items():
        if len(rs) >= 25 and statistics.fmean(rs) < -0.35 and h not in b['horas'] and len(b['horas']) < 8:
            b['horas'].append(h)
            _leccion(None, None, None, 'compartida', f"Enjambre: a las {h}:00 UTC se pierde de forma sistemática ({len(rs)} trades, {statistics.fmean(rs):+.2f}R). Nadie opera a esa hora.", {'hora': h})


def aptitud(a):
    rs = [x['r'] for x in db.q('SELECT r FROM ag_trades WHERE agente_id=? AND abierto>=? AND r IS NOT NULL', (a['id'], a['genoma_desde'] or 0))]
    return (puntuar(rs) if len(rs) >= 5 else None), len(rs)


def _cruce(g1, g2, rnd):
    h = copy.deepcopy(g1)
    for k in ('stop_atr', 'rr', 'filtro_tendencia', 'riesgo_pct', 'max_barras', 'be_en_r', 'min_ventaja', 'marco'):
        if rnd.random() < 0.5:
            h[k] = copy.deepcopy(g2[k])
    return h


def evolucionar(e):
    """Selección natural: el 20 % peor se reemplaza por hijos (mutación / cruce) del 10 % mejor."""
    rnd = random.Random()
    vivos = agentes()
    medidos = []
    for a in vivos:
        f, n = aptitud(a)
        edad = time.time() - (a['genoma_desde'] or a['creado'])
        if f is None and edad > 2 * EVOLUCION and n < 5:
            f = -1.0  # no opera: tampoco aprende
        if f is not None:
            medidos.append((f, a))
    if len(medidos) < 6:
        return
    medidos.sort(key=lambda x: x[0], reverse=True)
    top = medidos[:max(3, len(vivos) // 10)]
    malos = [x for x in medidos[-max(1, len(vivos) // 5):] if x[0] < top[-1][0]]
    e['generacion'] = e.get('generacion', 0) + 1
    nombres = []
    for f, a in malos:
        p1, p2 = rnd.choice(top)[1], rnd.choice(top)[1]
        g = _cruce(_g(p1), _g(p2), rnd) if p1['id'] != p2['id'] and rnd.random() < 0.35 else _g(p1)
        g = _mutar(g, rnd)
        nuevo = _nombre(a['id'], rnd)
        nombres.append(f"{a['nombre']} ({f:+.2f}) → {nuevo}, hijo de {p1['nombre']}")
        _poner(a['id'], nombre=nuevo, generacion=(p1['generacion'] or 0) + 1, padre=p1['id'], genoma_desde=int(time.time()),
               descartes=0, descartes_ref=0, max_equity=0, metas=0, intentos=0)
        _nuevo_intento(e, db.uno('SELECT * FROM ag_agentes WHERE id=?', (a['id'],)), g, f"evolución gen {e['generacion']}")
    guardar(e)
    mejores = ', '.join(f"{a['nombre']} {f:+.2f}R ({_g(a)['estrategia']} {_g(a)['marco']})" for f, a in top[:3])
    _leccion(None, None, None, 'evolucion', f"Generación {e['generacion']}: los mejores son {mejores}. Reemplacé {len(nombres)}: " + '; '.join(nombres[:8]), {})


def buscar_solucion(e, a, ctx):
    rnd = random.Random()
    actual = _g(a)
    cand = [('su genoma con las lecciones aplicadas', actual)]
    vivos = [(aptitud(x)[0], x) for x in agentes() if x['id'] != a['id']]
    vivos = [v for v in vivos if v[0] is not None]
    if vivos:
        mejor = max(vivos, key=lambda v: v[0])[1]
        cand.append((f"copiar a {mejor['nombre']} (el mejor del enjambre)", _g(mejor)))
    for k in range(4):
        cand.append((f'mutación {k + 1}', _mutar(actual, rnd, k >= 2)))
    b = estado()['bandido']
    for _ in range(3):
        est = max(ESTRATEGIAS, key=lambda s: rnd.betavariate(*b.get(s, [1, 1])))
        g = copy.deepcopy(actual)
        g['estrategia'], g['p'] = est, dict(ESTRATEGIAS[est])
        cand.append((f'cambiar a {est}', _mutar(g, rnd)))
    for m in MARCOS:
        if m != actual.get('marco'):
            g = copy.deepcopy(actual)
            g['marco'] = m
            cand.append((f'mismo sistema en velas de {m}', g))
    sims = [s for s in _universo(e, actual) if s in ctx['q']][:8]
    series = {}
    for m in MARCOS:
        series[m] = [ctx['v'].get((s, m)) or datos.velas(s, m)[:-1] for s in sims] if sims else []
    if not sims:
        return _mutar(actual, rnd), 'Sin mercados abiertos para probar soluciones: sigo con una mutación.'
    coste = statistics.fmean(ctx['coste'][s][0] for s in sims)
    res = []
    for nombre, g in cand:
        rs = simular(g, [v for v in series.get(g.get('marco', '1m'), []) if v], coste)
        res.append((puntuar(rs), nombre, g, len(rs), statistics.fmean(rs) if rs else 0))
    res.sort(key=lambda x: x[0], reverse=True)
    p, nombre, g, n, m = res[0]
    g = copy.deepcopy(g)
    extra = ''
    if p <= 0:
        g['riesgo_pct'] = max(round(g['riesgo_pct'] * 0.7, 1), 3.0)
        extra = f' Nada sale positivo en datos recientes: arriesgo menos ({g["riesgo_pct"]} %).'
    tabla = '; '.join(f"{x[1]}: {x[4]:+.2f}R/{x[3]}" for x in res[:3])
    return g, f"Probé {len(res)} soluciones en {len(sims)} mercados reales. Elegida: {nombre} ({m:+.2f}R por trade en {n}). Top: {tabla}.{extra}"


def _quiebra(e, a, it, eq, ctx):
    causas = db.q('SELECT tipo, COUNT(*) n FROM ag_lecciones WHERE intento=? AND agente_id=? GROUP BY tipo ORDER BY n DESC', (it['id'], a['id']))
    causa = ', '.join(f"{c['tipo']} ×{c['n']}" for c in causas[:3]) or 'sin autopsias aún'
    db.ex('UPDATE ag_intentos SET fin=?, final=?, resultado=?, causa=? WHERE id=?', (int(time.time()), eq, 'quiebra', causa, it['id']))
    nuevo, inf = buscar_solucion(e, a, ctx)
    _leccion(a['id'], it['id'], None, 'quiebra', f"💀 {a['nombre']} quebró (llegó a {it['max_equity']:.3f} $). Causas: {causa}. {inf}", {})
    _poner(a['id'], genoma_desde=int(time.time()), estado='quebrado')
    _nuevo_intento(e, db.uno('SELECT * FROM ag_agentes WHERE id=?', (a['id'],)), nuevo, 'tras quiebra')

# ---------------- varios nodos (la Pi es la central; el PC u otros equipos suman agentes) ----------------


def _vista_agentes(lim=None):
    stats = {r['agente_id']: r for r in db.q('SELECT agente_id, COUNT(*) n, SUM(r>0) g, AVG(r) m FROM ag_trades WHERE r IS NOT NULL GROUP BY agente_id')}
    out = []
    for a in agentes():
        g = _g(a)
        try:
            eq = motor.resumen(a['cuenta_id'])['equity']
        except Exception:
            eq = None
        st = stats.get(a['id'], {})
        out.append({'id': a['id'], 'nombre': a['nombre'], 'estado': a['estado'], 'ultimo': a['ultimo'], 'equity': round(eq, 4) if eq is not None else None,
                    'max_equity': round(a['max_equity'] or 0, 4), 'generacion': a['generacion'], 'intentos': a['intentos'], 'metas': a['metas'],
                    'trades': st.get('n', 0), 'ganados': st.get('g') or 0, 'r_medio': round(st['m'], 3) if st.get('m') is not None else None,
                    'marco': g.get('marco'), 'estrategia': g['estrategia'], 'riesgo_pct': g['riesgo_pct'], 'tendencia': g['filtro_tendencia']})
    return out[:lim] if lim else out


def sincronizar(e):
    """Nodo → central: manda su estado (para Mission Control) y cada 30 min intercambia los mejores genomas (modelo de islas)."""
    import urllib.request
    migrar = time.time() - e.get('ultima_migracion', 0) > MIGRACION
    top = sorted(((aptitud(a)[0], a) for a in agentes()), key=lambda x: -9 if x[0] is None else x[0], reverse=True)[:3]
    cuerpo = {'nodo': NODO, 'kpis': kpis(), 'agentes': _vista_agentes(), 'lecciones': lecciones(15),
              'migrantes': [{'genoma': _g(a), 'aptitud': f, 'nombre': a['nombre']} for f, a in top if f is not None] if migrar else [], 'pedir': migrar}
    req = urllib.request.Request(CENTRAL + '/api/enjambre/nodo', data=json.dumps(cuerpo, default=str).encode(), method='POST',
                                 headers={'content-type': 'application/json', **({'x-token': CENTRAL_TOKEN} if CENTRAL_TOKEN else {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode())
    except Exception as err:
        log(f'sin conexión con la central: {err}')
        return
    if migrar:
        e = estado()
        e['ultima_migracion'] = time.time()
        guardar(e)
        recibir_migrantes(resp.get('migrantes') or [], 'central')


def recibir_migrantes(migs, de):
    if not migs:
        return
    vivos = sorted(((aptitud(a)[0], a) for a in agentes()), key=lambda x: -9 if x[0] is None else x[0])
    e = estado()
    for m, (f, a) in zip(migs, vivos):
        if f is not None and m.get('aptitud') is not None and f >= m['aptitud']:
            continue
        _poner(a['id'], nombre=_nombre(a['id']), genoma_desde=int(time.time()), descartes=0, descartes_ref=0, generacion=(a['generacion'] or 0) + 1)
        _nuevo_intento(e, db.uno('SELECT * FROM ag_agentes WHERE id=?', (a['id'],)), m['genoma'], f"migrante de {de} ({m.get('nombre')})")
        _leccion(a['id'], None, None, 'migracion', f"Llega de {de} el sistema de {m.get('nombre')} ({m.get('aptitud', 0):+.2f}R) y sustituye a {a['nombre']}.", {})


def recibir_nodo(c):
    """En la central: guarda lo que manda un nodo y, si lo pide, le devuelve los 3 mejores genomas de aquí."""
    db.ex('INSERT INTO ag_nodos(nodo,visto,datos) VALUES(?,?,?) ON CONFLICT(nodo) DO UPDATE SET visto=excluded.visto, datos=excluded.datos',
          (str(c.get('nodo'))[:40], int(time.time()), json.dumps({k: c.get(k) for k in ('kpis', 'agentes', 'lecciones')}, default=str)))
    out = {'ok': True}
    if c.get('pedir'):
        if (estado() or {}).get('activo'):
            recibir_migrantes(c.get('migrantes') or [], c.get('nodo'))
        top = sorted(((aptitud(a)[0], a) for a in agentes()), key=lambda x: -9 if x[0] is None else x[0], reverse=True)[:3]
        out['migrantes'] = [{'genoma': _g(a), 'aptitud': f, 'nombre': a['nombre']} for f, a in top if f is not None]
    return out

# ---------------- consultas ----------------


def kpis():
    e = estado() or {}
    t = db.uno('SELECT COUNT(*) n, SUM(r>0) g, AVG(r) m FROM ag_trades WHERE r IS NOT NULL AND abierto>=?', (e.get('sesion_inicio', 0),))
    its = db.uno("SELECT SUM(resultado='quiebra') q, SUM(resultado='meta') m, MAX(max_equity) mx FROM ag_intentos WHERE inicio>=?", (e.get('sesion_inicio', 0),))
    return {'nodo': NODO, 'activo': bool(e.get('activo')) and bool(_hilo and _hilo.is_alive()), 'agentes': len(agentes()), 'n': e.get('n'),
            'capital': e.get('capital'), 'meta': e.get('meta'), 'mercados': e.get('mercados'), 'hasta': e.get('hasta'), 'generacion': e.get('generacion', 0),
            'trades': t['n'], 'ganados': t['g'] or 0, 'r_medio': round(t['m'], 3) if t['m'] is not None else None,
            'quiebras': its['q'] or 0, 'metas': its['m'] or 0, 'max_equity': round(its['mx'] or 0, 4), 'tick_s': ULTIMO_TICK['dur'],
            'bloqueos': e.get('bloqueos'), 'bandido': e.get('bandido'), 'sesion_inicio': e.get('sesion_inicio')}


_cache_mapa = {'t': 0, 'v': None}


def resumen():
    preparar()
    return {**kpis(), 'log': LOG[-40:]}


def mapa():
    """Todos los nodos con todos sus agentes (para Mission Control y el panel)."""
    if time.time() - _cache_mapa['t'] < 4:
        return _cache_mapa['v']
    nodos = [{'nodo': NODO, 'local': True, 'visto': int(time.time()), 'kpis': kpis(), 'agentes': _vista_agentes()}]
    for n in db.q('SELECT * FROM ag_nodos ORDER BY nodo'):
        d = json.loads(n['datos'])
        nodos.append({'nodo': n['nodo'], 'local': False, 'visto': n['visto'], 'en_linea': time.time() - n['visto'] < 300, **d})
    tot = {'agentes': sum(len(x.get('agentes') or []) for x in nodos), 'trades': sum((x.get('kpis') or {}).get('trades') or 0 for x in nodos),
           'metas': sum((x.get('kpis') or {}).get('metas') or 0 for x in nodos), 'quiebras': sum((x.get('kpis') or {}).get('quiebras') or 0 for x in nodos)}
    v = {'nodos': nodos, 'total': tot, 'lecciones': lecciones(25), 'log': LOG[-30:]}
    _cache_mapa.update(t=time.time(), v=v)
    return v


def agente(aid):
    a = db.uno('SELECT * FROM ag_agentes WHERE id=?', (aid,))
    if not a:
        raise motor.ErrorOrden('no existe ese agente')
    f, n = aptitud(a)
    return {**{k: a[k] for k in ('id', 'nombre', 'estado', 'ultimo', 'generacion', 'padre', 'intentos', 'metas', 'max_equity')},
            'genoma': _g(a), 'aptitud': f, 'trades_genoma': n, 'cuenta': motor.cuenta(a['cuenta_id']),
            'intentos_lista': db.q('SELECT id,inicio,fin,max_equity,final,trades,ganados,resultado,causa FROM ag_intentos WHERE agente_id=? ORDER BY id DESC LIMIT 15', (aid,)),
            'lecciones': db.q('SELECT * FROM ag_lecciones WHERE agente_id=? ORDER BY id DESC LIMIT 20', (aid,)),
            'trades': db.q("""SELECT t.simbolo,t.direccion,t.entrada,t.salida,t.pnl,t.r,t.abierto,t.cerrado,ag.causa FROM ag_trades ag JOIN trades t ON t.id=ag.trade_id
                              WHERE ag.agente_id=? ORDER BY t.id DESC LIMIT 20""", (aid,))}


def lecciones(limite=50):
    return db.q("SELECT l.*, a.nombre FROM ag_lecciones l LEFT JOIN ag_agentes a ON a.id=l.agente_id WHERE l.tipo<>'ruido' ORDER BY l.id DESC LIMIT ?", (limite,))


def informe(desde=None):
    preparar()
    e = estado() or {}
    desde = int(desde or e.get('sesion_inicio') or 0)
    tr = db.q('SELECT ag.*, t.pnl FROM ag_trades ag JOIN trades t ON t.id=ag.trade_id WHERE ag.abierto>=? AND ag.r IS NOT NULL', (desde,))
    its = db.q('SELECT * FROM ag_intentos WHERE inicio>=?', (desde,))
    if not tr and not its:
        return 'El enjambre aún no ha operado en esta sesión.'
    g = sum(1 for t in tr if t['pnl'] > 0)
    por = {}
    for t in tr:
        gt = json.loads(t['genoma'])
        por.setdefault(f"{gt['estrategia']} {gt['marco']}", []).append(t['r'])
    ranking = sorted(((k, statistics.fmean(v), len(v)) for k, v in por.items() if len(v) >= 5), key=lambda x: -x[1])
    mejores = sorted(((aptitud(a)[0], a) for a in agentes()), key=lambda x: -9 if x[0] is None else x[0], reverse=True)[:3]
    lec = db.q("SELECT tipo, COUNT(*) n FROM ag_lecciones WHERE ts>=? AND tipo NOT IN ('ruido','quiebra') GROUP BY tipo ORDER BY n DESC", (desde,))
    comp = db.q("SELECT texto FROM ag_lecciones WHERE ts>=? AND tipo IN ('compartida','evolucion','meta','migracion') ORDER BY id DESC LIMIT 6", (desde,))
    mx = max(its, key=lambda i: i['max_equity'] or 0) if its else None
    nodos = db.q('SELECT nodo, visto FROM ag_nodos')
    return '\n'.join([
        f"Desde {time.strftime('%d/%m %H:%M', time.localtime(desde))}: {len(agentes())} agentes en {NODO}" + (f" + nodos {', '.join(n['nodo'] for n in nodos)}" if nodos else '') +
        f", generación {e.get('generacion', 0)}. {len(tr)} operaciones, {g} ganadas ({(g / len(tr) * 100) if tr else 0:.0f} %).",
        f"Intentos: {len(its)} · quiebras {sum(1 for i in its if i['resultado'] == 'quiebra')} · metas {sum(1 for i in its if i['resultado'] == 'meta')}" +
        (f" · mejor capital alcanzado {mx['max_equity']:.3f} $ (meta {mx['meta']:.0f} $)." if mx else '.'),
        'Sistemas que mejor funcionan: ' + (', '.join(f"{k} {m:+.2f}R ({n})" for k, m, n in ranking[:4]) or 'aún sin datos suficientes') + '.',
        'Sistemas que peor: ' + (', '.join(f"{k} {m:+.2f}R ({n})" for k, m, n in ranking[-3:]) if len(ranking) > 4 else '—') + '.',
        'Mejores agentes ahora: ' + (', '.join(f"{a['nombre']} {f:+.2f}R ({_g(a)['estrategia']} {_g(a)['marco']})" for f, a in mejores if f is not None) or '—') + '.',
        'Por qué se pierde (autopsias): ' + (', '.join(f"{x['tipo']} ×{x['n']}" for x in lec) or '—') + '.',
        'Aprendizajes compartidos: ' + (' | '.join(x['texto'] for x in comp) or '—'),
    ])
