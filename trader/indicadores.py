"""Indicadores técnicos sobre listas (sin numpy). Devuelven listas del mismo largo con None donde aún no hay datos."""
import math


def sma(x, n):
    out, s = [None] * len(x), 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(x, n):
    out, k, e = [None] * len(x), 2 / (n + 1), None
    for i, v in enumerate(x):
        if v is None:
            continue
        if e is None:
            if i >= n - 1 and all(a is not None for a in x[i - n + 1:i + 1]):
                e = sum(x[i - n + 1:i + 1]) / n
            else:
                continue
        else:
            e = v * k + e * (1 - k)
        out[i] = e
    return out


def rsi(c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = p = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        g += max(d, 0)
        p += max(-d, 0)
    g, p = g / n, p / n
    out[n] = 100 if p == 0 else 100 - 100 / (1 + g / p)
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        p = (p * (n - 1) + max(-d, 0)) / n
        out[i] = 100 if p == 0 else 100 - 100 / (1 + g / p)
    return out


def macd(c, rapida=12, lenta=26, senal=9):
    a, b = ema(c, rapida), ema(c, lenta)
    linea = [x - y if x is not None and y is not None else None for x, y in zip(a, b)]
    s = ema(linea, senal)
    hist = [x - y if x is not None and y is not None else None for x, y in zip(linea, s)]
    return linea, s, hist


def bollinger(c, n=20, k=2.0):
    m = sma(c, n)
    sup, inf = [None] * len(c), [None] * len(c)
    for i in range(n - 1, len(c)):
        ventana = c[i - n + 1:i + 1]
        d = math.sqrt(sum((v - m[i]) ** 2 for v in ventana) / n)
        sup[i], inf[i] = m[i] + k * d, m[i] - k * d
    return m, sup, inf


def atr(h, l, c, n=14):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    out, a = [None] * len(c), None
    for i in range(len(c)):
        if i == n - 1:
            a = sum(tr[:n]) / n
        elif i >= n:
            a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def estocastico(h, l, c, n=14, d=3):
    k = [None] * len(c)
    for i in range(n - 1, len(c)):
        hh, ll = max(h[i - n + 1:i + 1]), min(l[i - n + 1:i + 1])
        k[i] = 50.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100
    kk = [v for v in k if v is not None]
    dd = sma(kk, d)
    return k, [None] * (len(c) - len(dd)) + dd


def donchian(h, l, n=20):
    sup, inf = [None] * len(h), [None] * len(h)
    for i in range(n, len(h)):  # canal de las n velas ANTERIORES (para rupturas sin mirar al futuro)
        sup[i], inf[i] = max(h[i - n:i]), min(l[i - n:i])
    return sup, inf


def vwap(velas):
    """VWAP que se reinicia cada día (útil en intradía)."""
    import time as _t
    out, pv, vv, dia = [], 0.0, 0.0, None
    for b in velas:
        d = _t.gmtime(b['t'])[:3]
        if d != dia:
            pv = vv = 0.0
            dia = d
        tp = (b['h'] + b['l'] + b['c']) / 3
        pv += tp * (b['v'] or 0)
        vv += b['v'] or 0
        out.append(pv / vv if vv else tp)
    return out


def pivotes(h, l, ventana=5, maximo=4):
    """Soportes y resistencias: mínimos y máximos locales recientes."""
    sop, res = [], []
    for i in range(ventana, len(h) - ventana):
        if h[i] == max(h[i - ventana:i + ventana + 1]):
            res.append(h[i])
        if l[i] == min(l[i - ventana:i + ventana + 1]):
            sop.append(l[i])
    return sop[-maximo:], res[-maximo:]
