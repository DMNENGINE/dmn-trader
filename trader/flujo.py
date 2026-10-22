"""Flujo del mercado de cripto (datos gratuitos, sin cuenta): tasa de financiación (funding) e interés abierto de los
futuros perpetuos de Hyperliquid y desequilibrio del libro de órdenes de Coinbase. Binance bloquea estas consultas desde EE. UU."""
import json, threading, time, urllib.request

HL = 'https://api.hyperliquid.xyz/info'
_perp, _oi_hist, _libro = {'ts': 0, 'd': {}}, {}, {}
_lock = threading.Lock()


def _base(sim):
    return sim.upper().replace('-USD', '')


def perpetuos():
    """{'BTC': {'funding': tasa por hora, 'oi': interés abierto en $}} (caché de 2 min)."""
    with _lock:
        if time.time() - _perp['ts'] < 120:
            return _perp['d']
        _perp['ts'] = time.time()
    try:
        req = urllib.request.Request(HL, data=b'{"type":"metaAndAssetCtxs"}', headers={'Content-Type': 'application/json', 'User-Agent': 'dmn-trader'})
        meta, ctxs = json.loads(urllib.request.urlopen(req, timeout=10).read())
        d = {}
        for u, x in zip(meta['universe'], ctxs):
            try:
                d[u['name'].lstrip('k') if u['name'].startswith('k') and u['name'][1:].isupper() else u['name']] = {
                    'funding': float(x['funding']), 'oi': float(x['openInterest']) * float(x['markPx'])}
            except (KeyError, TypeError, ValueError):
                pass
        ahora = time.time()
        for k, v in d.items():  # historial del interés abierto para medir su cambio en ~1 h
            h = _oi_hist.setdefault(k, [])
            h.append((ahora, v['oi']))
            del h[:-40]
        with _lock:
            _perp['d'] = d
        return d
    except Exception:
        return _perp['d']


def libro(sim, banda=0.5):
    """Desequilibrio del libro de Coinbase a ±banda % del precio: +1 todo compras, -1 todo ventas (caché de 30 s)."""
    x = _libro.get(sim)
    if x and time.time() - x[0] < 30:
        return x[1]
    try:
        req = urllib.request.Request(f'https://api.exchange.coinbase.com/products/{sim.upper()}/book?level=2', headers={'User-Agent': 'dmn-trader'})
        b = json.loads(urllib.request.urlopen(req, timeout=6).read())
        bid, ask = float(b['bids'][0][0]), float(b['asks'][0][0])
        mid = (bid + ask) / 2
        cb = sum(float(p) * float(q) for p, q, *_ in b['bids'] if float(p) >= mid * (1 - banda / 100))
        ca = sum(float(p) * float(q) for p, q, *_ in b['asks'] if float(p) <= mid * (1 + banda / 100))
        v = round((cb - ca) / (cb + ca), 3) if cb + ca else 0.0
    except Exception:
        v = None
    _libro[sim] = (time.time(), v)
    if len(_libro) > 200:
        _libro.clear()
    return v


def rasgos(sim):
    """Rasgos de flujo para una entrada (vacío para acciones o si no hay datos)."""
    if not sim.upper().endswith('-USD'):
        return {}
    out = {}
    p = perpetuos().get(_base(sim))
    if p:
        out['funding'] = p['funding']
        h = _oi_hist.get(_base(sim)) or []
        viejo = next((oi for t, oi in h if 1800 <= time.time() - t <= 4200), None)
        if viejo:
            out['oi_cambio'] = round((p['oi'] / viejo - 1) * 100, 3)
    lb = libro(sim)
    if lb is not None:
        out['libro'] = lb
    return out


def en_contra(ras, d):
    """Gen flujo: no entrar si el libro empuja fuerte en contra o si ya hay demasiada gente apostando en nuestra dirección (funding extremo)."""
    return (ras.get('libro') is not None and ras['libro'] * d < -0.3) or ras.get('funding', 0) * d > 0.00005


def resumen():
    d = perpetuos()
    if not d:
        return 'Flujo: sin datos de futuros.'
    top = sorted(((k, v['funding']) for k, v in d.items() if k in ('BTC', 'ETH', 'SOL', 'XRP', 'DOGE')), key=lambda x: -abs(x[1]))
    return 'Flujo (funding/h de los perpetuos): ' + ', '.join(f"{k} {v * 100:+.4f}%" for k, v in top) + '.'
