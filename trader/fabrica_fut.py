"""FÁBRICA DE FUTUROS (micro futuros de CME, solo simulado): el mismo generador evolutivo que la de memecoins (fabrica.py)
con datos propios. Muestras = cada 15 min de 60 días de velas de 5 min (Yahoo; se amplían solas cada 10 min). Rasgos solo con
velas ANTERIORES a la entrada; ruta = las 4 h siguientes. Puede ir en largo y en corto. Costes por contrato: comisión + 1 tick
de deslizamiento al entrar y otro al salir. Entrena con el 70 % más viejo, valida con el 30 % reciente y lo que entra al salón se
mide EN VIVO desde ese momento: solo eso lo promociona (el piloto de futuros lo opera en Sim y, si gana, en la cuenta real)."""
import json, threading, time
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
from . import conciencia, datos, db, fabrica

# raíz Yahoo → contrato de NinjaTrader, tamaño del tick y valor del punto ($)
FUT = {'MES=F': {'nt': 'MES 12-26', 'full': 'MES DEC26', 'tick': 0.25, 'pv': 5.0, 'nombre': 'Micro S&P 500'},
       'MNQ=F': {'nt': 'MNQ 12-26', 'full': 'MNQ DEC26', 'tick': 0.25, 'pv': 2.0, 'nombre': 'Micro Nasdaq 100'},
       'MYM=F': {'nt': 'MYM 12-26', 'full': 'MYM DEC26', 'tick': 1.0, 'pv': 0.5, 'nombre': 'Micro Dow'},
       'M2K=F': {'nt': 'M2K 12-26', 'full': 'M2K DEC26', 'tick': 0.1, 'pv': 5.0, 'nombre': 'Micro Russell 2000'},
       'MGC=F': {'nt': 'MGC 12-26', 'full': 'MGC DEC26', 'tick': 0.1, 'pv': 10.0, 'nombre': 'Micro oro'},
       'MCL=F': {'nt': 'MCL 12-26', 'full': 'MCL DEC26', 'tick': 0.01, 'pv': 100.0, 'nombre': 'Micro petróleo WTI'}}
COMISION = 1.5            # $ por contrato ida y vuelta (comisión + tasas de CME, redondeado hacia arriba)
PASOS, CADA_MUESTRA, BARRA = 48, 3, 300
CADA, CADA_EVO = 600, 1800
NY = ZoneInfo('America/New_York')
RASGOS = ['ret1h', 'ch4h', 'ret4h', 'ch1d', 'rsi', 'atr', 'tend', 'volr', 'hora', 'dia', 'pos48']
ESQUEMA = """
CREATE TABLE IF NOT EXISTS fut_velas (inst TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, PRIMARY KEY(inst, t));
"""
MANUALES = [
    {'nombre': 'Impulso de la apertura de NY', 'conds': [['hora', '>', 9.75], ['hora', '<', 11], ['ret1h', '>', 0.15], ['tend', '>', 0]], 'sl_k': 3, 'rr': 2, 'hold': 24, 'lado': 1},
    {'nombre': 'Rebote de sobreventa', 'conds': [['rsi', '<', 25], ['tend', '<', -0.25]], 'sl_k': 3, 'rr': 1.5, 'hold': 12, 'lado': 1},
    {'nombre': 'Corto en sobrecompra', 'conds': [['rsi', '>', 75], ['tend', '>', 0.25]], 'sl_k': 3, 'rr': 1.5, 'hold': 12, 'lado': -1},
    {'nombre': 'Retroceso en día alcista', 'conds': [['ch1d', '>', 0.5], ['ret4h', '<', -0.3], ['rsi', '<', 45]], 'sl_k': 3, 'rr': 2, 'hold': 24, 'lado': 1},
    {'nombre': 'Continuación bajista', 'conds': [['ch1d', '<', -0.5], ['ret1h', '<', -0.2], ['pos48', '<', 0.3]], 'sl_k': 3, 'rr': 2, 'hold': 24, 'lado': -1},
]
PROMO = {'vivo_n': 10, 'vivo_usd': 5.0, 'vivo_gana': 40}   # $ por contrato y operación, ya con costes
_hilo, _ultimo = {'t': None}, {'evo': 0, 'msg': '', 'gen': 0}


def preparar():
    db.con().executescript(ESQUEMA)
    if not db.ajuste('fut_inicio'):
        db.poner_ajuste('fut_inicio', int(time.time()))


def actualizar(rango=None):
    """Baja velas de 5 min de Yahoo y las guarda (60 días la primera vez, luego 5)."""
    n = 0
    for s in FUT:
        r = rango or ('5d' if db.uno('SELECT 1 x FROM fut_velas WHERE inst=? LIMIT 1', (s,)) else '60d')
        try:
            v = datos.velas(s, '5m', r)
        except Exception:
            continue
        ahora = time.time()
        filas = [(s, x['t'], x['o'], x['h'], x['l'], x['c'], x['v']) for x in v if x['t'] + BARRA <= ahora]  # sin la vela abierta
        with db.LOCK:
            db.con().executemany('INSERT OR REPLACE INTO fut_velas VALUES(?,?,?,?,?,?,?)', filas)
        n += len(filas)
    return n


def _velas(inst):
    r = db.q('SELECT t, o, h, l, c, v FROM fut_velas WHERE inst=? ORDER BY t', (inst,))
    return {k: np.array([x[k] for x in r], dtype=float) for k in ('t', 'o', 'h', 'l', 'c', 'v')}


def _ema(c, n=20):
    e, k = np.empty_like(c), 2 / (n + 1)
    e[0] = c[0]
    for i in range(1, len(c)):
        e[i] = c[i] * k + e[i - 1] * (1 - k)
    return e


def _rsi(c, n=14):
    d = np.diff(c, prepend=c[0])
    g, p = np.where(d > 0, d, 0), np.where(d < 0, -d, 0)
    mg, mp = np.convolve(g, np.ones(n) / n)[:len(c)], np.convolve(p, np.ones(n) / n)[:len(c)]
    return 100 - 100 / (1 + mg / np.where(mp == 0, 1e-12, mp))


def rasgos(V, i, ema=None, rsi=None):
    """Rasgos de la entrada al ABRIR la vela i (solo velas < i)."""
    c, h, l, o, v, t = V['c'], V['h'], V['l'], V['o'], V['v'], V['t']
    p = o[i] if i < len(o) else c[i - 1]
    ema = _ema(c[:i]) if ema is None else ema
    rsi = _rsi(c[:i]) if rsi is None else rsi
    mx, mn = h[i - 48:i].max(), l[i - 48:i].min()
    va = v[i - 48:i - 6].mean()
    hny = datetime.fromtimestamp(t[i] if i < len(t) else t[i - 1] + BARRA, NY)
    return {'ret1h': (c[i - 1] / c[i - 13] - 1) * 100, 'ch4h': (c[i - 1] / o[i - 48] - 1) * 100, 'ret4h': (p / mx - 1) * 100,
            'ch1d': (c[i - 1] / c[i - 277] - 1) * 100, 'rsi': float(rsi[i - 1]),
            'atr': float(((h[i - 12:i] - l[i - 12:i]) / c[i - 12:i]).mean() * 100), 'tend': (p / ema[i - 1] - 1) * 100,
            'volr': float(v[i - 6:i].mean() / va) if va else 1.0, 'hora': hny.hour + hny.minute / 60, 'dia': hny.weekday(),
            'pos48': float((p - mn) / (mx - mn)) if mx > mn else 0.5}


class Datos:
    """Mismos atributos que fabrica.Datos para usar su simulador y su generador."""
    rasgos, holds, cortos, prefijo, sl_lim = RASGOS, [6, 12, 24, 36, 48], True, 'F', (0.03, 3.0)

    def __init__(self):
        F, H, L, C, ts, tok, cost, precio = [], [], [], [], [], [], [], []
        self.tokens = list(FUT)
        for k, s in enumerate(self.tokens):
            V = _velas(s)
            if len(V['t']) < 400:
                continue
            ema, rsi = _ema(V['c']), _rsi(V['c'])
            info = FUT[s]
            for i in range(288, len(V['t']) - PASOS, CADA_MUESTRA):
                if V['t'][i + PASOS - 1] - V['t'][i] > (PASOS - 1) * BARRA + 3700 or V['t'][i] - V['t'][i - 48] > 47 * BARRA + 3700:
                    continue  # cruza un cierre de sesión o el fin de semana
                p = V['o'][i]
                f = rasgos(V, i, ema, rsi)
                F.append([f[r] for r in RASGOS])
                H.append((V['h'][i:i + PASOS] / p - 1) * 100)
                L.append((V['l'][i:i + PASOS] / p - 1) * 100)
                C.append((V['c'][i:i + PASOS] / p - 1) * 100)
                ts.append(int(V['t'][i]))
                tok.append(k)
                cost.append((COMISION + 2 * info['tick'] * info['pv']) / (p * info['pv']) * 100)
                precio.append(p)
        orden = np.argsort(ts, kind='stable')
        self.n = len(ts)
        self.ts = np.array(ts, dtype=np.int64)[orden]
        self.tok = np.array(tok, dtype=np.int64)[orden]
        self.F = np.array(F, dtype=float)[orden] if F else np.zeros((0, len(RASGOS)))
        self.H, self.L, self.C = (np.array(M, dtype=float)[orden] if M else np.zeros((0, PASOS)) for M in (H, L, C))
        self.costes = np.array(cost)[orden]
        self.precio = np.array(precio)[orden]
        self.vivo = np.zeros(self.n, dtype=bool)
        self.corte = int(np.quantile(self.ts, 0.7)) if self.n else 0
        self.col = {nom: j for j, nom in enumerate(RASGOS)}


def usd(e, D, ops):
    """Pasa las operaciones (ts, %, índice) a $ por contrato."""
    return [x / 100 * D.precio[n] * FUT[D.tokens[D.tok[n]]]['pv'] for _, x, n in ops]


def stats_usd(r):
    if not r:
        return {'n': 0, 'medio': None, 'total': 0, 'gana_pct': None}
    r = [float(x) for x in r]
    return {'n': len(r), 'medio': round(sum(r) / len(r), 1), 'total': round(sum(r)), 'gana_pct': round(100 * sum(x > 0 for x in r) / len(r))}


def evaluar(e, D, desde=None):
    ops = fabrica.simular(e, D, con_idx=True)
    u = usd(e, D, ops)
    tr = [x for o, x in zip(ops, u) if o[0] + e['hold'] * BARRA < D.corte]
    te = [x for o, x in zip(ops, u) if o[0] >= D.corte]
    vi = [x for o, x in zip(ops, u) if desde and o[0] >= desde]
    return {'entreno': stats_usd(tr), 'prueba': stats_usd(te), 'vivo': stats_usd(vi), 'todo': stats_usd(u)}


def _candidatas():
    ini = db.ajuste('fut_inicio')
    return ([{**m, 'id': f'FM{i + 1}', 'desde': ini} for i, m in enumerate(MANUALES)]
            + [{**e, 'nombre': f"Generada {e['id']}"} for e in db.ajuste('fut_salon') or []])


def promovidas(D=None):
    def calc():
        D_ = D or Datos()
        out = []
        for e in _candidatas():
            if not str(e['id']).startswith('FM') and e.get('version') != fabrica.version(e):
                continue
            ev = evaluar(e, D_, e['desde'])
            v = ev['vivo']
            if v['n'] >= PROMO['vivo_n'] and (v['medio'] or 0) >= PROMO['vivo_usd'] and (v['gana_pct'] or 0) >= PROMO['vivo_gana'] and (ev['prueba']['medio'] or 0) > 0:
                out.append({**e, 'vivo': v})
        return sorted(out, key=lambda e: -e['vivo']['medio'] * e['vivo']['n'] ** 0.5)
    return datos._cacheado(('fut_promo',), 300, calc)


def _grabar_ronda(D, pob, nuevo, probadas):
    mejores = sorted(pob, key=lambda e: -fabrica._aptitud(e, D))[:5]
    lista = ([{**e, 'nombre': f"Generada {e['id']}"} for e in nuevo[:10]] + [{**c, 'desde': c['desde']} for c in _candidatas()[:len(MANUALES)]]
             + [{**e, 'id': 'FP' + str(abs(hash(fabrica.describir(e))) % 10 ** 6), 'nombre': 'Candidata de la población'} for e in mejores])
    top, vistos = [], set()
    for e in lista:
        if e['id'] in vistos:
            continue
        vistos.add(e['id'])
        ops = fabrica.simular(e, D, con_idx=True)
        u = usd(e, D, ops)
        st = evaluar(e, D, e.get('desde'))
        top.append({'id': e['id'], 'nombre': e.get('nombre'), 'regla': fabrica.describir(e), **{k: st[k] for k in ('entreno', 'prueba', 'vivo')},
                    'ops': [{'token': D.tokens[D.tok[n]], 'simbolo': FUT[D.tokens[D.tok[n]]]['nt'].split()[0], 'ts': t, 'res': round(x, 1)}
                            for (t, _, n), x in list(zip(ops, u))[-200:]]})
    conciencia.ronda({'mercado': 'futuros', 'escala': 20.0, 'gen': _ultimo['gen'], 'muestras': D.n, 'vivas': 0, 'tokens': len(FUT),
                      'probadas': probadas, 'pasan': len(nuevo), 'top': top})
    ya = set(db.ajuste('fut_promovidas') or [])
    prom = promovidas(D)
    for e in prom:
        if e['id'] not in ya:
            conciencia.promocion({**e, 'regla': fabrica.describir(e), 'nombre': 'Futuros · ' + (e.get('nombre') or e['id'])})
    db.poner_ajuste('fut_promovidas', sorted(ya | {e['id'] for e in prom}))


def evolucionar():
    D = Datos()
    return fabrica.evolucionar(D=D, manuales=MANUALES, salon_clave='fut_salon', ultimo=_ultimo, grabar=_grabar_ronda)


def senales_ahora(estrategias, barras):
    """barras = {yahoo: {t,o,h,l,c,v} arrays con la última vela CERRADA al final} (del puente de NinjaTrader, en tiempo real).
    Devuelve las entradas que cumple ahora cada estrategia (una por instrumento)."""
    out, usados = [], set()
    for e in estrategias:
        for s, V in barras.items():
            if s in usados or len(V['t']) < 300:
                continue
            i = len(V['t'])
            f = rasgos(V, i)
            if not all(fabrica._cumple(f, c) for c in e['conds']):
                continue
            info = FUT[s]
            p = V['c'][-1]
            sl_pct = float(np.clip(e['sl_k'] * f['atr'], *Datos.sl_lim))
            sl_t = max(2, int(round(p * sl_pct / 100 / info['tick'])))
            usados.add(s)
            out.append({'inst': s, 'nt': info['nt'], 'full': info['full'], 'precio': p, 'lado': e.get('lado', 1), 'sl_ticks': sl_t, 'tp_ticks': max(2, int(round(sl_t * e['rr']))),
                        'riesgo_usd': round(sl_t * info['tick'] * info['pv'] + COMISION + info['tick'] * info['pv'], 2), 'hold': int(e['hold']),
                        'estrategia': e.get('nombre') or e['id'], 'est_id': e['id'], 'vivo': e.get('vivo'), 'regla': fabrica.describir(e)})
    return out


def resumen():
    def calc():
        try:
            D = Datos()
        except Exception as ex:
            return {'error': str(ex)[:120]}
        ini = db.ajuste('fut_inicio')
        man = [{'nombre': m['nombre'], 'regla': fabrica.describir(m), **evaluar(m, D, ini)} for m in MANUALES]
        salon = [{'id': e['id'], 'regla': fabrica.describir(e), 'desde': e['desde'], **evaluar(e, D, e['desde'])} for e in (db.ajuste('fut_salon') or [])[:10]]
        return {'muestras': D.n, 'corte': D.corte, 'desde': int(D.ts[0]) if D.n else None, 'manuales': man, 'salon': salon, 'promo': PROMO,
                'promovidas': [{'id': e['id'], 'nombre': e.get('nombre'), 'regla': fabrica.describir(e), 'vivo': e['vivo']} for e in promovidas(D)],
                'evo': _ultimo['evo'], 'rondas': _ultimo['gen'], 'msg': _ultimo['msg'], 'instrumentos': {k: v['nt'] for k, v in FUT.items()}}
    return datos._cacheado(('fut_resumen',), 120, calc)


def _bucle():
    while True:
        t0 = time.time()
        try:
            actualizar()
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
    _ultimo['gen'] = max(_ultimo['gen'], conciencia.rondas('futuros'))
    if not (_hilo['t'] and _hilo['t'].is_alive()):
        _hilo['t'] = threading.Thread(target=_bucle, daemon=True)
        _hilo['t'].start()
