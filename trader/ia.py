"""Filtro de IA del enjambre (idea de FreqAI, sin dependencias): una regresión logística que aprende de las operaciones
cerradas del propio enjambre qué entradas suelen salir bien. Solo se usa si en las operaciones más recientes (que no vio al
entrenar) predice mejor que el azar; si no, no filtra nada."""
import json, math, random, statistics, threading, time
from . import db

ESTRATEGIAS = ('ema_cruce', 'rsi_rev', 'ruptura', 'bollinger', 'impulso', 'rejilla')
MARCOS = ('1m', '5m', '15m')
MIN_TRADES = 200
CLAVE = 'enjambre_ia'
_modelo = None
_lock = threading.Lock()


def rasgos(ras, g, d):
    """Vector de entrada: solo información disponible en el momento de entrar (sin mirar el futuro)."""
    h = ras.get('hora', 0)
    precio = ras.get('precio') or 1
    x = [ras.get('atr_pct', 0.0), (ras.get('tendencia', 0) or 0) * d, math.sin(2 * math.pi * h / 24), math.cos(2 * math.pi * h / 24),
         ras.get('coste_pct', 0.0), (ras.get('dist') or 0) / precio * 100, float(d), 1.0 if g.get('entrada') == 'limite' else 0.0,
         float(g.get('tramos', 1)), float(g.get('rr', 1.5)), float(g.get('stop_atr', 1.5)),
         (ras.get('coste_pct', 0.0) / ((ras.get('dist') or 1) / precio * 100)) if ras.get('dist') else 0.0,
         ((ras['fng'] - 50) / 50) if ras.get('fng') is not None else 0.0, (ras.get('sent') or 0.0) * d,
         (ras.get('macro') or 0.0) * d, float(ras.get('evento') or 0)]  # analista: riesgo de mercado a favor y evento macro en curso  # sentimiento: miedo/euforia y noticias a favor
    x += [1.0 if g.get('estrategia') == e else 0.0 for e in ESTRATEGIAS]
    x += [1.0 if g.get('marco') == m else 0.0 for m in MARCOS]
    return x


def _norm(X):
    cols = list(zip(*X))
    mu = [statistics.fmean(c) for c in cols]
    sd = [statistics.pstdev(c) or 1.0 for c in cols]
    return mu, sd


def _prob(w, x):
    z = w[0] + sum(a * b for a, b in zip(w[1:], x))
    return 1 / (1 + math.exp(-max(min(z, 30), -30)))


def _entrenar(X, y, epocas=120, lr=0.1, l2=0.01):
    rnd = random.Random(7)
    w = [0.0] * (len(X[0]) + 1)
    idx = list(range(len(X)))
    for ep in range(epocas):
        rnd.shuffle(idx)
        paso = lr / (1 + ep * 0.05)
        for i in idx:
            err = _prob(w, X[i]) - y[i]
            w[0] -= paso * err
            for k, xk in enumerate(X[i], 1):
                w[k] -= paso * (err * xk + l2 * w[k])
    return w


def _logloss(ps, y):
    return -statistics.fmean(math.log(min(max(p, 1e-6), 1 - 1e-6)) if t else math.log(min(max(1 - p, 1e-6), 1 - 1e-6)) for p, t in zip(ps, y))


def entrenar(filas=None, limite=4000):
    """filas: [(rasgos_dict, genoma_dict, direccion, r)] de más antigua a más reciente. Devuelve el modelo (y lo guarda)."""
    if filas is None:
        filas = []
        for t in db.q("SELECT rasgos, genoma, direccion, r FROM ag_trades WHERE r IS NOT NULL AND COALESCE(causa,'')<>'sistema' ORDER BY trade_id DESC LIMIT ?", (limite,))[::-1]:
            try:
                filas.append((json.loads(t['rasgos']), json.loads(t['genoma']), 1 if t['direccion'] == 'largo' else -1, t['r']))
            except Exception:
                pass
    if len(filas) < MIN_TRADES:
        m = {'ok': False, 'n': len(filas), 'motivo': f'pocas operaciones para aprender ({len(filas)}/{MIN_TRADES})', 'ts': int(time.time())}
        return _guardar(m)
    X = [rasgos(r, g, d) for r, g, d, _ in filas]
    y = [1 if r > 0 else 0 for *_, r in filas]
    R = [r for *_, r in filas]
    corte = int(len(X) * 0.75)  # valida con lo más reciente, que el modelo no vio
    mu, sd = _norm(X[:corte])
    Z = [[(a - m) / s for a, m, s in zip(x, mu, sd)] for x in X]
    w = _entrenar(Z[:corte], y[:corte])
    ps = [_prob(w, z) for z in Z[corte:]]
    base = statistics.fmean(y[:corte])
    ll, llb = _logloss(ps, y[corte:]), _logloss([base] * len(ps), y[corte:])
    test = list(zip(ps, R[corte:]))
    buenos = [r for p, r in test if p >= 0.5]
    r_todos, r_filtro = statistics.fmean(R[corte:]), (statistics.fmean(buenos) if buenos else None)
    ok = ll < llb * 0.995 and r_filtro is not None and r_filtro > r_todos and len(buenos) >= 15
    mu, sd = _norm(X)  # modelo final con todo
    w = _entrenar([[(a - m) / s for a, m, s in zip(x, mu, sd)] for x in X], y)
    m = {'ok': ok, 'n': len(X), 'nf': len(X[0]), 'w': w, 'mu': mu, 'sd': sd, 'ts': int(time.time()), 'logloss': round(ll, 4), 'logloss_base': round(llb, 4),
         'r_todos': round(r_todos, 3), 'r_filtrado': round(r_filtro, 3) if r_filtro is not None else None, 'aceptadas_pct': round(len(buenos) / len(test) * 100),
         'motivo': 'predice mejor que el azar en operaciones nuevas' if ok else 'aún no predice mejor que el azar: no filtra'}
    return _guardar(m)


def _guardar(m):
    global _modelo
    with _lock:
        _modelo = m
    db.poner_ajuste(CLAVE, m)
    return m


def modelo():
    global _modelo
    if _modelo is None:
        _modelo = db.ajuste(CLAVE, {'ok': False, 'n': 0})
    return _modelo


def probabilidad(ras, g, d):
    """Probabilidad de que la operación salga ganadora, o None si el modelo no es fiable todavía."""
    m = modelo()
    x = rasgos(ras, g, d)
    if not m.get('ok') or m.get('nf') != len(x):  # sin modelo fiable, o entrenado con otros rasgos: no filtra hasta reentrenar
        return None
    return _prob(m['w'], [(a - mu) / s for a, mu, s in zip(x, m['mu'], m['sd'])])


def resumen():
    m = modelo()
    if not m.get('n'):
        return 'Filtro de IA: aún sin entrenar.'
    if not m.get('w'):
        return f"Filtro de IA: {m.get('motivo')}."
    return (f"Filtro de IA: entrenado con {m['n']} operaciones · {'ACTIVO' if m['ok'] else 'en pruebas'} ({m['motivo']}). "
            f"En operaciones nuevas: todas {m['r_todos']:+.2f}R, las que aprueba {m['r_filtrado'] if m['r_filtrado'] is not None else '—'}R "
            f"({m['aceptadas_pct']} % aprobadas).")
