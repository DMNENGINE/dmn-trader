"""Enjambre de agentes autónomos de APOLO: N agentes, cada uno con su cuenta de microoperaciones simuladas (p. ej. 1 $ → 100 $).
Cada agente: opera con su genoma → autopsia de cada pérdida con su arreglo → si quiebra, prueba soluciones en datos reales recientes.
El enjambre: comparte lo que aprende (símbolos/horas que pierden para todos), evoluciona cada 2 h (el 20 % peor se reemplaza por hijos
del 10 % mejor) y, si hay varios equipos (Pi = central, PC = nodo), intercambia los mejores genomas cada 30 min (modelo de islas)."""
import bisect, copy, json, math, os, random, socket, statistics, sys, threading, time, traceback
from . import analista, flujo, ia, sentimiento, datos, db, indicadores as ind, motor

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
    'bollinger': {'n': 20, 'k': 2.0}, 'impulso': {'n': 5, 'umbral': 0.25}, 'rejilla': {'n': 60, 'niveles': 5},
}
GENOMA_BASE = {'marco': '1m', 'estrategia': 'ema_cruce', 'p': dict(ESTRATEGIAS['ema_cruce']), 'stop_atr': 1.5, 'rr': 1.5, 'filtro_tendencia': False,
               'vol_min_pct': 0.0, 'vol_max_pct': 3.0, 'riesgo_pct': 20.0, 'max_barras': 30, 'be_en_r': 0.0, 'min_ventaja': 2.0,
               'cortos': True, 'horas_bloqueadas': [], 'simbolos_bloqueados': [],
               'entrada': 'limite', 'retroceso': 0.3, 'espera': 3, 'tramos': 1,
               'enfriar': 2, 'guardia': 3, 'roi_min': None, 'trail_r': 0.0, 'trail_frac': 0.8, 'filtro_ia': 0.0, 'noticias': 0,
               'parcial_r': 1.0, 'parcial_frac': 0.5, 'sin_avance': 0, 'regimen': 1, 'confirmar_1h': 0, 'flujo': 0, 'veto_llm': 0}
# parcial_r: en +parcial_r R cierra parcial_frac de la posición y sube el stop a la entrada (no devolver lo ganado); 0 = no.
# sin_avance: si en N velas no ha ido ni +0,3R a favor, cierra (stop por tiempo); 0 = no.
# regimen = 1: los sistemas de tendencia solo operan con ADX ≥ 20, los de rango con ADX ≤ 25 y nadie con volatilidad disparada.
# confirmar_1h = 1: la señal solo vale si la tendencia de 1 h (EMA20) va en la misma dirección.
# flujo = 1: no entra contra el libro de órdenes ni con funding extremo a su favor (todos apostando lo mismo).
# veto_llm = 1: antes de entrar, el comité de veto (gemma, en el PC) revisa la operación con las noticias y dice sí o no.
# noticias = 1: no entra contra noticias fuertes del símbolo (nota ≤ -0,4 en su dirección) ni durante un evento macro de impacto alto.
# protecciones (idea de freqtrade): `enfriar` velas sin volver al mismo símbolo tras cerrar; `guardia` pérdidas seguidas → pausa.
# roi_min: el objetivo baja con el tiempo de rr a roi_min (en R) a lo largo de max_barras; None = objetivo fijo.
# trail_r: desde +trail_r R el stop persigue al precio a trail_frac × la distancia del stop; 0 = sin trailing.
# filtro_ia: probabilidad mínima de ganar según el modelo del enjambre (trader/ia.py); 0 = sin filtro.
POOL_CRIPTO = CRIPTO + ['LTC-USD', 'BCH-USD', 'DOT-USD', 'UNI-USD', 'AAVE-USD', 'NEAR-USD', 'SUI-USD', 'SHIB-USD', 'APT-USD', 'ARB-USD',
                        'OP-USD', 'HBAR-USD', 'XLM-USD', 'ATOM-USD', 'FIL-USD', 'ETC-USD', 'PEPE-USD']
N_CRIPTO, SPREAD_MAX, UNIVERSO_CADA, IA_CADA, SENT_CADA = 12, 0.15, 3600, 1800, 1800
MIN_VENTAJA_MIN = 3.0  # regla dura: el objetivo debe pagar al menos 3 veces comisión + spread (sea cual sea el gen min_ventaja)
MAX_CORR = 2           # como mucho 2 posiciones cripto en la misma dirección (se mueven casi todas con BTC)
REG_TEND, REG_RANGO = ('ema_cruce', 'ruptura', 'impulso'), ('bollinger', 'rsi_rev', 'rejilla')
IA_HIST_CADA = 6 * 3600
RANGO_HIST = {'1m': '5d', '5m': '20d', '15m': '40d'}
# entrada 'limite': espera un retroceso de `retroceso` ATR durante `espera` velas (no paga spread ni deslizamiento al entrar).
# tramos > 1 (DCA): el resto de la posición se compra escalonado entre la entrada y el stop, promediando el precio.
DCA_HASTA = 0.6  # el último tramo va al 60 % del camino hacia el stop
LOG = []
_hilo = None
_parar = threading.Event()


def log(t):
    LOG.append(f"{time.strftime('%H:%M:%S')} {t}")
    del LOG[:-200]


# ---------------- señales (compartidas por el agente en vivo y por el simulador de soluciones) ----------------


def senales(g, v, v1h=None):
    """Lista de señales (1, -1, 0) por vela cerrada según el genoma, con filtros aplicados. También devuelve ATR y tendencia."""
    return _senales(g, v, v1h)[:3]


def tendencia_1h(v1h):
    """[(hora de cierre, tendencia)] de las velas de 1 h: +1 si cierra sobre la EMA20 y esta sube, -1 al revés."""
    c = [b['c'] for b in v1h]
    m = ind.ema(c, 20)
    return [(b['t'] + 3600, (1 if c[k] > m[k] > m[k - 3] else -1 if c[k] < m[k] < m[k - 3] else 0) if k >= 3 and None not in (m[k], m[k - 3]) else 0)
            for k, b in enumerate(v1h)]


def _senales(g, v, v1h=None):
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
    elif e == 'rejilla':  # rejilla (grid): en un rango lateral compra en el escalón de abajo y vende en el de arriba
        k, nv, m = int(p['n']), max(int(p['niveles']), 3), ind.ema(c, 50)
        for i in range(max(k, 10), n):
            hi, lo = max(h[i - k:i]), min(l[i - k:i])
            paso = (hi - lo) / nv
            if not paso or m[i] is None or m[i - 10] is None or abs(m[i] - m[i - 10]) > paso:
                continue  # sin rango o con tendencia: la rejilla no aplica
            s[i] = 1 if lo < c[i] <= lo + paso else -1 if hi - paso <= c[i] < hi else 0
    atr = ind.atr(h, l, c, 14)
    ad = ind.adx(h, l, c, 14)
    lenta, al = [None] * n, None  # ATR medio lento: si el actual lo dobla, el mercado está en caos
    for i in range(n):
        if atr[i]:
            al = atr[i] if al is None else al + (atr[i] - al) * 2 / 101
            lenta[i] = al
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
            elif g.get('regimen') and (ad[i] is None or atr[i] > 1.8 * lenta[i] or (e in REG_TEND and ad[i] < 20) or (e in REG_RANGO and ad[i] > 25)):
                s[i] = 0
        elif s[i]:
            s[i] = 0
    if g.get('confirmar_1h'):
        th = tendencia_1h(v1h) if v1h else []
        cierres = [x[0] for x in th]
        seg = MARCOS.get(g.get('marco'), 1) * 60
        for i in range(n):
            if s[i]:
                k = bisect.bisect_right(cierres, v[i]['t'] + seg) - 1  # la última hora ya cerrada cuando cierra esta vela
                if k < 0 or th[k][1] != s[i]:
                    s[i] = 0
    return s, atr, tend, ad


def coste_pct(cfg, spread_pct=0.0, tipo=None):
    com = (cfg.get('comision_por_tipo') or {}).get(tipo or '', cfg['comision_pct'])
    return 2 * (com + cfg['deslizamiento_pct']) + spread_pct


def niveles_dca(g):
    """Fracciones del camino entrada→stop donde se compra cada tramo (0 = la entrada)."""
    tr = max(1, int(g.get('tramos', 1)))
    return [DCA_HASTA * j / (tr - 1) for j in range(tr)] if tr > 1 else [0.0]


def simular(g, series, coste, ahorro=0.0, altas=None, ventana=(0.0, 1.0), muestras=None):
    """Prueba un genoma en velas reales recientes: entra en la apertura siguiente a la señal (o en el límite si el precio
    retrocede hasta él), stop/objetivo dentro de la vela (si tocan los dos, cuenta el stop), tramos DCA, salida por tiempo
    y breakeven. `ahorro` = lo que no se paga al entrar con límite (deslizamiento + medio spread), en %.
    Devuelve la lista de resultados en R (netos de costes) frente al riesgo planeado.
    altas: velas de 1 h de cada serie (para confirmar_1h); ventana: tramo de la serie (fracciones) donde se permiten entradas;
    muestras: si es una lista, se le añade (rasgos, dirección, R) de cada operación (datos para entrenar la IA)."""
    lim, fr = g.get('entrada') == 'limite', niveles_dca(g)
    peso = sum(1 - f for f in fr)
    pr_r, pf, sa = g.get('parcial_r') or 0, g.get('parcial_frac', 0.5), int(g.get('sin_avance') or 0)
    rs = []
    for k_, v in enumerate(series):
        s, atr, tend, ad = _senales(g, v, altas[k_] if altas else None)
        i, fin = max(1, int(ventana[0] * len(v))), min(len(v) - 1, int(ventana[1] * len(v)))
        while i < fin:
            if not s[i - 1] or not atr[i - 1]:
                i += 1
                continue
            d = s[i - 1]
            dist = atr[i - 1] * g['stop_atr']
            c0 = coste - ahorro if lim else coste
            if lim:
                L = v[i - 1]['c'] - d * g.get('retroceso', 0.3) * atr[i - 1]
                k = next((j for j in range(i, min(i + int(g.get('espera', 3)), len(v) - 1)) if (v[j]['l'] <= L if d > 0 else v[j]['h'] >= L)), None)
                if k is None:  # el precio no volvió: no hay operación
                    i += 1
                    continue
                ent, i = (min(L, v[k]['o']) if d > 0 else max(L, v[k]['o'])), k
            else:
                ent = v[i]['o']
            if g['rr'] * dist / ent * 100 < max(g['min_ventaja'], MIN_VENTAJA_MIN) * c0 or dist / ent * 100 < 2 * c0:
                i += 1
                continue
            ras = {'atr_pct': atr[i - 1] / v[i - 1]['c'] * 100, 'tendencia': tend[i - 1], 'hora': time.gmtime(v[i]['t']).tm_hour,
                   'coste_pct': c0, 'dist': dist, 'precio': ent, 'adx': ad[i - 1]}
            if g.get('filtro_ia'):
                pr = ia.probabilidad(ras, g, d)
                if pr is not None and pr < g['filtro_ia']:
                    i += 1
                    continue
            stop, obj, be_hecho, mejor = ent - d * dist, ent + d * g['rr'] * dist, False, ent
            lleno = [ent]  # precios de los tramos ya comprados
            salida, j, parc, mfav = None, i, None, 0.0
            while j < len(v) and j - i < g['max_barras']:
                b = v[j]
                for f in fr[len(lleno):]:  # tramos DCA (órdenes límite por debajo de la entrada)
                    nv = ent - d * dist * f
                    if (b['l'] <= nv) if d > 0 else (b['h'] >= nv):
                        lleno.append(nv)
                if (d > 0 and b['l'] <= stop) or (d < 0 and b['h'] >= stop):
                    salida = stop
                    break
                fav = b['h'] - ent if d > 0 else ent - b['l']
                mfav = max(mfav, fav)
                if pr_r and parc is None and fav >= pr_r * dist:  # toma parcial y stop a la entrada
                    parc = ent + d * pr_r * dist
                    stop = max(stop, ent) if d > 0 else min(stop, ent)
                if (d > 0 and b['h'] >= obj) or (d < 0 and b['l'] <= obj):
                    salida = obj
                    break
                if g['be_en_r'] and not be_hecho and (b['h'] - ent if d > 0 else ent - b['l']) >= g['be_en_r'] * dist:
                    stop, be_hecho = ent, True
                if g.get('trail_r'):  # el stop persigue al precio (vale desde la vela siguiente)
                    mejor = max(mejor, b['h']) if d > 0 else min(mejor, b['l'])
                    if (mejor - ent) * d >= g['trail_r'] * dist:
                        nv = mejor - d * dist * g.get('trail_frac', 0.8)
                        stop = max(stop, nv) if d > 0 else min(stop, nv)
                if g.get('roi_min') is not None:  # tabla ROI: cuanto más tiempo, menos ganancia exige
                    req = g['rr'] - (g['rr'] - g['roi_min']) * min(1, (j - i + 1) / g['max_barras'])
                    if (b['c'] - ent) * d >= req * dist:
                        salida = b['c']
                        break
                if sa and j - i + 1 >= sa and parc is None and mfav < 0.3 * dist:  # stop por tiempo: no arranca
                    salida = b['c']
                    break
                j += 1
            if salida is None:
                j = min(j, len(v) - 1)
                salida = v[j]['c']
            bruto = sum((salida - x) * d for x in lleno)
            if parc is not None:
                bruto = pf * sum((parc - x) * d for x in lleno) + (1 - pf) * bruto
            r = bruto / (peso * dist) - (c0 + (len(lleno) - 1) * (coste - ahorro)) / 100 * ent / (peso * dist)
            rs.append(r)
            if muestras is not None:
                muestras.append((ras, d, r))
            i = j + 1 + int(g.get('enfriar', 0))
    return rs


def puntuar(rs, minimo=5):
    """Esperanza en R penalizada si hay pocos trades (para no premiar la suerte)."""
    if len(rs) < minimo:
        return -9.0
    m = statistics.fmean(rs)
    return m - 0.5 * (statistics.pstdev(rs) / math.sqrt(len(rs)))


def evaluar(g, series, coste, ahorro=0.0, altas=None):
    """Walk-forward: puntúa en el 60 % antiguo y en el 40 % reciente por separado y se queda con el peor de los dos.
    Un sistema solo puntúa bien si gana en los dos tramos (no basta con que haya tenido suerte en uno)."""
    a = simular(g, series, coste, ahorro, altas, (0.0, 0.6))
    b = simular(g, series, coste, ahorro, altas, (0.6, 1.0))
    if len(a) < 3 or len(b) < 3:
        return -9.0, a + b
    return min(puntuar(a, 3), puntuar(b, 3)), a + b

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
    if rnd.random() < 0.15:  # sesgo hacia la orden límite: paga menos costes
        g['entrada'] = 'mercado' if g.get('entrada') == 'limite' and rnd.random() < 0.3 else 'limite'
    g['retroceso'] = round(min(max(g.get('retroceso', 0.3) * rnd.uniform(1 - f, 1 + f), 0.05), 1.5), 2)
    if rnd.random() < 0.1:
        g['espera'] = rnd.choice([2, 3, 5, 8])
    if rnd.random() < 0.12:
        g['tramos'] = rnd.choice([1, 1, 2, 3])
    if rnd.random() < 0.1:
        g['enfriar'] = rnd.choice([0, 1, 2, 3, 5])
    if rnd.random() < 0.1:
        g['guardia'] = rnd.choice([0, 2, 3, 4])
    if rnd.random() < 0.15:
        g['roi_min'] = None if g.get('roi_min') is not None else round(rnd.uniform(0.2, 1.0), 2)
    elif g.get('roi_min') is not None:
        g['roi_min'] = round(min(max(g['roi_min'] * rnd.uniform(1 - f, 1 + f), 0.0), g['rr']), 2)
    if rnd.random() < 0.15:
        g['trail_r'] = 0.0 if g.get('trail_r') else round(rnd.uniform(0.5, 1.5), 2)
    g['trail_frac'] = round(min(max(g.get('trail_frac', 0.8) * rnd.uniform(1 - f, 1 + f), 0.3), 1.5), 2)
    if rnd.random() < 0.15:
        g['filtro_ia'] = 0.0 if g.get('filtro_ia') else round(rnd.uniform(0.45, 0.6), 2)
    if rnd.random() < 0.15:
        g['noticias'] = 0 if g.get('noticias') else 1
    if rnd.random() < 0.12:
        g['parcial_r'] = 0.0 if g.get('parcial_r') else round(rnd.uniform(0.7, 1.5), 2)
    elif g.get('parcial_r'):
        g['parcial_r'] = round(min(max(g['parcial_r'] * rnd.uniform(1 - f, 1 + f), 0.5), 2.0), 2)
    g['parcial_frac'] = round(min(max(g.get('parcial_frac', 0.5) * rnd.uniform(1 - f, 1 + f), 0.25), 0.75), 2)
    if rnd.random() < 0.1:
        g['sin_avance'] = rnd.choice([0, 0, 4, 6, 10])
    for k, pr in (('regimen', 0.1), ('confirmar_1h', 0.12), ('flujo', 0.12), ('veto_llm', 0.08)):
        if rnd.random() < pr:
            g[k] = 0 if g.get(k) else 1
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
    c.execute('CREATE INDEX IF NOT EXISTS ix_agt ON ag_trades(agente_id, trade_id)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_tr_cta ON trades(cuenta_id, estado, cerrado)')
    viejo = db.ajuste('agente')
    if viejo and viejo.get('cuenta_id'):  # el agente único de la v1 se retira (sus datos se quedan)
        db.ex('UPDATE cuentas SET archivada=1 WHERE id=?', (viejo['cuenta_id'],))
        db.ex("DELETE FROM ajustes WHERE clave='agente'")


def estado():
    return db.ajuste('enjambre', None)


def guardar(e):
    db.poner_ajuste('enjambre', e)


def _llm(a):
    """Trader del escuadrón LLM (trader/escuadron.py): decide gemma, no la evolución."""
    return '"estrategia": "llm"' in (a['genoma'] or '')


def _g(a):
    g = json.loads(a['genoma'])
    for k, v in GENOMA_BASE.items():
        g.setdefault(k, copy.deepcopy(v))
    return g


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
    g['entrada'] = 'limite' if est == 'rejilla' or rnd.random() < 0.8 else 'mercado'
    g['tramos'] = rnd.choice([1, 1, 2, 3])
    g['roi_min'] = rnd.choice([None, None, 0.5])
    g['trail_r'] = rnd.choice([0.0, 0.0, 1.0])
    g['filtro_ia'] = rnd.choice([0.0, 0.0, 0.5, 0.55])
    g['noticias'] = rnd.choice([0, 0, 1])
    g['parcial_r'] = rnd.choice([1.0, 1.0, 0.0])
    g['sin_avance'] = rnd.choice([0, 0, 6])
    g['regimen'] = rnd.choice([1, 1, 0])
    g['confirmar_1h'], g['flujo'] = rnd.choice([0, 1]), rnd.choice([0, 1])
    g['veto_llm'] = rnd.choice([0, 0, 0, 1])
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
        vivos, rnd = [a for a in agentes() if not _llm(a)], random.Random()
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
    vivos = [a for a in agentes() if not _llm(a)]
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
    db.ex("UPDATE ordenes SET estado='cancelada', motivo=? WHERE cuenta_id=? AND estado IN ('pendiente','espera')", (motivo, a['cuenta_id']))
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
        db.ex("UPDATE ordenes SET estado='cancelada', motivo='enjambre parado' WHERE cuenta_id=? AND estado IN ('pendiente','espera')", (a['cuenta_id'],))
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
_FOTOS = {}


def _despierto(si):
    """Windows: mientras el enjambre opera, el equipo no se suspende (la pantalla sí puede apagarse). Se libera al parar."""
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if si else 0))
        except Exception:
            pass


def _bucle():
    _despierto(True)
    try:
        _bucle_()
    finally:
        _despierto(False)


def _bucle_():
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
        sims += (e.get('universo') or {}).get('cripto') or CRIPTO
    if 'acciones' in e['mercados']:
        sims += ACCIONES
    bloq = set(e.get('bloqueos', {}).get('simbolos', [])) | set((g or {}).get('simbolos_bloqueados', []))
    return [s for s in sims if s not in bloq]


def _refrescar_universo(e):
    """Selección de mercados por volumen (VolumePairList): las criptos con más dólares negociados en 24 h y spread bajo."""
    u = e.get('universo') or {}
    if 'cripto' not in e.get('mercados', []) or time.time() - u.get('ts', 0) < UNIVERSO_CADA:
        return e
    filas = []
    for s_ in POOL_CRIPTO:
        try:
            q = datos.cotizacion(s_)
        except Exception:
            continue
        if not q.get('bid') or not q.get('ask') or not q.get('precio'):
            continue
        sp = (q['ask'] - q['bid']) / q['precio'] * 100
        if sp <= SPREAD_MAX:
            filas.append((q['volumen'] * q['precio'], s_, sp))
    filas.sort(reverse=True)
    elegidas = [x[1] for x in filas[:N_CRIPTO]]
    if len(elegidas) < 4:
        return e
    u = {'ts': int(time.time()), 'cripto': elegidas, 'detalle': [[x[1], round(x[0] / 1e6), round(x[2], 4)] for x in filas[:N_CRIPTO]]}
    e2 = estado()
    e2['universo'] = e['universo'] = u
    guardar(e2)
    log('🌐 mercados cripto por volumen: ' + ', '.join(x.replace('-USD', '') for x in elegidas))
    return e


def _jefe(e):
    from . import escuadron
    try:
        j = escuadron.jefe(e)
        if j:
            log('🧑‍💼 ' + escuadron.resumen_jefe()[:300])
    except Exception:
        log('error del jefe de mesa: ' + traceback.format_exc().splitlines()[-1])


def muestras_historicas(e, maximo=None):
    """Operaciones simuladas de los mejores sistemas vivos en semanas de velas reales (Yahoo): miles de ejemplos para la IA."""
    maximo = maximo or (3000 if NODO == 'pi' else 6000)
    rnd = random.Random()
    vivos = sorted(((aptitud(a)[0], a) for a in agentes() if not _llm(a)), key=lambda x: -9 if x[0] is None else x[0], reverse=True)
    gens, vistos = [], set()
    for f, a in vivos:
        g = _g(a)
        k = json.dumps({x: g.get(x) for x in ('estrategia', 'marco', 'p', 'stop_atr', 'rr')}, sort_keys=True)
        if k not in vistos:
            vistos.add(k)
            gens.append(dict(g, filtro_ia=0.0))
        if len(gens) >= 16:
            break
    sims = ((e.get('universo') or {}).get('cripto') or CRIPTO)[:6] + (ACCIONES[:3] if 'acciones' in e.get('mercados', []) else [])
    filas, cache = [], {}
    for g in gens:
        for s in rnd.sample(sims, min(3, len(sims))):
            try:
                if (s, g['marco']) not in cache:
                    cache[(s, g['marco'])] = datos.velas(s, g['marco'], rango=RANGO_HIST[g['marco']])[:-1]
                if (s, '60m') not in cache:
                    cache[(s, '60m')] = datos.velas(s, '60m', rango='60d')
            except Exception:
                continue
            cr = s.endswith('-USD')
            coste = coste_pct(CUENTA_CFG, 0.02 if cr else 0.01, 'CRYPTOCURRENCY' if cr else 'EQUITY')
            mu = []
            simular(g, [cache[(s, g['marco'])]], coste, CUENTA_CFG['deslizamiento_pct'] + 0.01, [cache[(s, '60m')]], muestras=mu)
            filas += [(dict(ras, coste_pct=round(ras['coste_pct'], 4)), g, d, round(r, 3)) for ras, d, r in mu]
    rnd.shuffle(filas)
    return filas[:maximo]


def _entrenar_ia():
    try:
        h = db.ajuste('ia_hist', None)
        if not h or time.time() - h.get('ts', 0) > IA_HIST_CADA:
            h = {'ts': int(time.time()), 'filas': muestras_historicas(estado() or {})}
            db.poner_ajuste('ia_hist', h)
            log(f"🤖 {len(h['filas'])} operaciones simuladas en semanas de velas reales para entrenar la IA")
        ia.entrenar(extra=h['filas'])
        log('🤖 ' + ia.resumen())
    except Exception:
        log('error entrenando la IA: ' + traceback.format_exc().splitlines()[-1])


def _sentimiento(e):
    try:
        sentimiento.actualizar(_universo(e), noticias=not analista.activo())
        log('📰 ' + sentimiento.resumen())
    except Exception:
        log('error en el sentimiento: ' + traceback.format_exc().splitlines()[-1])


def contexto(e, marcos):
    ctx = {'q': {}, 'v': {}, 'coste': {}, 'sen': {}, 'h': {}}
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
        x = _H1.get(s)
        if not x or time.time() - x[0] > 300:
            try:
                x = _H1[s] = (time.time(), datos.velas(s, '60m'))
            except Exception:
                pass
        if x:
            ctx['h'][s] = x[1]
    return ctx


_H1 = {}


def _senales_cache(ctx, g, s):
    clave = (json.dumps({k: g.get(k) for k in ('marco', 'estrategia', 'p', 'vol_min_pct', 'vol_max_pct', 'filtro_tendencia', 'cortos', 'horas_bloqueadas',
                                                'regimen', 'confirmar_1h')}, sort_keys=True), s)
    if clave not in ctx['sen']:
        v = ctx['v'].get((s, g['marco']))
        ctx['sen'][clave] = (_senales(g, v, ctx['h'].get(s)), v) if v and len(v) >= 60 else (None, v)
    return ctx['sen'][clave]

# ---------------- un tick de todo el enjambre ----------------


def _migrar_v2(e):
    """Una vez: las mejoras de costes pasan a todos (orden límite, toma parcial, régimen) y se reparten al azar las nuevas
    (confirmación de 1 h, flujo, veto, stop por tiempo) para que la evolución decida con datos cuáles sirven."""
    rnd = random.Random()
    for a in agentes():
        if _llm(a):
            continue
        g = _g(a)
        g['entrada'] = 'limite'
        g['confirmar_1h'], g['flujo'] = int(rnd.random() < 0.35), int(rnd.random() < 0.35)
        g['veto_llm'], g['sin_avance'] = int(rnd.random() < 0.15), (6 if rnd.random() < 0.2 else 0)
        _poner(a['id'], genoma=json.dumps(g))
    e['mejoras_v2'] = 1
    guardar(e)
    _leccion(None, None, None, 'evolucion', 'Mejoras: todos entran con orden límite, objetivo ≥ 3× costes, toma parcial en +1R con stop a la entrada, '
             'filtro de régimen (ADX), máximo 2 apuestas cripto iguales, y a prueba confirmación de 1 h, flujo del libro/funding, comité de veto y stop por tiempo.', {})


def paso_enjambre(e):
    if not e.get('mejoras_v2'):
        _migrar_v2(e)
        e = estado()
    e = _refrescar_universo(e)
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
    if ahora_ - e.get('ultimo_sent', 0) > SENT_CADA:
        e['ultimo_sent'] = ahora_
        guardar(e)
        threading.Thread(target=_sentimiento, args=(e,), daemon=True).start()
    from . import alpaca, escuadron
    if ahora_ - e.get('ultimo_espejo', 0) > 60 and alpaca.configurado():
        e['ultimo_espejo'] = ahora_
        guardar(e)
        threading.Thread(target=alpaca.espejo, daemon=True).start()
    if escuadron.activo() and not analista.activo():  # el analista de noticias vive donde está el modelo de lenguaje
        analista.arrancar(lambda: _universo(estado() or e))
        log('📰 analista de noticias en marcha')
    if ahora_ - e.get('ultima_ronda_llm', 0) > escuadron.LLM_CADA and escuadron.activo():
        e['ultima_ronda_llm'] = ahora_
        guardar(e)
        threading.Thread(target=escuadron.ronda, args=(e,), daemon=True).start()
    if ahora_ - e.get('ultimo_jefe', 0) > escuadron.JEFE_CADA and escuadron.activo():
        e['ultimo_jefe'] = ahora_
        guardar(e)
        threading.Thread(target=_jefe, args=(e,), daemon=True).start()
    if ahora_ - e.get('ultima_ia', 0) > IA_CADA:
        e['ultima_ia'] = ahora_
        guardar(e)
        threading.Thread(target=_entrenar_ia, daemon=True).start()
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


_PAUSA, _MALOS, _MEJOR = {}, {}, {}


def _proteccion(a, g, it):
    """Protecciones: StoplossGuard (N pérdidas seguidas) y racha mala (≤ -4R en 6 h) pausan al agente; devuelve el motivo."""
    ahora_ = time.time()
    hasta, motivo, ref = _PAUSA.get(a['id'], (0, '', None))
    if ahora_ < hasta:
        return motivo
    ult = db.q("""SELECT trade_id, r FROM ag_trades WHERE agente_id=? AND r IS NOT NULL AND COALESCE(causa,'')<>'sistema' AND abierto>=?
                  ORDER BY trade_id DESC LIMIT 40""", (a['id'], max(a['genoma_desde'] or 0, int(ahora_) - 6 * 3600)))
    if not ult or ult[0]['trade_id'] == ref:  # sin operaciones nuevas desde la última pausa
        return None
    marco = MARCOS.get(g['marco'], 1)
    n = int(g.get('guardia') or 0)
    if n and len(ult) >= n and all(x['r'] <= 0 for x in ult[:n]):
        mins = min(30 * marco, 180)
        motivo = f'⏸ {n} pérdidas seguidas: pausa de {mins} min'
    elif sum(x['r'] for x in ult) <= -4:
        mins = 120
        motivo = f"⏸ racha mala ({sum(x['r'] for x in ult):+.1f}R en 6 h): pausa de {mins} min"
    else:
        return None
    _PAUSA[a['id']] = (ahora_ + mins * 60, motivo, ult[0]['trade_id'])
    _leccion(a['id'], it['id'], None, 'proteccion', f"{a['nombre']}: {motivo[2:]} para no seguir perdiendo en un mal momento.", {})
    return motivo


def _vetados(a, cid, g):
    """Símbolos que este agente no debe tocar ahora: enfriamiento tras cerrar y LowProfitPairs (≥3 trades, < -0,5R en 12 h)."""
    ahora_ = time.time()
    ts, malos = _MALOS.get(a['id'], (0, set()))
    if ahora_ - ts > 300:
        malos = {x['simbolo'] for x in db.q("""SELECT simbolo, AVG(r) m, COUNT(*) n FROM ag_trades WHERE agente_id=? AND r IS NOT NULL
                  AND COALESCE(causa,'')<>'sistema' AND abierto>? GROUP BY simbolo HAVING n >= 3 AND m < -0.5""", (a['id'], int(ahora_) - 12 * 3600))}
        _MALOS[a['id']] = (ahora_, malos)
    espera = int(g.get('enfriar') or 0) * MARCOS.get(g['marco'], 1) * 60
    frios = {x['simbolo'] for x in db.q("SELECT simbolo, MAX(cerrado) c FROM trades WHERE cuenta_id=? AND estado='cerrado' AND cerrado>? GROUP BY simbolo",
                                        (cid, int(ahora_) - espera))} if espera else set()
    return malos | frios


_PEND = {}  # orden de entrada pendiente → datos para registrarla cuando se llene (si el proceso se reinicia, se cancelan)


def _entradas(a, g, it):
    """Revisa las órdenes de entrada pendientes: registra las que se llenaron (y pone sus tramos DCA), caduca las viejas."""
    cid = a['cuenta_id']
    con_pos = {p['simbolo'] for p in motor.posiciones(cid, False)}
    mias = [oid for oid, i in _PEND.items() if i['cid'] == cid]
    for o in db.q(f"SELECT * FROM ordenes WHERE cuenta_id=? AND reduce=0 AND padre_id IS NULL AND origen='agente' AND (estado='pendiente' OR id IN ({','.join('?' * len(mias)) or 'NULL'}))",
                  (cid, *mias)):
        info = _PEND.get(o['id'])
        if o['estado'] == 'ejecutada' and info:
            _PEND.pop(o['id'], None)
            _al_llenar(a, info, o)
        elif o['estado'] == 'cancelada':
            _PEND.pop(o['id'], None)
        elif o['estado'] == 'pendiente':
            tramo = (o['nota'] or '').startswith('tramo')
            vieja = time.time() - o['creada'] > g['espera'] * MARCOS.get(g['marco'], 1) * 60
            if (tramo and o['simbolo'] not in con_pos) or (not tramo and (o['id'] not in _PEND or vieja)):
                db.ex("UPDATE ordenes SET estado='cancelada', motivo=? WHERE id=? OR (padre_id=? AND estado='espera')",
                      ('tramo sin posición' if tramo else 'entrada límite caducada', o['id'], o['id']))
                _PEND.pop(o['id'], None)


def _al_llenar(a, info, o):
    cid, sim, d = a['cuenta_id'], info['sim'], info['d']
    pz = db.uno('SELECT trade_id FROM posiciones WHERE cuenta_id=? AND simbolo=?', (cid, sim))
    if not pz:
        return
    db.ex('UPDATE trades SET riesgo=? WHERE id=?', (info['riesgo'], pz['trade_id']))  # R frente al riesgo de TODOS los tramos
    db.ex('INSERT OR IGNORE INTO ag_trades(trade_id,intento,simbolo,direccion,estrategia,genoma,rasgos,abierto,agente_id) VALUES(?,?,?,?,?,?,?,?,?)',
          (pz['trade_id'], info['it'], sim, 'largo' if d > 0 else 'corto', info['g']['estrategia'], json.dumps(info['g']), json.dumps(info['rasgos']), int(time.time()), a['id']))
    ent = o['precio_ejec']
    for k, f in enumerate(niveles_dca(info['g'])[1:], 2):
        try:
            motor.enviar(cid, sim, 'buy' if d > 0 else 'sell', info['q'], 'limite', limite=ent - d * info['dist'] * f,
                         nota=f"tramo {k} {a['nombre']}", setup=info['g']['estrategia'], origen='agente')
        except motor.ErrorOrden as err:
            log(f"{a['nombre']}: tramo rechazado {sim}: {err}")
    txt = f"{'🟢' if d > 0 else '🔴'} {sim} {'largo' if d > 0 else 'corto'} @ {ent:.6g}" + (' (límite)' if o['tipo'] == 'limite' else '')
    _poner(a['id'], estado='en posición', ultimo=txt)
    log(f"{a['nombre']} {txt}")


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
    if time.time() - _FOTOS.get(a['id'], 0) > 300:
        _FOTOS[a['id']] = time.time()
        motor.instantanea(cid)
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
    _entradas(a, g, it)
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
    pausa = _proteccion(a, g, it)
    if pausa:
        if not pos:
            _poner(a['id'], estado='en pausa', ultimo=pausa)
        return
    if g['estrategia'] == 'llm':  # sus entradas las decide el modelo de lenguaje en escuadron.ronda()
        return
    pend = db.q("SELECT simbolo, lado FROM ordenes WHERE cuenta_id=? AND estado='pendiente' AND reduce=0 AND origen='agente'", (cid,))
    esperando = {o['simbolo'] for o in pend} | {k[1] for k in _VETO if k[0] == a['id']}
    dirs = [1 if p_['cantidad'] > 0 else -1 for p_ in pos if p_['simbolo'].endswith('-USD')] + \
           [1 if o['lado'] == 'buy' else -1 for o in pend if o['simbolo'].endswith('-USD') and o['simbolo'] not in {p_['simbolo'] for p_ in pos}]
    if len(pos) + len(esperando - {p['simbolo'] for p in pos}) >= CUENTA_CFG['max_posiciones']:
        return
    hora = time.gmtime().tm_hour
    if hora in g['horas_bloqueadas'] or hora in e.get('bloqueos', {}).get('horas', []):
        return
    abiertos = {p['simbolo'] for p in pos} | esperando | _vetados(a, cid, g)
    sent = sentimiento.actual()
    for sim in _universo(e, g):
        if sim in abiertos or sim not in ctx['q']:
            continue
        (s_atr, v) = _senales_cache(ctx, g, sim)
        if not s_atr:
            continue
        s, atr, tend, ad = s_atr
        if not s[-1] or not atr[-1]:
            continue
        q, d = ctx['q'][sim], s[-1]
        if sim.endswith('-USD') and dirs.count(d) >= MAX_CORR:  # ya hay 2 apuestas cripto iguales: la tercera sería la misma apuesta
            continue
        lim = g['entrada'] == 'limite'
        coste, spread = ctx['coste'][sim]
        if lim:  # entra en el retroceso con orden límite: no paga deslizamiento ni spread al entrar
            coste -= CUENTA_CFG['deslizamiento_pct'] + spread / 2
        p = q['precio'] - d * g['retroceso'] * atr[-1] if lim else q['precio']
        dist = atr[-1] * g['stop_atr']
        # regla de profesional: el objetivo debe pagar los costes con margen, y los costes no pueden comerse más de la mitad del stop
        if g['rr'] * dist / p * 100 < max(g['min_ventaja'], MIN_VENTAJA_MIN) * coste or dist / p * 100 < 2 * coste:
            db.ex('UPDATE ag_agentes SET descartes=descartes+1 WHERE id=?', (a['id'],))
            continue
        r = motor.resumen(cid)
        fr = niveles_dca(g)
        libres = max(1, CUENTA_CFG['max_posiciones'] - len(pos) - len(esperando))
        total = min(r['equity'] * g['riesgo_pct'] / 100 / (dist * sum(1 - f for f in fr) / len(fr)), r['poder_compra'] * 0.97 / libres / p)
        qty = total / len(fr)  # primer tramo; el resto, escalonado hacia el stop
        if qty * p < 0.0001:
            continue
        sl, tp = p - d * dist, p + d * g['rr'] * dist
        rasgos = {'atr_pct': round(atr[-1] / p * 100, 4), 'tendencia': tend[-1], 'hora': hora, 'spread_pct': round(spread, 4),
                  'coste_pct': round(coste, 4), 'precio': p, 'dist': dist, 'entrada': g['entrada'], 'tramos': len(fr),
                  'adx': round(ad[-1], 1) if ad[-1] else None, **sentimiento.rasgos(sim, sent), **flujo.rasgos(sim)}
        if g.get('noticias') and (rasgos.get('sent', 0) * d <= -0.4 or rasgos.get('evento')):  # el analista avisa: noticias en contra o evento macro
            db.ex('UPDATE ag_agentes SET descartes=descartes+1 WHERE id=?', (a['id'],))
            continue
        if g.get('flujo') and flujo.en_contra(rasgos, d):  # el libro empuja en contra o todo el mundo ya está en ese lado
            db.ex('UPDATE ag_agentes SET descartes=descartes+1 WHERE id=?', (a['id'],))
            continue
        if g.get('filtro_ia'):  # el modelo del enjambre opina: si ve pocas opciones de ganar, no entra
            pr = ia.probabilidad(rasgos, g, d)
            if pr is not None:
                rasgos['p_ia'] = round(pr, 3)
                if pr < g['filtro_ia']:
                    db.ex('UPDATE ag_agentes SET descartes=descartes+1 WHERE id=?', (a['id'],))
                    continue
        info = {'cid': cid, 'it': it['id'], 'sim': sim, 'd': d, 'g': g, 'rasgos': rasgos, 'q': qty, 'dist': dist, 'riesgo': qty * dist * sum(1 - f for f in fr),
                'p': p, 'lim': lim, 'sl': sl, 'tp': tp, 'atr': atr[-1], 'ref': q['precio']}
        from . import escuadron
        if g.get('veto_llm') and escuadron.activo():  # la decide el comité de veto (escuadron.vetar) en el próximo minuto
            _VETO[(a['id'], sim)] = dict(info, ts=time.time(), aid=a['id'])
            esperando.add(sim)
            _poner(a['id'], ultimo=f"⚖ {sim}: esperando al comité de veto")
        elif _enviar_entrada(a, info):
            esperando.add(sim)
        if sim.endswith('-USD') and sim in esperando:
            dirs.append(d)
        if len(motor.posiciones(cid, False)) + len(esperando) >= CUENTA_CFG['max_posiciones']:
            break


_VETO = {}  # (agente, símbolo) → entrada pendiente del comité de veto


def _enviar_entrada(a, info):
    """Envía la orden de entrada (mercado o límite) con su stop y objetivo. True si queda en marcha."""
    g, sim, d, p = info['g'], info['sim'], info['d'], info['p']
    try:
        o = motor.enviar(info['cid'], sim, 'buy' if d > 0 else 'sell', info['q'], 'limite' if info['lim'] else 'mercado', limite=p if info['lim'] else None,
                         sl=info['sl'], tp=info['tp'], setup=g['estrategia'], nota=f"{a['nombre']} {g['estrategia']} {g['marco']}", origen='agente')
    except motor.ErrorOrden as err:
        log(f"{a['nombre']}: orden rechazada {sim}: {err}")
        return False
    if o['estado'] == 'ejecutada':
        _al_llenar(a, info, o)
    else:
        _PEND[o['id']] = info
        _poner(a['id'], ultimo=f"⏳ {sim} límite @ {p:.6g}")
    return True


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
        ag = db.uno('SELECT rasgos FROM ag_trades WHERE trade_id=?', (p['trade_id'],))
        dist = (json.loads(ag['rasgos']).get('dist') if ag else None) or abs(t['entrada'] - (t['stop_inicial'] or t['entrada']))
        sg = 1 if p['cantidad'] > 0 else -1
        barras = (time.time() - t['abierto']) / 60 / MARCOS.get(g.get('marco', '1m'), 1)
        if dist and g.get('roi_min') is not None and barras >= 1:  # tabla ROI
            req = g['rr'] - (g['rr'] - g['roi_min']) * min(1, barras / g['max_barras'])
            if (p['precio'] - t['entrada']) * sg >= req * dist:
                try:
                    motor.cerrar(cid, p['simbolo'], 100, 'roi')
                except motor.ErrorOrden:
                    pass
                continue
        fav = (p['precio'] - t['entrada']) * sg
        if dist and g.get('parcial_r') and not (t['salida_cant'] or 0) and fav >= g['parcial_r'] * dist:  # toma parcial + stop a la entrada
            try:
                motor.cerrar(cid, p['simbolo'], g.get('parcial_frac', 0.5) * 100, 'parcial')
                if p['stop'] is None or (t['entrada'] - p['stop']) * sg > 0:
                    motor.mover_stop(cid, p['simbolo'], stop=t['entrada'])
            except motor.ErrorOrden:
                pass
            continue
        if dist and g.get('sin_avance') and barras >= g['sin_avance'] and not (t['salida_cant'] or 0) and (t['mfe'] or 0) < 0.3 * dist:
            try:
                motor.cerrar(cid, p['simbolo'], 100, 'sin avance')
            except motor.ErrorOrden:
                pass
            continue
        if dist and g.get('trail_r'):  # trailing stop desde +trail_r R
            k = p['trade_id']
            mejor = _MEJOR.get(k, t['entrada'])
            mejor = max(mejor, p['precio']) if sg > 0 else min(mejor, p['precio'])
            _MEJOR[k] = mejor
            if len(_MEJOR) > 20000:
                _MEJOR.clear()
            if (mejor - t['entrada']) * sg >= g['trail_r'] * dist:
                nv = mejor - sg * dist * g.get('trail_frac', 0.8)
                if p['stop'] is None or (nv - p['stop']) * sg > dist * 0.02:
                    try:
                        motor.mover_stop(cid, p['simbolo'], stop=nv)
                    except motor.ErrorOrden:
                        pass
                    continue
        if g['be_en_r'] and t['stop_inicial'] and p['stop'] is not None:
            dist = abs(t['entrada'] - t['stop_inicial'])
            fav = (p['precio'] - t['entrada']) * (1 if p['cantidad'] > 0 else -1)
            if dist and fav >= g['be_en_r'] * dist and abs(p['stop'] - t['entrada']) > dist * 0.05:
                motor.mover_stop(cid, p['simbolo'], stop=t['entrada'])


SISTEMA = ('nuevo intento', 'enjambre parado', 'agente parado', 'sobra (tamaño del enjambre)')


def _registrar_cerrados(e, a, it):
    for t in db.q("""SELECT t.id, t.r, t.pnl, t.nota_cierre, ag.estrategia FROM trades t JOIN ag_trades ag ON ag.trade_id=t.id
                     WHERE t.estado='cerrado' AND ag.r IS NULL AND t.cuenta_id=?""", (a['cuenta_id'],)):
        r = t['r'] if t['r'] is not None else 0.0
        if (t['nota_cierre'] or '') in SISTEMA:  # lo cerró el sistema (reinicio, parada…): no es una decisión del agente, no se aprende de él
            db.ex("UPDATE ag_trades SET r=?, causa='sistema', revisado=1 WHERE trade_id=?", (r, t['id']))
            continue
        db.ex('UPDATE ag_trades SET r=? WHERE trade_id=?', (r, t['id']))
        db.ex('UPDATE ag_intentos SET trades=trades+1, ganados=ganados+? WHERE id=?', (1 if r > 0 else 0, it['id']))
        e = estado()
        b = e['bandido'].setdefault(t['estrategia'], [1, 1])
        b[0 if r > 0 else 1] += 1
        guardar(e)
        _poner(a['id'], ultimo=f"{'✅' if r > 0 else '❌'} {r:+.2f}R")


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
        if (x['r'] if x['r'] is not None else x['pnl']) > 0:
            continue
        causas = diagnosticar(x, g)
        db.ex('UPDATE ag_trades SET causa=? WHERE trade_id=?', (causas[0][0], x['trade_id']))
        for tipo, texto, cambio in causas[:2]:
            _leccion(a['id'], x['intento'], x['trade_id'], tipo, f"{a['nombre']} · {texto}", cambio)
            if g['estrategia'] != 'llm':
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
        elif not g.get('trail_r'):
            causas.append(('devolvio', f"{x['simbolo']}: devolvió la ganancia aun con breakeven. Solución: trailing stop desde +1R.", {'trail_r': 1.0}))
        else:
            nv = round(max(g['rr'] * 0.85, 1.0), 2)
            causas.append(('devolvio', f"{x['simbolo']}: devolvió la ganancia aun con breakeven y trailing. Solución: objetivo de {g['rr']}R a {nv}R.", {'rr': nv}))
    if ras['tendencia'] == -d and not g['filtro_tendencia']:
        causas.append(('contra_tendencia', f"{x['simbolo']}: entró contra la tendencia de la EMA50. Solución: solo a favor de la tendencia.", {'filtro_tendencia': True}))
    gan = [json.loads(y['rasgos'])['atr_pct'] for y in db.q("SELECT rasgos FROM ag_trades WHERE r > 0 AND COALESCE(causa,'')<>'sistema' ORDER BY trade_id DESC LIMIT 60")]
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
    filas = db.q("SELECT simbolo, rasgos, r, agente_id, abierto FROM ag_trades WHERE r IS NOT NULL AND COALESCE(causa,'')<>'sistema' ORDER BY trade_id DESC LIMIT 1500")
    por_s, por_h = {}, {}
    for f in filas:
        por_s.setdefault(f['simbolo'], []).append(f)
        por_h.setdefault(json.loads(f['rasgos'])['hora'], []).append(f)
    b = e.setdefault('bloqueos', {'simbolos': [], 'horas': []})
    dias = lambda fs: len({time.strftime('%Y-%m-%d', time.gmtime(x['abierto'])) for x in fs})
    horas = lambda fs: len({time.gmtime(x['abierto']).tm_hour for x in fs})
    agentes_ = lambda fs: len({x['agente_id'] for x in fs})
    for s_, fs in por_s.items():  # un símbolo: muchos trades de muchos agentes, repartidos en varias horas
        rs = [x['r'] for x in fs]
        if len(rs) >= 30 and agentes_(fs) >= 8 and horas(fs) >= 4 and statistics.fmean(rs) < -0.35 and s_ not in b['simbolos'] and len(b['simbolos']) < len(CRIPTO + ACCIONES) - 4:
            b['simbolos'].append(s_)
            _leccion(None, None, None, 'compartida', f"Enjambre: {s_} pierde en {len(rs)} operaciones de {agentes_(fs)} agentes a lo largo de {horas(fs)} horas ({statistics.fmean(rs):+.2f}R de media). Nadie lo opera más.", {'simbolo': s_})
    for h, fs in por_h.items():  # una hora: hace falta verlo en al menos 3 días distintos
        rs = [x['r'] for x in fs]
        if len(rs) >= 30 and dias(fs) >= 3 and agentes_(fs) >= 8 and statistics.fmean(rs) < -0.35 and h not in b['horas'] and len(b['horas']) < 8:
            b['horas'].append(h)
            _leccion(None, None, None, 'compartida', f"Enjambre: a las {h}:00 UTC se pierde en {dias(fs)} días distintos ({len(rs)} trades, {statistics.fmean(rs):+.2f}R). Nadie opera a esa hora.", {'hora': h})


def aptitud(a):
    rs = [x['r'] for x in db.q("SELECT r FROM ag_trades WHERE agente_id=? AND abierto>=? AND r IS NOT NULL AND COALESCE(causa,'')<>'sistema'", (a['id'], a['genoma_desde'] or 0))]
    return (puntuar(rs) if len(rs) >= 5 else None), len(rs)


def _cruce(g1, g2, rnd):
    h = copy.deepcopy(g1)
    for k in ('stop_atr', 'rr', 'filtro_tendencia', 'riesgo_pct', 'max_barras', 'be_en_r', 'min_ventaja', 'marco', 'entrada', 'retroceso', 'tramos',
              'enfriar', 'guardia', 'roi_min', 'trail_r', 'trail_frac', 'filtro_ia', 'noticias', 'parcial_r', 'parcial_frac', 'sin_avance',
              'regimen', 'confirmar_1h', 'flujo', 'veto_llm'):
        if rnd.random() < 0.5:
            h[k] = copy.deepcopy(g2.get(k, GENOMA_BASE[k]))
    return h


def evolucionar(e):
    """Selección natural: el 20 % peor se reemplaza por hijos (mutación / cruce) del 10 % mejor."""
    rnd = random.Random()
    vivos = [a for a in agentes() if not _llm(a)]
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
    vivos = [(aptitud(x)[0], x) for x in agentes() if x['id'] != a['id'] and not _llm(x)]
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
    g = copy.deepcopy(actual)
    g['entrada'] = 'mercado' if actual['entrada'] == 'limite' else 'limite'
    cand.append((f"mismo sistema entrando con {'orden a mercado' if g['entrada'] == 'mercado' else 'orden límite en el retroceso'}", g))
    g = copy.deepcopy(actual)
    g['tramos'] = 1 if actual['tramos'] > 1 else 3
    cand.append(('mismo sistema ' + ('sin DCA' if g['tramos'] == 1 else 'comprando en 3 tramos (DCA)'), g))
    g = copy.deepcopy(actual)
    g['roi_min'] = None if actual['roi_min'] is not None else 0.4
    cand.append(('mismo sistema ' + ('con objetivo fijo' if g['roi_min'] is None else 'con objetivo que baja con el tiempo (ROI)'), g))
    g = copy.deepcopy(actual)
    g['trail_r'] = 0.0 if actual['trail_r'] else 1.0
    cand.append(('mismo sistema ' + ('sin trailing' if not g['trail_r'] else 'con trailing stop desde +1R'), g))
    if ia.modelo().get('ok'):
        g = copy.deepcopy(actual)
        g['filtro_ia'] = 0.0 if actual['filtro_ia'] else 0.52
        cand.append(('mismo sistema ' + ('sin filtro de IA' if not g['filtro_ia'] else 'con filtro de IA'), g))
    if actual['estrategia'] != 'rejilla':
        g = copy.deepcopy(actual)
        g.update(estrategia='rejilla', p=dict(ESTRATEGIAS['rejilla']), entrada='limite')
        cand.append(('rejilla (grid) en rango lateral', g))
    for k, nombre in (('confirmar_1h', 'confirmando con la tendencia de 1 h'), ('regimen', 'operando solo en su régimen (ADX)'), ('parcial_r', 'con toma parcial en +1R')):
        g = copy.deepcopy(actual)
        g[k] = 0 if actual.get(k) else 1
        cand.append(('mismo sistema ' + (nombre if g[k] else 'sin ' + nombre.split(' ', 1)[1]), g))
    sims = [s for s in _universo(e, actual) if s in ctx['q']][:8]
    series = {}
    for m in MARCOS:
        series[m] = [ctx['v'].get((s, m)) or datos.velas(s, m)[:-1] for s in sims] if sims else []
    altas = [ctx['h'].get(s) for s in sims]
    if not sims:
        return _mutar(actual, rnd), 'Sin mercados abiertos para probar soluciones: sigo con una mutación.'
    coste = statistics.fmean(ctx['coste'][s][0] for s in sims)
    ahorro = CUENTA_CFG['deslizamiento_pct'] + statistics.fmean(ctx['coste'][s][1] for s in sims) / 2
    res = []
    for nombre, g in cand:
        par = [(v, h) for v, h in zip(series.get(g.get('marco', '1m'), []), altas) if v]
        pt, rs = evaluar(g, [x[0] for x in par], coste, ahorro, [x[1] for x in par])
        res.append((pt, nombre, g, len(rs), statistics.fmean(rs) if rs else 0))
    res.sort(key=lambda x: x[0], reverse=True)
    p, nombre, g, n, m = res[0]
    g = copy.deepcopy(g)
    extra = ''
    if p <= 0:
        g['riesgo_pct'] = max(round(g['riesgo_pct'] * 0.7, 1), 3.0)
        extra = f' Nada sale positivo en datos recientes: arriesgo menos ({g["riesgo_pct"]} %).'
    tabla = '; '.join(f"{x[1]}: {x[4]:+.2f}R/{x[3]}" for x in res[:3])
    return g, f"Probé {len(res)} soluciones en {len(sims)} mercados reales (cada una debe ganar en el tramo antiguo Y en el reciente). Elegida: {nombre} ({m:+.2f}R por trade en {n}). Top: {tabla}.{extra}"


def _quiebra(e, a, it, eq, ctx):
    causas = db.q('SELECT tipo, COUNT(*) n FROM ag_lecciones WHERE intento=? AND agente_id=? GROUP BY tipo ORDER BY n DESC', (it['id'], a['id']))
    causa = ', '.join(f"{c['tipo']} ×{c['n']}" for c in causas[:3]) or 'sin autopsias aún'
    db.ex('UPDATE ag_intentos SET fin=?, final=?, resultado=?, causa=? WHERE id=?', (int(time.time()), eq, 'quiebra', causa, it['id']))
    if _llm(a):
        nuevo, inf = _g(a), 'Sigue con su criterio: las lecciones de esta quiebra van en su próximo prompt.'
    else:
        nuevo, inf = buscar_solucion(e, a, ctx)
    _leccion(a['id'], it['id'], None, 'quiebra', f"💀 {a['nombre']} quebró (llegó a {it['max_equity']:.3f} $). Causas: {causa}. {inf}", {})
    _poner(a['id'], genoma_desde=int(time.time()), estado='quebrado')
    _nuevo_intento(e, db.uno('SELECT * FROM ag_agentes WHERE id=?', (a['id'],)), nuevo, 'tras quiebra')

# ---------------- varios nodos (la Pi es la central; el PC u otros equipos suman agentes) ----------------


def _vista_agentes(lim=None):
    stats = {r['agente_id']: r for r in db.q("SELECT agente_id, COUNT(*) n, SUM(r>0) g, AVG(r) m FROM ag_trades WHERE r IS NOT NULL AND COALESCE(causa,'')<>'sistema' GROUP BY agente_id")}
    quie = {r['agente_id']: r['n'] for r in db.q("SELECT agente_id, COUNT(*) n FROM ag_intentos WHERE resultado='quiebra' GROUP BY agente_id")}
    curvas = {}
    for r in db.q('SELECT cuenta_id, valor FROM equity WHERE ts>=? ORDER BY ts, rowid', (int(time.time()) - 6 * 3600,)):
        curvas.setdefault(r['cuenta_id'], []).append(r['valor'])
    cap = (estado() or {}).get('capital') or 1.0
    out = []
    for a in agentes():
        g = _g(a)
        try:
            eq = motor.resumen(a['cuenta_id'])['equity']
        except Exception:
            eq = None
        st, cv = stats.get(a['id'], {}), curvas.get(a['cuenta_id'], [])
        paso = max(1, len(cv) // 30)
        out.append({'id': a['id'], 'nombre': a['nombre'], 'estado': a['estado'], 'ultimo': a['ultimo'], 'equity': round(eq, 4) if eq is not None else None,
                    'pnl_pct': round((eq / cap - 1) * 100, 2) if eq is not None else None, 'max_equity': round(a['max_equity'] or 0, 4),
                    'generacion': a['generacion'], 'intentos': a['intentos'], 'metas': a['metas'], 'quiebras': quie.get(a['id'], 0),
                    'trades': st.get('n', 0), 'ganados': st.get('g') or 0, 'r_medio': round(st['m'], 3) if st.get('m') is not None else None,
                    'marco': g.get('marco'), 'estrategia': g['estrategia'], 'riesgo_pct': g['riesgo_pct'], 'tendencia': g['filtro_tendencia'],
                    'curva': [round(x, 4) for x in (cv[::paso] + ([cv[-1]] if cv and paso > 1 else []))][-31:] + ([round(eq, 4)] if eq is not None else [])})
    return out[:lim] if lim else out


def precios():
    out = []
    for s_ in CRIPTO + ACCIONES:
        try:
            q = datos.cotizacion(s_)
            out.append({'simbolo': s_, 'precio': q['precio'], 'cambio_pct': q['cambio_pct'], 'abierto': q['abierto']})
        except Exception:
            pass
    return out


def sincronizar(e):
    """Nodo → central: manda su estado (para Mission Control) y cada 30 min intercambia los mejores genomas (modelo de islas)."""
    import urllib.request
    migrar = time.time() - e.get('ultima_migracion', 0) > MIGRACION
    top = sorted(((aptitud(a)[0], a) for a in agentes() if not _llm(a)), key=lambda x: -9 if x[0] is None else x[0], reverse=True)[:3]
    cuerpo = {'nodo': NODO, 'kpis': kpis(), 'agentes': _vista_agentes(), 'lecciones': lecciones(15),
              'migrantes': [{'genoma': _g(a), 'aptitud': f, 'nombre': a['nombre']} for f, a in top if f is not None] if migrar else [], 'pedir': migrar,
              'sentimiento': sentimiento.actual()}
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
    vivos = sorted(((aptitud(a)[0], a) for a in agentes() if not _llm(a)), key=lambda x: -9 if x[0] is None else x[0])
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
    sentimiento.recibir(c.get('sentimiento'))  # las noticias puntuadas por el LLM del nodo (el PC) le sirven a la central
    out = {'ok': True}
    if c.get('pedir'):
        if (estado() or {}).get('activo'):
            recibir_migrantes(c.get('migrantes') or [], c.get('nodo'))
        top = sorted(((aptitud(a)[0], a) for a in agentes() if not _llm(a)), key=lambda x: -9 if x[0] is None else x[0], reverse=True)[:3]
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
    v = {'nodos': nodos, 'total': tot, 'lecciones': lecciones(25), 'log': LOG[-30:], 'precios': precios(), 'analista': noticias(15)}
    _cache_mapa.update(t=time.time(), v=v)
    return v


def noticias(n=30):
    """Lo último del analista de noticias (aquí o recibido del nodo que lo tiene)."""
    st = sentimiento.actual()
    ev = [x for x in st.get('eventos') or [] if x['ts'] > time.time() - 1800][:8]
    return {'activo': bool(st.get('analista')), 'ts': st.get('ts'), 'macro': st.get('macro') or {}, 'fng': st.get('fng'), 'noticias': (st.get('noticias') or [])[:n],
            'eventos': ev, 'simbolos': st.get('simbolos') or {}, 'resumen': analista.resumen(st), 'local': analista.activo(), 'estado': dict(analista.ESTADO)}


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


def _resumen_jefe():
    from . import escuadron
    return escuadron.resumen_jefe()


def _resumen_mejoras(tr):
    """Cómo van los genes nuevos: R medio con y sin cada uno (en las operaciones de la sesión)."""
    out = []
    for k, nombre in (('confirmar_1h', 'confirmación 1 h'), ('flujo', 'flujo'), ('veto_llm', 'veto'), ('regimen', 'régimen'), ('sin_avance', 'stop por tiempo')):
        si = [t['r'] for t in tr if json.loads(t['genoma']).get(k)]
        no = [t['r'] for t in tr if not json.loads(t['genoma']).get(k)]
        if len(si) >= 5 and len(no) >= 5:
            out.append(f"{nombre} {statistics.fmean(si):+.2f}R ({len(si)}) vs sin {statistics.fmean(no):+.2f}R")
    from . import escuadron
    return 'Genes nuevos: ' + (', '.join(out) or 'aún sin datos suficientes') + '. ' + escuadron.resumen_veto()


def _resumen_alpaca():
    from . import alpaca
    return alpaca.resumen() if alpaca.configurado() else 'Alpaca (paper): sin conectar.'


def informe(desde=None):
    preparar()
    e = estado() or {}
    desde = int(desde or e.get('sesion_inicio') or 0)
    tr = db.q("SELECT ag.*, t.pnl FROM ag_trades ag JOIN trades t ON t.id=ag.trade_id WHERE ag.abierto>=? AND ag.r IS NOT NULL AND COALESCE(ag.causa,'')<>'sistema'", (desde,))
    its = db.q('SELECT * FROM ag_intentos WHERE inicio>=?', (desde,))
    if not tr and not its:
        return 'El enjambre aún no ha operado en esta sesión.'
    g = sum(1 for t in tr if (t['r'] or 0) > 0)
    por = {}
    for t in tr:
        gt = json.loads(t['genoma'])
        por.setdefault(f"{gt['estrategia']} {gt['marco']}", []).append(t['r'])
    ranking = sorted(((k, statistics.fmean(v), len(v)) for k, v in por.items() if len(v) >= 5), key=lambda x: -x[1])
    mejores = sorted(((aptitud(a)[0], a) for a in agentes()), key=lambda x: -9 if x[0] is None else x[0], reverse=True)[:3]
    lec = db.q("SELECT tipo, COUNT(*) n FROM ag_lecciones WHERE ts>=? AND tipo NOT IN ('ruido','quiebra','jefe') GROUP BY tipo ORDER BY n DESC", (desde,))
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
        ia.resumen(),
        _resumen_mejoras(tr),
        flujo.resumen(),
        sentimiento.resumen(),
        analista.resumen(),
        _resumen_jefe(),
        _resumen_alpaca(),
        'Mercados cripto (por volumen): ' + ', '.join(x.replace('-USD', '') for x in ((e.get('universo') or {}).get('cripto') or CRIPTO)) + '.',
        'Aprendizajes compartidos: ' + (' | '.join(x['texto'] for x in comp) or '—'),
    ])
