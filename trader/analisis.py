"""Análisis técnico de un símbolo y escáner de la lista de vigilancia. Lenguaje descriptivo: hechos, no consejos."""
from . import datos, indicadores as ind


def _r(x, d=2):
    return None if x is None else round(x, d)


def analizar(simbolo, intervalo='1d'):
    v = datos.velas(simbolo, intervalo, '2y' if intervalo == '1d' else None)
    q = datos.cotizacion(simbolo)
    c, h, l = [b['c'] for b in v], [b['h'] for b in v], [b['l'] for b in v]
    vol = [b['v'] for b in v]
    p = q['precio']
    s20, s50, s200 = ind.sma(c, 20)[-1], ind.sma(c, 50)[-1], ind.sma(c, 200)[-1]
    r = ind.rsi(c)[-1]
    ml, ms, mh = (x[-1] for x in ind.macd(c))
    mh_prev = ind.macd(c)[2][-2]
    bm, bs, bi = (x[-1] for x in ind.bollinger(c))
    a = ind.atr(h, l, c)[-1]
    k, d = ind.estocastico(h, l, c)
    sop, res = ind.pivotes(h, l)
    vmed = sum(vol[-21:-1]) / 20 if len(vol) > 21 else None
    rend = lambda n: _r((c[-1] / c[-1 - n] - 1) * 100) if len(c) > n else None
    señales = []
    if s50 and s200:
        señales.append('tendencia de fondo ALCISTA (SMA50 > SMA200)' if s50 > s200 else 'tendencia de fondo BAJISTA (SMA50 < SMA200)')
        s50p, s200p = ind.sma(c, 50)[-6], ind.sma(c, 200)[-6]
        if s50p and s200p and (s50p - s200p) * (s50 - s200) < 0:
            señales.append('⚠️ cruce reciente de SMA50/SMA200 (' + ('cruce dorado' if s50 > s200 else 'cruce de la muerte') + ')')
    if s200:
        señales.append(f'precio {"por encima" if p > s200 else "por debajo"} de la SMA200')
    if r is not None:
        if r >= 70:
            señales.append(f'RSI {r:.0f}: sobrecompra')
        elif r <= 30:
            señales.append(f'RSI {r:.0f}: sobreventa')
    if mh is not None and mh_prev is not None and mh * mh_prev < 0:
        señales.append('MACD acaba de cruzar ' + ('al alza' if mh > 0 else 'a la baja'))
    if bs and p > bs:
        señales.append('precio por encima de la banda superior de Bollinger')
    if bi and p < bi:
        señales.append('precio por debajo de la banda inferior de Bollinger')
    if vmed and vol[-1] > 2 * vmed:
        señales.append(f'volumen {vol[-1] / vmed:.1f}× la media de 20 días')
    if q.get('max_52s') and p >= q['max_52s'] * 0.98:
        señales.append('cerca de máximos de 52 semanas')
    if q.get('min_52s') and p <= q['min_52s'] * 1.02:
        señales.append('cerca de mínimos de 52 semanas')
    puntos = sum([p > (s20 or p), p > (s50 or p), p > (s200 or p), (ml or 0) > (ms or 0), 40 < (r or 50) < 70]) - \
        sum([p < (s20 or p), p < (s50 or p), p < (s200 or p), (ml or 0) < (ms or 0), (r or 50) < 40])
    sesgo = 'alcista' if puntos >= 3 else 'bajista' if puntos <= -3 else 'lateral / mixto'
    return {
        'simbolo': q['simbolo'], 'nombre': q['nombre'], 'precio': p, 'moneda': q['moneda'], 'cambio_pct': q['cambio_pct'],
        'abierto': q['abierto'], 'intervalo': intervalo,
        'rendimiento': {'1s': rend(5), '1m': rend(21), '3m': rend(63), '6m': rend(126), '1a': rend(252)},
        'medias': {'sma20': _r(s20), 'sma50': _r(s50), 'sma200': _r(s200)},
        'rsi14': _r(r, 1), 'macd': {'linea': _r(ml, 3), 'senal': _r(ms, 3), 'hist': _r(mh, 3)},
        'bollinger': {'sup': _r(bs), 'media': _r(bm), 'inf': _r(bi)},
        'atr14': _r(a, 4), 'atr_pct': _r(a / p * 100) if a else None,
        'estocastico': {'k': _r(k[-1], 1), 'd': _r(d[-1], 1)},
        'soportes': [_r(x) for x in sorted(set(sop)) if x < p][-3:], 'resistencias': [_r(x) for x in sorted(set(res)) if x > p][:3],
        'rango_52s': {'min': q.get('min_52s'), 'max': q.get('max_52s'),
                      'posicion_pct': _r((p - q['min_52s']) / (q['max_52s'] - q['min_52s']) * 100, 0) if q.get('max_52s') and q.get('min_52s') and q['max_52s'] > q['min_52s'] else None},
        'stop_sugerido_atr': {'largo': _r(p - 2 * a) if a else None, 'corto': _r(p + 2 * a) if a else None},
        'sesgo_tecnico': sesgo, 'senales': señales,
        'aviso': 'Lectura técnica automática, no es una recomendación de inversión.',
    }


def escanear(simbolos, intervalo='1d'):
    filas = []
    for s in simbolos:
        try:
            a = analizar(s, intervalo)
            filas.append({k: a[k] for k in ('simbolo', 'nombre', 'precio', 'cambio_pct', 'rsi14', 'sesgo_tecnico', 'senales', 'atr_pct')} |
                         {'rend_1m': a['rendimiento']['1m'], 'rango_52s_pct': a['rango_52s']['posicion_pct']})
        except Exception as e:
            filas.append({'simbolo': s, 'error': str(e)})
    return filas
