"""FÁBRICA DE ESTRATEGIAS (memecoins de Solana, solo simulado). Tres piezas:

1. MUESTRAS: cada 10 min fotografía el mercado (ranking de GMGN 5m/1h/24h + flujo smart money/KOL del laboratorio) y guarda
   por token sus rasgos. Pasadas 2 h baja las velas de 5 min y completa la muestra con rasgos de velas (solo velas ANTERIORES
   a la foto: sin mirar el futuro) y la RUTA de las 2 h siguientes (máximo, mínimo y cierre de cada vela, en % del precio).
   Al arrancar con la tabla vacía rellena unos días hacia atrás solo con velas (origen 'hist': tiene sesgo de supervivencia).
2. ESTRATEGIAS: reglas «rasgo > / < umbral» + salida (SL = k × ATR de 5 min, TP = rr × SL, tiempo máximo). Se simulan sobre
   las rutas con un supuesto de costes del 6 % ida y vuelta y una sola posición por token a la vez.
3. GENERADOR: algoritmo evolutivo que inventa y cruza reglas. Aprende con el 70 % más viejo de las muestras y se valida con el
   30 % más reciente; las que superan la validación entran en el SALÓN y desde ese momento se miden en VIVO (muestras que no
   existían cuando se inventaron), que es la única prueba que cuenta antes de darles dinero real."""
import json, math, random, threading, time, hashlib, bisect
import numpy as np
from . import conciencia, db, onchain, validacion_fabrica as VF, estres_fabrica as EF

CADA, CADA_EVO, HORIZONTE, BARRA, COSTES = 600, 1800, 7200, 300, 6.0
PASOS = HORIZONTE // BARRA  # 24 velas de 5 min
VERSION_VALIDACION = 'gmgn-causal-20261024-2'
ESQUEMA = """
CREATE TABLE IF NOT EXISTS fab_muestras (id INTEGER PRIMARY KEY, ts INTEGER, token TEXT, simbolo TEXT, origen TEXT, precio REAL,
  f TEXT, ruta TEXT, lista INTEGER DEFAULT 0, UNIQUE(token, ts));
CREATE INDEX IF NOT EXISTS ix_fab ON fab_muestras(lista, ts);
"""
# rasgos de la foto (ranking + smart money) y de las velas (se calculan al completar la muestra)
RASGOS = ['ch5m', 'ch1h', 'liq_k', 'mcap_k', 'holders', 'top10', 'rug', 'bundler', 'snipers', 'smart_cnt', 'kol_cnt', 'bots',
          'compras_ventas', 'edad_h', 'sm_n', 'sm_neto', 'ret4h', 'ch4h', 'rsi', 'atr', 'tend', 'volr']
MANUALES = [
    {'nombre': 'Retroceso en tendencia', 'conds': [['tend', '>', 0], ['ret4h', '<', -6], ['ret4h', '>', -25], ['rsi', '<', 52], ['rsi', '>', 35]], 'sl_k': 3, 'rr': 2, 'hold': 24},
    {'nombre': 'Ruptura con volumen', 'conds': [['ret4h', '>', -2], ['ch1h', '>', 8], ['volr', '>', 1.5]], 'sl_k': 3, 'rr': 2, 'hold': 12},
    {'nombre': 'Smart money compra', 'conds': [['sm_n', '>', 1.5], ['sm_neto', '>', 300], ['edad_h', '>', 1]], 'sl_k': 3, 'rr': 2, 'hold': 24},
    {'nombre': 'Smart money en lanzamiento', 'conds': [['sm_n', '>', 1.5], ['sm_neto', '>', 300], ['edad_h', '<', 1]], 'sl_k': 3, 'rr': 2.5, 'hold': 12},
    {'nombre': 'Rebote de sobreventa', 'conds': [['rsi', '<', 25], ['ch4h', '<', -15], ['volr', '>', 1]], 'sl_k': 2.5, 'rr': 1.5, 'hold': 12},
    {'nombre': 'Recién graduada con fuerza', 'conds': [['edad_h', '<', 3], ['liq_k', '>', 30], ['compras_ventas', '>', 1.2], ['top10', '<', 0.35]], 'sl_k': 3, 'rr': 2.5, 'hold': 18},
]
# para dar dinero real a una estrategia tiene que ganar EN VIVO (datos que no existían al diseñarla o descubrirla)
PROMO = {'vivo_n': 30, 'vivo_medio': 1.5, 'vivo_gana': 35, 'dias': 5, 'tokens': 3, 'concentracion_maxima': 0.5}
# Variantes registradas antes de observar su cohorte futura; no se declaran ganadoras.
MANUALES += [
    {'nombre':'Tendencia con volumen y ATR limitado','conds':[['tend','>',0],['volr','>',1.2],['atr','<',6]],'sl_k':3,'rr':2,'hold':12},
    {'nombre':'Ruptura con tendencia confirmada','conds':[['ret4h','>',-2],['ch1h','>',8],['volr','>',1.5],['tend','>',0],['atr','<',5]],'sl_k':3,'rr':2.5,'hold':12},
    {'nombre':'Retroceso con flujo comprador','conds':[['ret4h','<',-6],['ret4h','>',-25],['sm_neto','>',300],['tend','>',0]],'sl_k':3,'rr':2,'hold':24},
]
_hilo, _ultimo = {'t': None}, {'evo': 0, 'msg': '', 'gen': 0}


def preparar():
    with db.transaccion():
        for statement in ESQUEMA.split(';'):
            if statement.strip():db.ex(statement)
        VF.preparar(MANUALES, version, VERSION_VALIDACION)

# ---------------- muestras ----------------


def _num(x, d=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def foto():
    """Guarda una muestra por token del ranking (5m, 1h y 24h) con sus rasgos de la foto."""
    from . import laboratorio_ia
    filas = {}
    for iv in ('5m', '1h', '24h'):
        try:
            d = onchain._api('/v1/market/rank', chain='sol', interval=iv, limit=100, order_by='volume', direction='desc',
                             filters=['not_wash_trading'], min_liquidity=10_000, min_volume=20_000)
            for t in (d or {}).get('rank') or []:
                filas.setdefault(t['address'], t)
        except onchain.ErrorOnchain:
            pass
    sm = {}
    for ts, tok, usd, w, _ in list(laboratorio_ia._feed.values()):
        if time.time() - ts <= 3600:
            a = sm.setdefault(tok, [set(), 0.0])
            a[1] += usd
            if usd > 0:
                a[0].add(w)
    ahora = int(time.time())
    n = 0
    for tok, t in filas.items():
        if tok in laboratorio_ia.NO_MEME or not _num(t.get('price')):
            continue
        b, s = _num(t.get('buys'), 0), _num(t.get('sells'), 0)
        f = {'foto_version': VERSION_VALIDACION, 'ch5m': _num(t.get('price_change_percent5m')), 'ch1h': _num(t.get('price_change_percent1h')),
             'liq_k': _num(t.get('liquidity'), 0) / 1e3, 'mcap_k': _num(t.get('market_cap'), 0) / 1e3, 'holders': _num(t.get('holder_count')),
             'top10': _num(t.get('top_10_holder_rate')), 'rug': _num(t.get('rug_ratio')), 'bundler': _num(t.get('bundler_rate')),
             'snipers': _num(t.get('sniper_count')), 'smart_cnt': _num(t.get('smart_degen_count')), 'kol_cnt': _num(t.get('renowned_count')),
             'bots': _num(t.get('bot_degen_rate')), 'compras_ventas': b / s if s else None,
             'edad_h': (time.time() - (t.get('open_timestamp') or t.get('creation_timestamp') or time.time())) / 3600,
             'sm_n': len(sm.get(tok, [()])[0]), 'sm_neto': round(sm.get(tok, [0, 0.0])[1])}
        try:
            db.ex('INSERT OR IGNORE INTO fab_muestras(ts,token,simbolo,origen,precio,f) VALUES(?,?,?,?,?,?)',
                  (ahora, tok, t.get('symbol') or '', 'vivo', float(t['price']), json.dumps(f)))
            n += 1
        except Exception:
            pass
    return n


def _rasgos_velas(v, i, p):
    """Rasgos con las velas ANTERIORES a la i (la foto es al abrir la vela i)."""
    pre = v[max(0, i - 48):i]
    if len(pre) < 12:
        return {}
    c = [x['c'] for x in pre]
    g = [max(0, b - a) for a, b in zip(c[-15:-1], c[-14:])]
    pe = [max(0, a - b) for a, b in zip(c[-15:-1], c[-14:])]
    e, k = c[0], 2 / 21
    for x in c[1:]:
        e = x * k + e * (1 - k)
    vol_reciente = sum(x['v'] for x in pre[-6:]) / 6
    vol_antes = sum(x['v'] for x in pre[:-6]) / max(1, len(pre) - 6)
    return {'ret4h': (p / max(x['h'] for x in pre) - 1) * 100, 'ch4h': (p / pre[0]['o'] - 1) * 100,
            'rsi': 100 - 100 / (1 + (sum(g) / 14) / ((sum(pe) / 14) or 1e-12)) if len(c) >= 15 else None,
            'atr': sum((x['h'] - x['l']) / x['c'] for x in pre[-12:]) / len(pre[-12:]) * 100,
            'tend': (p / e - 1) * 100, 'volr': vol_reciente / vol_antes if vol_antes else None}


def _ruta(v, i, p):
    fut = v[i:i + PASOS]
    return [[round((x['h'] / p - 1) * 100, 2), round((x['l'] / p - 1) * 100, 2), round((x['c'] / p - 1) * 100, 2)] for x in fut]


def completar(max_tokens=30):
    """Pone rasgos de velas y ruta a las muestras de hace más de 2 h (una petición de velas por token)."""
    lim = int(time.time()) - HORIZONTE - BARRA
    toks = db.q('SELECT token, simbolo, MIN(ts) a, MAX(ts) b FROM fab_muestras WHERE lista=0 AND ts<? GROUP BY token LIMIT ?', (lim, max_tokens))
    hechos, lote = 0, []
    for t in toks:
        ap = {'token': t['token'], 'simbolo': t['simbolo'], 'n': 0, 'mx': 0.0, 'ci': 0.0}
        filas = db.q('SELECT id, ts, precio, f FROM fab_muestras WHERE lista=0 AND token=? AND ts<?', (t['token'], lim))
        try:
            s = onchain.registrar('SOL', t['token'], t['simbolo'])
            v = onchain.velas(s, '5m', inicio=t['a'] - 4 * 3600 - BARRA, fin=t['b'] + HORIZONTE + BARRA)
        except onchain.ErrorOnchain as e:
            if 'límite' in str(e):
                break
            v = []
        idx = {x['t']: n for n, x in enumerate(v)}
        for fl in filas:
            i = idx.get((fl['ts'] + BARRA - 1) // BARRA * BARRA)  # primera vela que abre en o tras la foto
            if i is None or len(v) - i < PASOS // 2:
                # sin velas: si ya pasó mucho, se descarta
                if fl['ts'] < lim - 6 * 3600:
                    db.ex('UPDATE fab_muestras SET lista=-1 WHERE id=?', (fl['id'],))
                continue
            # Una foto entre aperturas no puede conocer el cierre de la vela en curso.
            previo = bisect.bisect_right([x['t'] for x in v], fl['ts'] - BARRA)
            original = json.loads(fl['f'])
            f = {**original, **_rasgos_velas(v, previo, fl['precio']),
                 'version_temporal': VERSION_VALIDACION if original.get('foto_version') == VERSION_VALIDACION else 'foto_legacy_recalculada',
                 'rasgos_hasta': v[previo - 1]['t'] + BARRA if previo else None}
            ru = _ruta(v, i, fl['precio'])
            db.ex('UPDATE fab_muestras SET f=?, ruta=?, lista=1 WHERE id=?', (json.dumps(f), json.dumps(ru), fl['id']))
            hechos += 1
            ap['n'] += 1
            ap['mx'] += max(x[0] for x in ru)
            ap['ci'] += ru[-1][2]
        if ap['n']:
            lote.append({'token': ap['token'], 'simbolo': ap['simbolo'], 'n': ap['n'],
                         'max_med': round(ap['mx'] / ap['n'], 1), 'cierre_med': round(ap['ci'] / ap['n'], 1)})
        time.sleep(0.5)
    try:
        conciencia.velas(lote)
    except Exception:
        pass
    return hechos


def relleno_historico(dias=3, cada=1800, n_tokens=60):
    """Primer arranque: muestras de los últimos días solo con velas, para que el generador no empiece de cero."""
    try:
        d = onchain._api('/v1/market/rank', chain='sol', interval='24h', limit=100, order_by='volume', direction='desc',
                         filters=['not_wash_trading'], min_liquidity=50_000, min_volume=200_000)
    except onchain.ErrorOnchain:
        return 0
    from . import laboratorio_ia
    n = 0
    for t in ((d or {}).get('rank') or [])[:n_tokens]:
        if t['address'] in laboratorio_ia.NO_MEME:
            continue
        try:
            s = onchain.registrar('SOL', t['address'], t.get('symbol'), t.get('name'))
            v = onchain.velas(s, '5m', inicio=int(time.time()) - dias * 86400, fin=int(time.time()))
        except onchain.ErrorOnchain:
            time.sleep(5)
            continue
        for i in range(48, len(v) - PASOS, cada // BARRA):
            p = v[i]['o']
            if p <= 0:
                continue
            f = {'edad_h': (v[i]['t'] - (t.get('open_timestamp') or v[0]['t'])) / 3600, **_rasgos_velas(v, i, p)}
            db.ex('INSERT OR IGNORE INTO fab_muestras(ts,token,simbolo,origen,precio,f,ruta,lista) VALUES(?,?,?,?,?,?,?,1)',
                  (v[i]['t'], t['address'], t.get('symbol') or '', 'hist', p, json.dumps(f), json.dumps(_ruta(v, i, p))))
            n += 1
        time.sleep(1)
    return n

# ---------------- simulación ----------------


class Datos:
    """Muestras listas en matrices: rasgos (NaN si falta), ruta (máx/mín/cierre por vela), ts y token."""

    def __init__(self):
        filas = db.q('SELECT ts, token, origen, f, ruta FROM fab_muestras WHERE lista=1 ORDER BY ts')
        # Conservar las filas en SQLite; sus indicadores antiguos no acreditan evidencia causal.
        self.cuarentena_temporal = sum(r['origen'] == 'vivo' and json.loads(r['f']).get('version_temporal') != VERSION_VALIDACION for r in filas)
        filas = [r for r in filas if r['origen'] != 'vivo' or json.loads(r['f']).get('version_temporal') == VERSION_VALIDACION]
        self.version_modelo = VF.VERSION
        self.n = len(filas)
        self.ts = np.array([r['ts'] for r in filas], dtype=np.int64)
        tok = {}
        self.tok = np.array([tok.setdefault(r['token'], len(tok)) for r in filas], dtype=np.int64)
        self.tokens = list(tok)
        self.vivo = np.array([r['origen'] == 'vivo' for r in filas], dtype=bool)
        self.F = np.full((self.n, len(RASGOS)), np.nan)
        self.H = np.full((self.n, PASOS), np.nan)
        self.L = np.full((self.n, PASOS), np.nan)
        self.C = np.full((self.n, PASOS), np.nan)
        self.longitudes = np.zeros(self.n, dtype=int)
        self.simulables = np.ones(self.n, dtype=bool)
        for k, r in enumerate(filas):
            f = json.loads(r['f'])
            if r['origen'] == 'vivo':
                hasta = f.get('rasgos_hasta')
                self.simulables[k] = _seguridad_foto(f) and not isinstance(hasta, bool) and isinstance(hasta, (int, float)) and math.isfinite(hasta) and hasta <= r['ts']
            for j, nom in enumerate(RASGOS):
                x = f.get(nom)
                if x is not None:
                    self.F[k, j] = x
            ru = json.loads(r['ruta'] or '[]')[:PASOS]
            self.longitudes[k] = len(ru)
            if ru:
                a = np.array(ru, dtype=float)
                self.H[k, :len(a)], self.L[k, :len(a)], self.C[k, :len(a)] = a[:, 0], a[:, 1], a[:, 2]
        # Relleno vectorial; longitudes impide certificar cierres sobre valores extrapolados.
        for M in (self.H, self.L, self.C):
            for j in range(1, PASOS):
                hueco = np.isnan(M[:, j])
                M[hueco, j] = M[hueco, j - 1]
        policy = VF.politica()
        self.corte = policy.get('entreno_hasta', int(np.quantile(self.ts, 0.7)) if self.n else 0)
        self.seleccion_hasta = policy.get('seleccion_hasta', float('inf'))
        self.reservado_desde = policy.get('reservado_desde', 0)
        self.col = {nom: j for j, nom in enumerate(RASGOS)}


def simular(e, D, mascara=None, con_idx=False, diagnostico=False):
    """Operaciones de la estrategia e: lista de (ts, resultado neto %) [, índice de la muestra]."""
    m = np.ones(D.n, dtype=bool) if mascara is None else mascara.copy()
    if hasattr(D, 'simulables'):
        m &= D.simulables
    atr_todos = D.F[:, D.col['atr']]
    m &= np.isfinite(atr_todos) & (atr_todos > 0)
    for nom, op, u in e['conds']:
        x = D.F[:, D.col[nom]]
        with np.errstate(invalid='ignore'):
            m &= (x > u) if op == '>' else (x < u)
    idx = np.nonzero(m)[0]
    if not len(idx):
        return {'ops': [], 'incompletas': []} if diagnostico else []
    atr = D.F[idx, D.col['atr']]
    lo, hi = getattr(D, 'sl_lim', (4, 30))
    sl = np.clip(e['sl_k'] * atr, lo, hi)
    tp = sl * e['rr']
    h = int(e['hold'])
    if h < 1 or h > D.C.shape[1]:
        raise ValueError('horizonte fuera de la ruta disponible')
    L, H, C = D.L[idx, :h], D.H[idx, :h], D.C[idx, :h]
    if e.get('lado', 1) == -1:  # corto: la ruta se ve al revés
        L, H, C = -H, -L, -C
    toca_sl = L <= -sl[:, None]
    toca_tp = H >= tp[:, None]
    i_sl = np.where(toca_sl.any(1), toca_sl.argmax(1), h)
    i_tp = np.where(toca_tp.any(1), toca_tp.argmax(1), h)
    res = np.where(i_sl <= i_tp, np.where(i_sl < h, -sl, np.nan), tp)  # si la misma vela toca los dos: stop (lo peor)
    costes = getattr(D, 'costes', None)
    res = np.where(np.isnan(res), C[:, -1], res) - (COSTES if costes is None else costes[idx])
    salida = np.minimum(np.minimum(i_sl, i_tp), h - 1)
    longitudes = getattr(D, 'longitudes', np.full(D.n, D.C.shape[1]))[idx]
    validas = np.isfinite(L) & np.isfinite(H) & np.isfinite(C) & (L <= C) & (C <= H)
    prefijo = np.where(validas.all(1), h, (~validas).argmax(1))
    conocidas = np.minimum(longitudes, prefijo)
    ops, incompletas, libre = [], [], {}
    for k, n in enumerate(idx):  # una posición por token a la vez
        tk = D.tok[n]
        if D.ts[n] < libre.get(tk, 0):
            continue
        if salida[k] >= conocidas[k] or not np.isfinite(res[k]):
            libre[tk] = D.ts[n] + h * BARRA
            incompletas.append((int(D.ts[n]), int(n)))
            continue
        libre[tk] = D.ts[n] + (salida[k] + 1) * BARRA
        ops.append((int(D.ts[n]), float(res[k]), int(n)) if con_idx else (int(D.ts[n]), float(res[k])))
    return {'ops': ops, 'incompletas': incompletas} if diagnostico else ops


def stats(ops, incompletas=0):
    if not ops:
        return {'n': 0, 'medio': None, 'total': 0, 'gana_pct': None, 'media_exacta': None, 'ganadoras': 0, 'incompletas': incompletas}
    r = [x for _, x in ops]
    ganadoras = sum(x > 0 for x in r)
    return {'n': len(r), 'medio': round(sum(r) / len(r), 2), 'total': round(sum(r), 1), 'gana_pct': round(100 * ganadoras / len(r)), 'media_exacta': sum(r) / len(r), 'ganadoras': ganadoras, 'incompletas': incompletas}


def evaluar(e, D, desde=None, solo_seleccion=False):
    masks = {'entreno': D.ts + e['hold'] * BARRA < D.corte, 'prueba': (D.ts >= D.corte) & (D.ts + e['hold'] * BARRA < getattr(D, 'seleccion_hasta', float('inf'))),
             'vivo': D.vivo & (D.ts >= max(desde, getattr(D, 'reservado_desde', 0))) if desde is not None else np.zeros(D.n, dtype=bool), 'todo': None}
    if solo_seleccion:
        masks['vivo'] = np.zeros(D.n, dtype=bool)
        masks['todo'] = D.ts + e['hold'] * BARRA < getattr(D, 'seleccion_hasta', float('inf'))
    out = {}
    for key, mask in masks.items():
        result = simular(e, D, mask, diagnostico=True)
        out[key] = stats(result['ops'], len(result['incompletas']))
    forward = masks['vivo']
    operations = simular(e, D, forward, con_idx=True)
    out['diversidad'] = VF.diversidad(operations, D)
    out['estres'] = {}
    if getattr(D, 'version_modelo', None) == VF.VERSION:
        for profile in EF.ESCENARIOS:
            values = EF.resultados(e, D, operations, profile, COSTES)
            out['estres'][profile] = stats([(t, x) for t, x, _ in values], max(0, len(operations)-len(values)))
    return out


def _aptitud(e, D):
    result = simular(e, D, D.ts + e['hold'] * BARRA < D.corte, diagnostico=True)
    if result['incompletas']:
        return -99
    tr = [x for t, x in result['ops']]
    if len(tr) < 15:
        return -99
    m, s = sum(tr) / len(tr), float(np.std(tr)) or 1
    return m / s * math.sqrt(len(tr))  # t de Student: gana de forma constante, no por una sola operación

# ---------------- generador ----------------


def _umbral(D, nom):
    mascara = D.ts < D.corte if hasattr(D, 'ts') else np.arange(len(D.F)) < D.corte
    if hasattr(D,'simulables'):mascara &= D.simulables
    x = D.F[mascara, D.col[nom]]
    x = x[np.isfinite(x)]
    return round(float(np.quantile(x, random.uniform(0.05, 0.95))), 4) if len(x) > 20 else None


def _base(e):
    return {k: e[k] for k in ('conds', 'sl_k', 'rr', 'hold', 'lado') if k in e}


def version(e):
    return hashlib.sha256(json.dumps(_base(e), sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _al_azar(D):
    e = {'conds': [], 'sl_k': round(random.uniform(1.5, 6), 2), 'rr': round(random.uniform(1, 4), 2), 'hold': random.choice(getattr(D, 'holds', [6, 12, 18, 24]))}
    if getattr(D, 'cortos', False):
        e['lado'] = random.choice([1, -1])
    for nom in random.sample(getattr(D, 'rasgos', RASGOS), random.randint(1, 3)):
        u = _umbral(D, nom)
        if u is not None:
            e['conds'].append([nom, random.choice('<>'), u])
    return e


def _mutar(e, D):
    e = json.loads(json.dumps(_base(e)))
    r = random.random()
    if r < 0.4 and e['conds']:
        c = random.choice(e['conds'])
        c[2] = _umbral(D, c[0]) if random.random() < 0.5 else round(c[2] * random.uniform(0.8, 1.2), 4)
    elif r < 0.55 and len(e['conds']) < 5:
        nom = random.choice(getattr(D, 'rasgos', RASGOS))
        u = _umbral(D, nom)
        if u is not None:
            e['conds'].append([nom, random.choice('<>'), u])
    elif r < 0.65 and len(e['conds']) > 1:
        e['conds'].pop(random.randrange(len(e['conds'])))
    elif r < 0.7 and getattr(D, 'cortos', False):
        e['lado'] = -e.get('lado', 1)
    else:
        k = random.choice(['sl_k', 'rr', 'hold'])
        e[k] = random.choice(getattr(D, 'holds', [6, 12, 18, 24])) if k == 'hold' else round(max(0.8, e[k] * random.uniform(0.75, 1.3)), 2)
    return e


def _cruzar(a, b):
    conds, vistos = [], set()
    for c in a['conds'] + b['conds']:
        if random.random() < 0.5 and (c[0], c[1]) not in vistos and len(conds) < 5:
            vistos.add((c[0], c[1]))
            conds.append(c)
    conds = conds or a['conds'][:1]
    h = {'conds': json.loads(json.dumps(conds)), 'sl_k': random.choice([a, b])['sl_k'], 'rr': random.choice([a, b])['rr'], 'hold': random.choice([a, b])['hold']}
    if 'lado' in a:
        h['lado'] = a['lado']
    return h


def describir(e):
    return ('CORTO: ' if e.get('lado') == -1 else 'LARGO: ' if e.get('lado') == 1 else '') + ' y '.join(f"{n} {o} {u:g}" for n, o, u in e['conds']) + f" → SL {e['sl_k']:g}×ATR, TP {e['rr']:g}×SL, máx. {e['hold'] * 5} min"


def evolucionar(poblacion=160, generaciones=20, D=None, manuales=None, salon_clave='fabrica_salon', ultimo=None, grabar=None):
    """Una ronda del generador. Por defecto, memecoins; la fábrica de futuros pasa sus datos, manuales, salón y estado."""
    if D is None:
        VF.preparar(MANUALES, version, VERSION_VALIDACION)
        D = Datos()
    if getattr(D,'version_modelo',None)==VF.VERSION and not VF.es_valida(VF.politica()):
        (ultimo if ultimo is not None else _ultimo)['msg']='Esperando datos suficientes para registrar cortes de validacion'
        return None
    manuales = MANUALES if manuales is None else manuales
    ultimo = _ultimo if ultimo is None else ultimo
    if D.n < 300:
        ultimo['msg'] = f'Esperando datos: {D.n} muestras listas (mínimo 300)'
        return None
    salon = db.ajuste(salon_clave) or []
    pob = [json.loads(json.dumps(x)) for x in salon] + [_base(m) for m in manuales]
    pob += [_al_azar(D) for _ in range(poblacion - len(pob))]
    for g in range(generaciones):
        punt = sorted(((_aptitud(e, D), i) for i, e in enumerate(pob)), reverse=True)
        if getattr(D, 'version_modelo', None) == VF.VERSION:
            VF.registrar_busqueda(len(pob))
        elite = [pob[i] for _, i in punt[:poblacion // 8]]
        hijos = list(elite)
        while len(hijos) < poblacion:
            a, b = random.sample(elite, 2) if len(elite) > 1 else (elite[0], elite[0])
            h = _cruzar(a, b) if random.random() < 0.5 else _mutar(a, D)
            hijos.append(_mutar(h, D) if random.random() < 0.3 else h)
        pob = hijos
    # al salón entran las que también ganan con datos que no vieron (el 30 % reciente), sin repetir reglas
    ahora, nuevo, vistos = int(time.time()), [], set()
    for e in salon + pob:
        if len({(c[0], c[1]) for c in e['conds']}) < len(e['conds']):
            continue
        seleccion = D.ts + e['hold'] * BARRA < getattr(D, 'seleccion_hasta', float('inf'))
        clave = tuple((t, n, round(res, 8)) for t, res, n in simular(e, D, mascara=seleccion, con_idx=True))
        if clave in vistos:
            continue
        vistos.add(clave)
        ev = evaluar(e, D, e.get('desde'), solo_seleccion=True)
        if ev['entreno']['n'] >= 15 and (ev['entreno']['media_exacta'] or 0) > 0 and ev['prueba']['n'] >= 8 and (ev['prueba']['media_exacta'] or 0) > 0 and not ev['entreno']['incompletas'] and not ev['prueba']['incompletas']:
            huella = version(e)
            congelada = e.get('version') == huella and e.get('validacion') == getattr(D, 'version_modelo', None)
            nuevo.append({**_base(e), 'version': huella, 'desde': e.get('desde') if congelada else ahora,
                          'validacion': getattr(D, 'version_modelo', None),
                          'id': e.get('id') if congelada else f"{getattr(D, 'prefijo', 'G')}-{ahora}-{huella[:12]}"})
    nuevo.sort(key=lambda e: -_aptitud(e, D))
    db.poner_ajuste(salon_clave, nuevo[:20])
    ultimo.update(evo=ahora, gen=ultimo['gen'] + 1, msg=f'{D.n} muestras · {len(nuevo)} estrategias pasan la validación')
    try:
        (grabar or _grabar_ronda)(D, pob, nuevo, poblacion * generaciones)
    except Exception as ex:
        ultimo['msg'] += f' (conciencia: {str(ex)[:60]})'
    return nuevo


def _grabar_ronda(D, pob, nuevo, probadas):
    """Lo simulado en la ronda: salón + manuales + las 5 mejores de la población (con sus operaciones simuladas)."""
    sim = {r['token']: r['simbolo'] for r in db.q('SELECT DISTINCT token, simbolo FROM fab_muestras')}
    ini = _inicio_vivo(D)
    mejores = sorted(pob, key=lambda e: -_aptitud(e, D))[:5]
    lista = ([{**e, 'nombre': f"Generada {e['id']}"} for e in nuevo[:10]]
             + [{**m, 'id': f'M{i + 1}', 'desde': VF.inicio_manual(f'M{i + 1}', m, version)} for i, m in enumerate(MANUALES)]
             + [{**e, 'id': 'P' + str(abs(hash(describir(e))) % 10 ** 6), 'nombre': 'Candidata de la población'} for e in mejores])
    top, vistos = [], set()
    for e in lista:
        if e['id'] in vistos:
            continue
        vistos.add(e['id'])
        ops = simular(e, D, con_idx=True)
        st = evaluar(e, D, e.get('desde'))
        top.append({'id': e['id'], 'nombre': e.get('nombre'), 'regla': describir(e), **{k: st[k] for k in ('entreno', 'prueba', 'vivo')},
                    'ops': [{'token': D.tokens[D.tok[n]], 'simbolo': sim.get(D.tokens[D.tok[n]], ''), 'ts': t, 'res': round(x, 2)} for t, x, n in ops[-200:]]})
    conciencia.ronda({'gen': _ultimo['gen'], 'muestras': D.n, 'vivas': int(D.vivo.sum()), 'tokens': len(D.tokens),
                      'probadas': probadas, 'pasan': len(nuevo), 'top': top})
    ya = set(db.ajuste('con_promovidas') or [])
    prom = promovidas()
    for e in prom:
        if e['id'] not in ya:
            conciencia.promocion({**e, 'regla': describir(e)})
    db.poner_ajuste('con_promovidas', sorted(ya | {e['id'] for e in prom}))


def _inicio_vivo(D):
    return int(D.ts[D.vivo].min()) if D.vivo.any() else None


def _candidatas(D):
    ini = _inicio_vivo(D)
    return ([{**m, 'id': f'M{i + 1}', 'desde': VF.inicio_manual(f'M{i + 1}', m, version)} for i, m in enumerate(MANUALES)]
            + [{**e, 'nombre': f"Generada {e['id']}"} for e in db.ajuste('fabrica_salon') or []])


def promovidas():
    """Estrategias con ventaja demostrada en vivo, de mejor a peor (cacheado 5 min)."""
    def calc():
        if not VF.es_valida(VF.politica()):return []
        D, out = Datos(), []
        for e in _candidatas(D):
            if not e.get('desde') or (not str(e['id']).startswith('M') and (e.get('version') != version(e) or e.get('validacion') != VF.VERSION)):
                continue
            ev = evaluar(e, D, e['desde'])
            v = ev['vivo']
            if (v['n'] >= PROMO['vivo_n'] and (v['media_exacta'] or 0) >= PROMO['vivo_medio'] and 100 * v['ganadoras'] / v['n'] >= PROMO['vivo_gana']
                    and (ev['prueba']['media_exacta'] or 0) > 0 and not v['incompletas'] and not ev['prueba']['incompletas']
                    and ev['diversidad']['dias'] >= PROMO['dias'] and ev['diversidad']['tokens'] >= PROMO['tokens']
                    and ev['diversidad']['concentracion_maxima'] <= PROMO['concentracion_maxima']
                    and set(ev['estres']) == set(EF.ESCENARIOS)
                    and all(x['n'] >= PROMO['vivo_n'] and not x['incompletas'] and (x['media_exacta'] or 0) > 0 for x in ev['estres'].values())):
                out.append({**e, 'vivo': v})
        return sorted(out, key=lambda e: -e['vivo']['medio'] * math.sqrt(e['vivo']['n']))
    return onchain._cacheado(('fab_promo',), 300, calc)


def _cumple(f, c):
    x = f.get(c[0])
    return x is not None and (x > c[2] if c[1] == '>' else x < c[2])


def _seguridad_foto(f):
    values = [f.get(k) for k in ('liq_k', 'top10', 'rug')]
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in values):
        return False
    return values[0] >= 30 and 0 <= values[1] <= 0.45 and 0 <= values[2] <= 0.3


def senales_ahora(estrategias, evitar=(), max_velas=15):
    """Tokens de la última foto (≤15 min) que cumplen ahora alguna estrategia promovida. Los rasgos de velas se calculan
    en el momento, solo para los tokens que ya pasan las condiciones de la foto."""
    ult = db.uno("SELECT MAX(ts) t FROM fab_muestras WHERE origen='vivo'")
    if not ult or not ult['t'] or time.time() - ult['t'] > 900:
        return []
    filas = db.q("SELECT token, simbolo, precio, f FROM fab_muestras WHERE origen='vivo' AND ts=?", (ult['t'],))
    velas_, out, usados = {}, [], set()
    for e in estrategias:
        for fl in filas:
            if fl['token'] in evitar or fl['token'] in usados:
                continue
            f = json.loads(fl['f'])
            if not _seguridad_foto(f):
                continue
            if not all(_cumple(f, c) for c in e['conds'] if c[0] in f):
                continue
            if fl['token'] not in velas_:
                if len(velas_) >= max_velas:
                    continue
                try:
                    v = onchain.velas(onchain.registrar('SOL', fl['token'], fl['simbolo']), '5m')
                    velas_[fl['token']] = _rasgos_velas(v, len(v) - 1, fl['precio'])  # sin la vela que aún se está formando
                except onchain.ErrorOnchain:
                    velas_[fl['token']] = {}
            f.update(velas_[fl['token']])
            if not all(_cumple(f, c) for c in e['conds']) or not f.get('atr'):
                continue
            sl = float(np.clip(e['sl_k'] * f['atr'], 4, 30))
            usados.add(fl['token'])
            out.append({'token': fl['token'], 'simbolo': fl['simbolo'], 'precio': fl['precio'], 'atr': round(f['atr'], 2), 'pts': 0,
                        'estrategia': e['nombre'], 'est_id': e['id'], 'vivo': e['vivo'], 'regla': describir(e),
                        'sl': round(sl, 1), 'tp': round(sl * e['rr'], 1), 'hold': int(e['hold'])})
    return out


def resumen():
    return onchain._cacheado(('fabrica',), 60, _resumen)


def _resumen():
    try:
        D = Datos()
    except Exception as ex:
        return {'error': str(ex)[:120]}
    man = [{'nombre': m['nombre'], 'regla': describir(m), **evaluar(m, D, VF.inicio_manual(f'M{i + 1}', m, version))} for i,m in enumerate(MANUALES)]
    salon = [{'id': e['id'], 'regla': describir(e), 'desde': e['desde'], **evaluar(e, D, e['desde'])} for e in (db.ajuste('fabrica_salon') or [])[:10]]
    cnt = {r['lista']: r['n'] for r in db.q('SELECT lista, COUNT(*) n FROM fab_muestras GROUP BY lista')}
    return {'validacion': VF.politica(), 'escenarios': EF.ESCENARIOS, 'muestras': {'listas': cnt.get(1, 0), 'pendientes': cnt.get(0, 0), 'vivas': int(D.vivo.sum()), 'cuarentena_temporal': D.cuarentena_temporal, 'tokens': int(len(set(D.tok.tolist())))},
            'corte': D.corte, 'manuales': man, 'promo': PROMO,
            'promovidas': [{'id': e['id'], 'nombre': e['nombre'], 'regla': describir(e), 'vivo': e['vivo']} for e in promovidas()], 'salon': salon, 'evo': _ultimo['evo'], 'rondas': _ultimo['gen'], 'msg': _ultimo['msg']}


def _bucle():
    if not db.uno('SELECT 1 x FROM fab_muestras LIMIT 1'):
        try:
            _ultimo['msg'] = 'Rellenando histórico de 3 días…'
            n = relleno_historico()
            _ultimo['msg'] = f'Histórico: {n} muestras'
        except Exception as ex:
            _ultimo['msg'] = f'Histórico falló: {ex}'[:120]
    while True:
        t0 = time.time()
        for fn in (foto, completar):
            try:
                fn()
            except Exception:
                pass
        if time.time() - _ultimo['evo'] > CADA_EVO:
            try:
                evolucionar()
            except Exception as ex:
                _ultimo['msg'] = f'Evolución falló: {ex}'[:120]
                _ultimo['evo'] = time.time()
        time.sleep(max(30, CADA - (time.time() - t0)))


def arrancar():
    preparar()
    conciencia.preparar()
    _ultimo['gen'] = max(_ultimo['gen'], conciencia.rondas('memes'))
    if not (_hilo['t'] and _hilo['t'].is_alive()):
        _hilo['t'] = threading.Thread(target=_bucle, daemon=True)
        _hilo['t'].start()
