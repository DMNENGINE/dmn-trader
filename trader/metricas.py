"""Métricas profesionales de rendimiento: comunes al backtest, a las cuentas simuladas y al examen de "listo para real"."""
import math, random


def curva(equity):
    """equity: lista de valores. Devuelve retornos por periodo y drawdown máximo (en % positivo)."""
    rets = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity)) if equity[i - 1]]
    pico, dd = -1e18, 0.0
    for v in equity:
        pico = max(pico, v)
        if pico > 0:
            dd = max(dd, (pico - v) / pico)
    return rets, dd * 100


def ratios(rets, periodos_anio=252):
    if len(rets) < 2:
        return None, None
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
    neg = [r for r in rets if r < 0]
    sdn = math.sqrt(sum(r * r for r in neg) / len(rets)) if neg else 0
    sharpe = m / sd * math.sqrt(periodos_anio) if sd else None
    sortino = m / sdn * math.sqrt(periodos_anio) if sdn else None
    return (round(sharpe, 2) if sharpe is not None else None), (round(sortino, 2) if sortino is not None else None)


def de_trades(pnls, rs=None):
    """pnls: beneficio de cada trade cerrado. rs: múltiplos de R (opcional)."""
    n = len(pnls)
    if not n:
        return {'trades': 0}
    gan, per = [p for p in pnls if p > 0], [p for p in pnls if p <= 0]
    bruto_g, bruto_p = sum(gan), -sum(per)
    racha = mejor_racha = peor_racha = 0
    for p in pnls:
        racha = (racha + 1 if racha >= 0 else 1) if p > 0 else (racha - 1 if racha <= 0 else -1)
        mejor_racha, peor_racha = max(mejor_racha, racha), min(peor_racha, racha)
    out = {
        'trades': n, 'ganadores': len(gan), 'perdedores': len(per),
        'tasa_acierto': round(len(gan) / n * 100, 1),
        'beneficio_neto': round(sum(pnls), 2),
        'factor_beneficio': round(bruto_g / bruto_p, 2) if bruto_p else None,
        'esperanza': round(sum(pnls) / n, 2),
        'ganancia_media': round(bruto_g / len(gan), 2) if gan else 0,
        'perdida_media': round(-bruto_p / len(per), 2) if per else 0,
        'mayor_ganancia': round(max(pnls), 2), 'mayor_perdida': round(min(pnls), 2),
        'racha_ganadora': mejor_racha, 'racha_perdedora': -peor_racha,
    }
    if out['perdida_media']:
        out['ratio_gan_per'] = round(out['ganancia_media'] / -out['perdida_media'], 2)
    rs = [r for r in (rs or []) if r is not None]
    if rs:
        out['r_medio'] = round(sum(rs) / len(rs), 2)
        out['r_total'] = round(sum(rs), 2)
    return out


def montecarlo(pnls, capital, n=500, semilla=7):
    """Baraja el orden de los trades n veces: qué drawdown y resultado cabe esperar por pura suerte del orden."""
    if len(pnls) < 5:
        return None
    rnd, dds, finales = random.Random(semilla), [], []
    for _ in range(n):
        orden = pnls[:]
        rnd.shuffle(orden)
        eq, v = [capital], capital
        for p in orden:
            v += p
            eq.append(v)
        dds.append(curva(eq)[1])
        finales.append(v)
    dds.sort()
    finales.sort()
    pct = lambda xs, q: round(xs[min(len(xs) - 1, int(q * len(xs)))], 2)
    return {'dd_mediano': pct(dds, .5), 'dd_95': pct(dds, .95), 'dd_peor': round(dds[-1], 2),
            'final_5': pct(finales, .05), 'final_mediano': pct(finales, .5),
            'prob_perdida': round(sum(1 for f in finales if f < capital) / n * 100, 1)}
