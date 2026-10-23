"""Mercados «Up or Down» de Polymarket (los que enseña Future.news, la terminal de GMGN) en SIMULADO: ¿BTC/ETH/SOL/XRP termina
más arriba o más abajo que al empezar la ventana de 5 o 15 min? Un enjambre de apostadores lee los precios y libros REALES
(gamma-api y clob de Polymarket, públicos y sin cuenta), compra acciones de Up o Down al ask como un taker, paga la comisión
real de estos mercados y cobra 1 $ por acción si acierta según la resolución OFICIAL de Polymarket. Nada de dinero real.

Estrategias: valor (probabilidad por modelo —distancia al precio de inicio frente a la volatilidad— contra el precio del
mercado), momento (apuesta a que sigue el lado que va ganando), contra (compra el lado barato esperando la vuelta) y azar
(control: si el azar gana a las demás, ninguna tiene ventaja). Cada 3 h el 25 % peor se sustituye por hijos del 25 % mejor."""
import copy, json, math, random, statistics, threading, time, traceback, urllib.request
from . import datos, db

GAMMA, CLOB = 'https://gamma-api.polymarket.com', 'https://clob.polymarket.com'
ACTIVOS = ['btc', 'eth', 'sol', 'xrp']
MARCOS = {'5m': 300, '15m': 900}
TICK, EVOLUCION, CAPITAL, MIN_APUESTA = 5, 3 * 3600, 5.0, 1.0
FEE_RATE, FEE_EXP = 0.25, 2  # comisión taker = acciones × p × 0,25 × (p·(1−p))² → 1,56 % a 50 ¢ (tabla oficial: 100 acc. a 0,45 → 0,69 $)
ESTRATEGIAS = ['valor', 'momento', 'contra', 'azar']
NOMBRES = ['Dado', 'Moneda', 'Ruleta', 'Naipe', 'Trébol', 'Herradura', 'Comodín', 'As', 'Tahúr', 'Crupier', 'Ficha', 'Bingo', 'Loto', 'Sota',
           'Caballo', 'Rey', 'Bala', 'Rayo', 'Cometa', 'Brújula', 'Péndulo', 'Oráculo', 'Augur', 'Sibila']
ESQUEMA = """
CREATE TABLE IF NOT EXISTS pm_agentes (id INTEGER PRIMARY KEY, nombre TEXT, genoma TEXT, capital REAL, generacion INTEGER DEFAULT 0,
  padre INTEGER, creado INTEGER, activo INTEGER DEFAULT 1, apuestas INTEGER DEFAULT 0, ganadas INTEGER DEFAULT 0, pnl REAL DEFAULT 0,
  apostado REAL DEFAULT 0, quiebras INTEGER DEFAULT 0, max_capital REAL DEFAULT 0, ultimo TEXT);
CREATE TABLE IF NOT EXISTS pm_apuestas (id INTEGER PRIMARY KEY, agente_id INTEGER, estrategia TEXT, slug TEXT, activo TEXT, marco TEXT,
  lado TEXT, precio REAL, acciones REAL, coste REAL, comision REAL, p_est REAL, segs_rest INTEGER, ref REAL, inicio_precio REAL, ts INTEGER,
  fin INTEGER, resultado TEXT, pago REAL, pnl REAL, resuelto INTEGER);
CREATE INDEX IF NOT EXISTS ix_pm_ap ON pm_apuestas(resultado, fin);
CREATE INDEX IF NOT EXISTS ix_pm_ag ON pm_apuestas(agente_id, slug);
CREATE TABLE IF NOT EXISTS pm_lecciones (id INTEGER PRIMARY KEY, ts INTEGER, texto TEXT);
"""
LOG, _MERC, _INI, _RES = [], {}, {}, {}
_hilo, _parar = {'t': None}, threading.Event()


def log(t):
    LOG.append(time.strftime('%H:%M:%S ') + t)
    del LOG[:-200]


def _get(url, cuerpo=None, timeout=12):
    req = urllib.request.Request(url, data=json.dumps(cuerpo).encode() if cuerpo is not None else None,
                                 headers={'User-Agent': 'dmn-trader', 'content-type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def comision(acciones, p):
    return acciones * p * FEE_RATE * (p * (1 - p)) ** FEE_EXP


def preparar():
    db.con().executescript(ESQUEMA)


def estado():
    return db.ajuste('updown') or {}


def guardar(e):
    db.poner_ajuste('updown', e)

# ---------------- mercados ----------------


def mercado(activo, marco, inicio):
    """El mercado de esa ventana: slug, tokens de Up y Down, inicio y fin (caché por slug)."""
    slug = f'{activo}-updown-{marco}-{inicio}'
    if slug not in _MERC:
        d = _get(f'{GAMMA}/events?slug={slug}')
        if not d or not d[0].get('markets'):
            _MERC[slug] = None
        else:
            m = d[0]['markets'][0]
            out, tok = json.loads(m['outcomes']), json.loads(m['clobTokenIds'])
            _MERC[slug] = {'slug': slug, 'activo': activo, 'marco': marco, 'inicio': inicio, 'fin': inicio + MARCOS[marco],
                           'tokens': dict(zip(out, tok)), 'titulo': d[0].get('title')}
        if len(_MERC) > 400:
            for k in list(_MERC)[:200]:
                del _MERC[k]
    return _MERC[slug]


def activos_ahora():
    ahora, out = int(time.time()), []
    for marco, dur in MARCOS.items():
        for a in ACTIVOS:
            try:
                m = mercado(a, marco, ahora // dur * dur)
            except Exception:
                continue
            if m:
                out.append(m)
    return out


def libros(mercs):
    """{token: {'bids': [(p, tamaño)] mejor primero, 'asks': [...]}} de todos los mercados en una sola llamada."""
    toks = [t for m in mercs for t in m['tokens'].values()]
    if not toks:
        return {}
    out = {}
    for b in _get(f'{CLOB}/books', [{'token_id': t} for t in toks]):
        out[b['asset_id']] = {'bids': sorted(((float(x['price']), float(x['size'])) for x in b.get('bids') or []), reverse=True),
                              'asks': sorted((float(x['price']), float(x['size'])) for x in b.get('asks') or [])}
    return out


def precio_inicio(activo, inicio):
    """Precio de referencia («price to beat»): apertura de la vela de 1 min de Coinbase en el inicio de la ventana. Polymarket usa
    Chainlink; la diferencia es de céntimos, pero cerca del empate cuenta (la resolución oficial manda al cobrar)."""
    k = (activo, inicio)
    if k not in _INI:
        v = datos.velas(f'{activo.upper()}-USD', '1m')
        x = next((b['o'] for b in v if b['t'] == inicio), None)
        if x is None:
            return None
        _INI[k] = x
        if len(_INI) > 500:
            for kk in list(_INI)[:250]:
                del _INI[kk]
    return _INI[k]


def sigma_min(activo):
    """Volatilidad por minuto (desviación de los rendimientos logarítmicos de la última hora)."""
    c = [b['c'] for b in datos.velas(f'{activo.upper()}-USD', '1m')[-61:]]
    r = [math.log(b / a) for a, b in zip(c, c[1:]) if a > 0 and b > 0]
    return statistics.pstdev(r) if len(r) > 10 else None


def p_subida(S, K, sig_m, segs):
    """Probabilidad de terminar por encima de K si el precio se mueve al azar con esa volatilidad (sin deriva)."""
    if segs <= 0 or not sig_m:
        return 1.0 if S > K else 0.0 if S < K else 0.5
    z = math.log(S / K) / (sig_m * math.sqrt(segs / 60))
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def contexto():
    mercs = activos_ahora()
    try:
        L = libros(mercs)
    except Exception as err:
        log(f'sin libros de Polymarket: {err}')
        return []
    ahora, out = time.time(), []
    for m in mercs:
        try:
            q = datos.cotizacion(f"{m['activo'].upper()}-USD")
            K = precio_inicio(m['activo'], m['inicio'])
            sig = sigma_min(m['activo'])
        except Exception:
            continue
        if not K:
            continue
        lb = {lado: L.get(t) or {'bids': [], 'asks': []} for lado, t in m['tokens'].items()}
        out.append(dict(m, S=q['precio'], K=K, sigma=sig, segs=m['fin'] - ahora, frac=(ahora - m['inicio']) / MARCOS[m['marco']], libros=lb,
                        p_up=p_subida(q['precio'], K, sig, m['fin'] - ahora)))
    return out


def comprar(asks, usd):
    """Compra a mercado recorriendo el libro: (acciones, coste sin comisión, precio medio) o None si no hay libro."""
    acc = coste = 0.0
    for p, s in asks:
        tomar = min(s, (usd - coste) / p)
        acc += tomar
        coste += tomar * p
        if coste >= usd - 1e-9:
            break
    return (acc, coste, coste / acc) if acc > 0 else None

# ---------------- apostadores ----------------


def genoma_inicial(i, rnd):
    g = {'estrategia': ESTRATEGIAS[i % len(ESTRATEGIAS)], 'marcos': rnd.choice([['5m'], ['15m'], ['5m', '15m']]),
         'activos': rnd.choice([['btc'], ['btc', 'eth'], ACTIVOS, ['eth', 'sol', 'xrp']]), 'margen': round(rnd.uniform(0.03, 0.12), 3),
         'desde': round(rnd.uniform(0.1, 0.8), 2), 'hasta': 0.97, 'tamano_pct': rnd.choice([10, 20, 25, 35]),
         'precio_max': round(rnd.uniform(0.7, 0.92), 2), 'sigma_mult': round(rnd.uniform(0.8, 1.4), 2),
         'umbral_mov': round(rnd.uniform(0.0002, 0.0015), 5), 'precio_contra': round(rnd.uniform(0.12, 0.35), 2)}
    g['hasta'] = round(min(0.98, g['desde'] + rnd.uniform(0.1, 0.5)), 2)
    return g


def mutar(g, rnd):
    g = copy.deepcopy(g)
    f = lambda v, lo, hi, d=3: round(min(hi, max(lo, v * rnd.uniform(0.8, 1.2))), d)
    g['margen'], g['precio_max'] = f(g['margen'], 0.0, 0.3), f(g['precio_max'], 0.5, 0.97, 2)
    g['sigma_mult'], g['umbral_mov'] = f(g['sigma_mult'], 0.5, 2.5, 2), f(g['umbral_mov'], 0.00005, 0.005, 5)
    g['precio_contra'] = f(g['precio_contra'], 0.05, 0.45, 2)
    g['desde'] = f(g['desde'], 0.0, 0.95, 2)
    g['hasta'] = round(min(0.99, max(g['desde'] + 0.05, g['hasta'] * rnd.uniform(0.9, 1.1))), 2)
    if rnd.random() < 0.15:
        g['tamano_pct'] = rnd.choice([10, 20, 25, 35, 50])
    if rnd.random() < 0.1:
        g['marcos'] = rnd.choice([['5m'], ['15m'], ['5m', '15m']])
    if rnd.random() < 0.1:
        g['activos'] = rnd.choice([['btc'], ['btc', 'eth'], ACTIVOS, ['eth', 'sol', 'xrp']])
    if g['estrategia'] != 'azar' and rnd.random() < 0.05:
        g['estrategia'] = rnd.choice(ESTRATEGIAS[:3])
    return g


def decidir(g, m, rnd=random):
    """('Up' | 'Down', probabilidad estimada) o None. Solo dentro de su ventana de entrada."""
    if not g['desde'] <= m['frac'] <= g['hasta'] or m['segs'] < 5:
        return None
    ask = {l: (m['libros'][l]['asks'][0][0] if m['libros'][l]['asks'] else None) for l in ('Up', 'Down')}
    e = g['estrategia']
    if e == 'valor':
        p_up = p_subida(m['S'], m['K'], (m['sigma'] or 0) * g['sigma_mult'], m['segs'])
        ventaja = {}
        for lado, p in (('Up', p_up), ('Down', 1 - p_up)):
            a = ask[lado]
            if a and a <= g['precio_max']:
                ventaja[lado] = (p - a - a * FEE_RATE * (a * (1 - a)) ** FEE_EXP, p)  # lo que se espera ganar por acción tras la comisión
        lado = max(ventaja, key=lambda l: ventaja[l][0], default=None)
        return (lado, ventaja[lado][1]) if lado and ventaja[lado][0] >= g['margen'] else None
    if e == 'momento':
        mov = math.log(m['S'] / m['K'])
        lado = 'Up' if mov > 0 else 'Down'
        if abs(mov) >= g['umbral_mov'] and ask[lado] and ask[lado] <= g['precio_max']:
            return lado, None
        return None
    if e == 'contra':
        lado = min(('Up', 'Down'), key=lambda l: ask[l] or 9)
        return (lado, None) if ask[lado] and ask[lado] <= g['precio_contra'] else None
    if e == 'azar':  # control: en un momento al azar de la ventana, a un lado al azar
        lado = rnd.choice(['Up', 'Down'])
        return (lado, None) if ask[lado] and ask[lado] <= g['precio_max'] and rnd.random() < 0.02 else None
    return None


def _g(a):
    return json.loads(a['genoma'])


def agentes(activos=True):
    return db.q('SELECT * FROM pm_agentes' + (' WHERE activo=1' if activos else '') + ' ORDER BY id')


def iniciar(n=24, capital=CAPITAL):
    preparar()
    e = estado()
    vivos = agentes()
    rnd = random.Random()
    for i in range(len(vivos), n):
        db.ex('INSERT INTO pm_agentes(nombre,genoma,capital,creado,max_capital) VALUES(?,?,?,?,?)',
              (f'{NOMBRES[i % len(NOMBRES)]}-{i + 1:02d}', json.dumps(genoma_inicial(i, rnd)), capital, int(time.time()), capital))
    for a in vivos[n:]:
        db.ex('UPDATE pm_agentes SET activo=0 WHERE id=?', (a['id'],))
    e.update(activo=True, n=n, capital=capital, inicio=e.get('inicio') or int(time.time()), ultima_evolucion=e.get('ultima_evolucion') or time.time())
    guardar(e)
    log(f'🎲 {n} apostadores en marcha con {capital} $ cada uno (simulado)')
    arrancar_hilo()
    return resumen()


def parar():
    e = estado()
    e['activo'] = False
    guardar(e)
    log('⏹ apostadores parados (las apuestas abiertas se resuelven igual)')
    return resumen()


def paso():
    e = estado()
    resolver()
    if not e.get('activo'):
        return
    ctx = contexto()
    if not ctx:
        return
    rnd = random.Random()
    for a in agentes():
        g = _g(a)
        ya = {r['slug'] for r in db.q("SELECT slug FROM pm_apuestas WHERE agente_id=? AND resultado IS NULL", (a['id'],))}
        for m in ctx:
            if m['slug'] in ya or m['activo'] not in g['activos'] or m['marco'] not in g['marcos']:
                continue
            d = decidir(g, m, rnd)
            if not d:
                continue
            usd = max(MIN_APUESTA, a['capital'] * g['tamano_pct'] / 100)
            r = comprar(m['libros'][d[0]]['asks'], usd)
            if not r:
                continue
            acc, coste, pm = r
            com = comision(acc, pm)
            if coste + com > a['capital']:
                continue
            db.ex('INSERT INTO pm_apuestas(agente_id,estrategia,slug,activo,marco,lado,precio,acciones,coste,comision,p_est,segs_rest,ref,inicio_precio,ts,fin) '
                  'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (a['id'], g['estrategia'], m['slug'], m['activo'], m['marco'], d[0], pm, acc, coste, com,
                                                            d[1], int(m['segs']), m['S'], m['K'], int(time.time()), m['fin']))
            db.ex('UPDATE pm_agentes SET capital=capital-?, apostado=apostado+?, ultimo=? WHERE id=?',
                  (coste + com, coste + com, f"{d[0]} {m['activo'].upper()} {m['marco']} a {pm:.2f}", a['id']))
            a = db.uno('SELECT * FROM pm_agentes WHERE id=?', (a['id'],))
            ya.add(m['slug'])
    if time.time() - e.get('ultima_evolucion', 0) > EVOLUCION:
        e['ultima_evolucion'] = time.time()
        guardar(e)
        evolucionar()


def resultado(slug):
    """'Up' | 'Down' según la resolución oficial de Polymarket, o None si aún no está."""
    if slug in _RES:
        return _RES[slug]
    d = _get(f'{GAMMA}/events?slug={slug}')
    if not d:
        return None
    m = d[0]['markets'][0]
    precios = json.loads(m.get('outcomePrices') or '[]')
    if not m.get('closed') or sorted(precios) != ['0', '1']:
        return None
    _RES[slug] = json.loads(m['outcomes'])[precios.index('1')]
    return _RES[slug]


def resolver():
    for slug in {r['slug'] for r in db.q('SELECT DISTINCT slug FROM pm_apuestas WHERE resultado IS NULL AND fin < ?', (int(time.time()) - 5,))}:
        try:
            res = resultado(slug)
        except Exception:
            continue
        if not res:
            continue
        for ap in db.q('SELECT * FROM pm_apuestas WHERE slug=? AND resultado IS NULL', (slug,)):
            gana = ap['lado'] == res
            pago = ap['acciones'] if gana else 0.0
            pnl = pago - ap['coste'] - ap['comision']
            db.ex('UPDATE pm_apuestas SET resultado=?, pago=?, pnl=?, resuelto=? WHERE id=?', (res, pago, pnl, int(time.time()), ap['id']))
            db.ex('UPDATE pm_agentes SET capital=capital+?, apuestas=apuestas+1, ganadas=ganadas+?, pnl=pnl+?, max_capital=MAX(max_capital, capital+?) WHERE id=?',
                  (pago, int(gana), pnl, pago, ap['agente_id']))
        log(f"✔ {slug}: ganó {res}")
    for a in agentes():  # sin dinero para la apuesta mínima y sin nada pendiente: quiebra → renace con el sistema de uno de los mejores
        if a['capital'] < MIN_APUESTA and not db.uno('SELECT 1 FROM pm_apuestas WHERE agente_id=? AND resultado IS NULL', (a['id'],)):
            _renacer(a, 'quiebra')


def _ranking():
    return sorted(agentes(), key=lambda a: a['capital'] + (db.uno('SELECT COALESCE(SUM(coste+comision),0) s FROM pm_apuestas WHERE agente_id=? AND resultado IS NULL',
                                                                    (a['id'],))['s']), reverse=True)


def _renacer(a, motivo, padre=None):
    rnd = random.Random()
    top = [x for x in _ranking() if x['id'] != a['id']][:max(1, len(agentes()) // 4)]
    p = padre or rnd.choice(top)
    g = mutar(_g(p), rnd)
    cap = (estado().get('capital') or CAPITAL)
    db.ex('UPDATE pm_agentes SET genoma=?, capital=?, generacion=generacion+1, padre=?, quiebras=quiebras+?, ultimo=? WHERE id=?',
          (json.dumps(g), cap, p['id'], int(motivo == 'quiebra'), f"{'💥 quiebra' if motivo == 'quiebra' else '🧬 relevo'}: hijo de {p['nombre']}", a['id']))
    return p


def evolucionar():
    rk = [a for a in _ranking() if a['apuestas'] >= 6]
    if len(rk) < 8:
        return
    k = len(rk) // 4
    mejores, peores = rk[:k], rk[-k:]
    for a, p in zip(peores, mejores):
        _renacer(a, 'relevo', p)
    texto = (f"Evolución: los mejores son " + ', '.join(f"{a['nombre']} ({_g(a)['estrategia']}, {a['capital']:.2f} $)" for a in mejores[:3]) +
             f"; sustituyo a {', '.join(a['nombre'] for a in peores)}.")
    db.ex('INSERT INTO pm_lecciones(ts,texto) VALUES(?,?)', (int(time.time()), texto))
    log('🧬 ' + texto)

# ---------------- hilo ----------------


def _bucle():
    while not _parar.is_set():
        t0 = time.time()
        try:
            paso()
        except Exception:
            log('error: ' + traceback.format_exc().splitlines()[-1])
        if not estado().get('activo') and not db.uno('SELECT 1 FROM pm_apuestas WHERE resultado IS NULL'):
            break
        _parar.wait(max(1, TICK - (time.time() - t0)))
    _hilo['t'] = None


def arrancar_hilo():
    if not (_hilo['t'] and _hilo['t'].is_alive()):
        _parar.clear()
        _hilo['t'] = threading.Thread(target=_bucle, daemon=True)
        _hilo['t'].start()

# ---------------- lo que se ve ----------------


def por_estrategia():
    out = {}
    for r in db.q('SELECT estrategia, COUNT(*) n, SUM(resultado=lado) g, AVG(precio) p, SUM(coste+comision) c, SUM(pago) pago, SUM(comision) com '
                  'FROM pm_apuestas WHERE resultado IS NOT NULL GROUP BY estrategia'):
        out[r['estrategia']] = {'apuestas': r['n'], 'acierto': r['g'] / r['n'], 'precio_medio': r['p'], 'roi': (r['pago'] - r['c']) / r['c'] if r['c'] else None,
                                'pnl': r['pago'] - r['c'], 'comisiones': r['com']}
    return out


def resumen():
    e = estado()
    ag = agentes()
    tot = db.uno('SELECT COUNT(*) n, COALESCE(SUM(resultado=lado),0) g, COALESCE(SUM(pago-coste-comision),0) pnl FROM pm_apuestas WHERE resultado IS NOT NULL') or {}
    return {'activo': bool(e.get('activo')), 'agentes': len(ag), 'capital_inicial': e.get('capital'), 'apuestas': tot.get('n', 0),
            'acierto': (tot['g'] / tot['n']) if tot.get('n') else None, 'pnl': tot.get('pnl'),
            'abiertas': (db.uno('SELECT COUNT(*) n FROM pm_apuestas WHERE resultado IS NULL') or {}).get('n', 0),
            'capital_total': sum(a['capital'] for a in ag), 'quiebras': sum(a['quiebras'] for a in ag), 'log': LOG[-30:]}


def mapa():
    """Todo lo del panel: mercados en vivo (precio de inicio, actual, probabilidad del modelo, precios de Up/Down), apostadores,
    apuestas recientes, resultados por estrategia y lecciones."""
    try:
        ctx = contexto()
    except Exception:
        ctx = []
    merc = [{'slug': m['slug'], 'activo': m['activo'].upper(), 'marco': m['marco'], 'K': m['K'], 'S': m['S'], 'segs': int(m['segs']),
             'p_up': m['p_up'], 'up': m['libros']['Up']['asks'][0][0] if m['libros']['Up']['asks'] else None,
             'down': m['libros']['Down']['asks'][0][0] if m['libros']['Down']['asks'] else None} for m in ctx]
    ag = [{'id': a['id'], 'nombre': a['nombre'], 'estrategia': _g(a)['estrategia'], 'genoma': _g(a), 'capital': a['capital'], 'apuestas': a['apuestas'],
           'ganadas': a['ganadas'], 'pnl': a['pnl'], 'generacion': a['generacion'], 'quiebras': a['quiebras'], 'max_capital': a['max_capital'],
           'ultimo': a['ultimo']} for a in _ranking()]
    ap = db.q('SELECT p.*, a.nombre FROM pm_apuestas p JOIN pm_agentes a ON a.id=p.agente_id ORDER BY p.id DESC LIMIT 40')
    return {**resumen(), 'mercados': merc, 'apostadores': ag, 'recientes': ap, 'estrategias': por_estrategia(),
            'lecciones': db.q('SELECT * FROM pm_lecciones ORDER BY id DESC LIMIT 15')}


def informe():
    r, est = resumen(), por_estrategia()
    if not r['apuestas']:
        return 'Up/Down (Polymarket, simulado): aún no se ha resuelto ninguna apuesta.' + ('' if r['activo'] else ' Está parado.')
    lin = [f"Up/Down (Polymarket, simulado): {r['agentes']} apostadores, {r['apuestas']} apuestas resueltas ({r['abiertas']} abiertas), "
           f"acierto {r['acierto'] * 100:.1f} %, resultado {r['pnl']:+.2f} $, {r['quiebras']} quiebras."]
    for k, v in sorted(est.items(), key=lambda x: -(x[1]['roi'] or -9)):
        # para no perder hay que acertar más que el precio medio pagado (más la comisión)
        lin.append(f"· {k}: {v['apuestas']} apuestas, acierto {v['acierto'] * 100:.1f} % (necesita > {v['precio_medio'] * 100:.0f} % + comisión), "
                   f"ROI {v['roi'] * 100:+.1f} %, comisiones {v['comisiones']:.2f} $")
    if 'azar' in est and any(k != 'azar' and (v['roi'] or -9) > (est['azar']['roi'] or -9) + 0.05 for k, v in est.items()):
        lin.append('Alguna estrategia supera claramente al azar; con más apuestas se verá si es ventaja real o suerte.')
    elif 'azar' in est:
        lin.append('Ninguna estrategia supera claramente al azar todavía: no hay ventaja demostrada.')
    return '\n'.join(lin)
