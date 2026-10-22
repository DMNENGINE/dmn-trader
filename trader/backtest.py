"""Backtester honesto: señales al cierre de la vela, ejecución en la APERTURA de la siguiente (sin mirar al futuro),
comisiones + deslizamiento, stop/objetivo por ATR comprobados dentro de cada vela (si tocan los dos, cuenta el stop).
Incluye optimizador por rejilla con separación entrenamiento/prueba para detectar sobreajuste."""
import itertools
from . import indicadores as ind, metricas

# --- estrategias: devuelven por vela 1 (largo), -1 (corto), 0 (fuera) o None (mantener) ---


def _cruce(a, b, i):
    return a[i] is not None and b[i] is not None and a[i - 1] is not None and b[i - 1] is not None and \
        ((a[i - 1] <= b[i - 1] and a[i] > b[i]) and 1 or (a[i - 1] >= b[i - 1] and a[i] < b[i]) and -1 or 0)


def est_cruce_medias(v, rapida=20, lenta=50, tipo='ema'):
    c = [b['c'] for b in v]
    f = ind.ema if tipo == 'ema' else ind.sma
    a, b = f(c, int(rapida)), f(c, int(lenta))
    return [None] + [{1: 1, -1: -1}.get(_cruce(a, b, i)) for i in range(1, len(v))]


def est_rsi_reversion(v, n=14, sobreventa=30, sobrecompra=70):
    r = ind.rsi([b['c'] for b in v], int(n))
    out = []
    for x in r:
        out.append(None if x is None else 1 if x < sobreventa else -1 if x > sobrecompra else 0 if 45 < x < 55 else None)
    return out


def est_macd(v, rapida=12, lenta=26, senal=9):
    l, s, _ = ind.macd([b['c'] for b in v], int(rapida), int(lenta), int(senal))
    return [None] + [{1: 1, -1: -1}.get(_cruce(l, s, i)) for i in range(1, len(v))]


def est_bollinger(v, n=20, k=2.0):
    c = [b['c'] for b in v]
    m, sup, inf = ind.bollinger(c, int(n), float(k))
    out = []
    for i, x in enumerate(c):
        if m[i] is None:
            out.append(None)
        elif x < inf[i]:
            out.append(1)
        elif x > sup[i]:
            out.append(-1)
        elif i and m[i - 1] is not None and (c[i - 1] < m[i - 1] <= x or c[i - 1] > m[i - 1] >= x):
            out.append(0)  # vuelve a la media: cerrar
        else:
            out.append(None)
    return out


def est_donchian(v, entrada=20, salida=10):
    h, l, c = [b['h'] for b in v], [b['l'] for b in v], [b['c'] for b in v]
    es, ei = ind.donchian(h, l, int(entrada))
    ss, si = ind.donchian(h, l, int(salida))
    out, pos = [], 0
    for i, x in enumerate(c):
        s = None
        if es[i] is not None:
            if x > es[i]:
                s = 1
            elif x < ei[i]:
                s = -1
            elif pos == 1 and x < si[i]:
                s = 0
            elif pos == -1 and x > ss[i]:
                s = 0
        if s is not None:
            pos = s
        out.append(s)
    return out


def est_rsi2(v, tendencia=200, umbral=10, salida=5):
    c = [b['c'] for b in v]
    t, r, sal = ind.sma(c, int(tendencia)), ind.rsi(c, 2), ind.sma(c, int(salida))
    out = []
    for i, x in enumerate(c):
        if t[i] is None or r[i] is None or sal[i] is None:
            out.append(None)
        elif x > t[i] and r[i] < umbral:
            out.append(1)
        elif x > sal[i]:
            out.append(0)
        else:
            out.append(None)
    return out


def est_comprar_mantener(v):
    return [1] + [None] * (len(v) - 1)


ESTRATEGIAS = {
    'cruce_medias': (est_cruce_medias, {'rapida': 20, 'lenta': 50, 'tipo': 'ema'}, 'Cruce de medias (tendencia)'),
    'rsi_reversion': (est_rsi_reversion, {'n': 14, 'sobreventa': 30, 'sobrecompra': 70}, 'RSI: comprar sobreventa, vender sobrecompra'),
    'macd': (est_macd, {'rapida': 12, 'lenta': 26, 'senal': 9}, 'Cruce MACD / señal'),
    'bollinger': (est_bollinger, {'n': 20, 'k': 2.0}, 'Reversión a la media con Bandas de Bollinger'),
    'donchian': (est_donchian, {'entrada': 20, 'salida': 10}, 'Ruptura de canal Donchian (Tortugas)'),
    'rsi2': (est_rsi2, {'tendencia': 200, 'umbral': 10, 'salida': 5}, 'RSI(2) de Connors a favor de la tendencia (solo largos)'),
    'comprar_mantener': (est_comprar_mantener, {}, 'Comprar y mantener (referencia)'),
}

ANIO = {'1d': 252, '1wk': 52, '1mo': 12, '60m': 252 * 7, '30m': 252 * 13, '15m': 252 * 26, '5m': 252 * 78, '1m': 252 * 390}


def ejecutar(velas, estrategia='cruce_medias', params=None, capital=10000, comision_pct=0.05, comision_fija=0.0,
             deslizamiento_pct=0.02, cortos=False, tamano='todo', fraccion=100, riesgo_pct=1.0,
             stop_atr=0.0, objetivo_atr=0.0, intervalo='1d', con_curva=True):
    fn, defecto, _ = ESTRATEGIAS[estrategia]
    p = {**defecto, **{k: v for k, v in (params or {}).items() if k in defecto}}
    sen = fn(velas, **p)
    a = ind.atr([b['h'] for b in velas], [b['l'] for b in velas], [b['c'] for b in velas], 14)
    efectivo, pos, entrada, stop, obj, t_ent, riesgo_ini = float(capital), 0.0, 0.0, None, None, None, 0.0
    trades, curva_eq, objetivo_sig = [], [], 0
    cpct, slip = comision_pct / 100, deslizamiento_pct / 100

    def cerrar(precio, t, motivo):
        nonlocal efectivo, pos, stop, obj
        px = precio * (1 - slip) if pos > 0 else precio * (1 + slip)
        com = abs(pos) * px * cpct + comision_fija
        pnl = pos * (px - entrada) - com
        efectivo += pos * px - com
        trades.append({'entrada_t': t_ent, 'salida_t': t, 'lado': 'largo' if pos > 0 else 'corto', 'cantidad': round(abs(pos), 6),
                       'entrada': round(entrada, 6), 'salida': round(px, 6), 'pnl': round(pnl, 2),
                       'r': round(pnl / riesgo_ini, 2) if riesgo_ini else None, 'motivo': motivo})
        pos, stop, obj = 0.0, None, None

    def abrir(lado, precio, t, i):
        nonlocal efectivo, pos, entrada, stop, obj, t_ent, riesgo_ini
        px = precio * (1 + slip) if lado > 0 else precio * (1 - slip)
        eq = efectivo
        dist = (a[i - 1] or 0) * stop_atr if stop_atr and i else 0
        if tamano == 'riesgo' and dist:
            qty = eq * riesgo_pct / 100 / dist
        elif tamano == 'fraccion':
            qty = eq * fraccion / 100 / px
        else:
            qty = eq / px
        qty = min(qty, eq / (px * (1 + cpct)))  # nunca más de lo que hay (sin apalancamiento)
        if qty <= 0:
            return
        com = qty * px * cpct + comision_fija
        efectivo -= lado * qty * px + com
        pos, entrada, t_ent = lado * qty, px, t
        stop = px - lado * dist if dist else None
        obj = px + lado * (a[i - 1] or 0) * objetivo_atr if objetivo_atr and i else None
        riesgo_ini = qty * dist if dist else qty * px * 0.02  # sin stop: R = 2 % de la posición

    for i, b in enumerate(velas):
        # 1) ejecutar en la apertura la señal de la vela anterior
        if i and objetivo_sig is not None:
            quiero = objetivo_sig if cortos or objetivo_sig >= 0 else 0
            actual = (pos > 0) - (pos < 0)
            if quiero != actual:
                if pos:
                    cerrar(b['o'], b['t'], 'señal')
                if quiero:
                    abrir(quiero, b['o'], b['t'], i)
        # 2) stop / objetivo dentro de la vela
        if pos and stop is not None and ((pos > 0 and b['l'] <= stop) or (pos < 0 and b['h'] >= stop)):
            cerrar(min(stop, b['o']) if pos > 0 else max(stop, b['o']), b['t'], 'stop')
        elif pos and obj is not None and ((pos > 0 and b['h'] >= obj) or (pos < 0 and b['l'] <= obj)):
            cerrar(max(obj, b['o']) if pos > 0 else min(obj, b['o']), b['t'], 'objetivo')
        objetivo_sig = sen[i]
        curva_eq.append(efectivo + pos * b['c'])
    if pos:
        cerrar(velas[-1]['c'], velas[-1]['t'], 'fin del periodo')
        curva_eq[-1] = efectivo

    rets, dd = metricas.curva(curva_eq)
    sharpe, sortino = metricas.ratios(rets, ANIO.get(intervalo, 252))
    anios = max((velas[-1]['t'] - velas[0]['t']) / 31557600, 1e-9)
    final = curva_eq[-1]
    bh = (velas[-1]['c'] / velas[0]['o'] - 1) * 100
    exp = sum(tr['salida_t'] - tr['entrada_t'] for tr in trades) / (velas[-1]['t'] - velas[0]['t'] or 1) * 100
    res = {
        'estrategia': estrategia, 'params': p, 'capital': capital, 'final': round(final, 2),
        'retorno_pct': round((final / capital - 1) * 100, 2),
        'cagr_pct': round(((final / capital) ** (1 / anios) - 1) * 100, 2) if final > 0 else -100,
        'max_drawdown_pct': round(dd, 2), 'sharpe': sharpe, 'sortino': sortino,
        'comprar_mantener_pct': round(bh, 2), 'tiempo_en_mercado_pct': round(exp, 1),
        'desde': velas[0]['t'], 'hasta': velas[-1]['t'], 'velas': len(velas),
        **metricas.de_trades([t['pnl'] for t in trades], [t['r'] for t in trades]),
    }
    res['calmar'] = round(res['cagr_pct'] / dd, 2) if dd else None
    res['montecarlo'] = metricas.montecarlo([t['pnl'] for t in trades], capital)
    if con_curva:
        paso = max(1, len(velas) // 600)
        res['curva'] = [{'t': velas[i]['t'], 'v': round(curva_eq[i], 2)} for i in range(0, len(velas), paso)]
        res['lista_trades'] = trades[-300:]
    return res


def _rejilla(rangos):
    claves = list(rangos)
    for combo in itertools.product(*[rangos[k] for k in claves]):
        yield dict(zip(claves, combo))


def optimizar(velas, estrategia, rangos, criterio='sharpe', entreno_pct=70, maximo=400, **kw):
    """Prueba combinaciones en el tramo de entrenamiento y valida el top 5 en el tramo de prueba (datos no vistos)."""
    corte = int(len(velas) * entreno_pct / 100)
    ent, pru = velas[:corte], velas[corte:]
    if len(ent) < 60 or len(pru) < 30:
        raise ValueError('pocos datos para optimizar: usa un rango más largo')
    filas = []
    for i, p in enumerate(_rejilla(rangos)):
        if i >= maximo:
            break
        r = ejecutar(ent, estrategia, p, con_curva=False, **kw)
        filas.append((r.get(criterio) if r.get(criterio) is not None else -1e9, p, r))
    filas.sort(key=lambda x: x[0], reverse=True)
    top = []
    for _, p, r in filas[:5]:
        t = ejecutar(pru, estrategia, p, con_curva=False, **kw)
        top.append({'params': p, 'entreno': {k: r.get(k) for k in ('retorno_pct', 'sharpe', 'max_drawdown_pct', 'trades', 'factor_beneficio')},
                    'prueba': {k: t.get(k) for k in ('retorno_pct', 'sharpe', 'max_drawdown_pct', 'trades', 'factor_beneficio')}})
    aviso = None
    if top and (top[0]['prueba']['sharpe'] or 0) < (top[0]['entreno']['sharpe'] or 0) * 0.5:
        aviso = 'El mejor ajuste rinde mucho peor en datos no vistos: huele a sobreajuste.'
    return {'probadas': len(filas), 'criterio': criterio, 'corte_t': velas[corte]['t'], 'top': top, 'aviso': aviso}
