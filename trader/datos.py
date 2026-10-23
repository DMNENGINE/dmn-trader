"""Datos de mercado REALES (Yahoo Finance, sin clave): cotizaciones, velas, búsqueda de símbolos y divisas.
Cubre acciones, ETF, índices (^GSPC), divisas (EURUSD=X), cripto (BTC-USD), futuros (GC=F) y materias primas.
Todo se cachea en memoria para no martillear a Yahoo (cotización 8 s, velas según intervalo)."""
import json, time, threading, urllib.request, urllib.parse, urllib.error
from . import onchain

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) DMN-Trader/1.0'
BASES = ['https://query1.finance.yahoo.com', 'https://query2.finance.yahoo.com']
INTERVALOS = {'1m': '5d', '2m': '5d', '5m': '1mo', '15m': '1mo', '30m': '1mo', '60m': '6mo', '1h': '6mo',
              '1d': '5y', '1wk': '10y', '1mo': 'max'}
TTL_VELAS = {'1m': 20, '2m': 30, '5m': 45, '15m': 90, '30m': 120, '60m': 300, '1h': 300, '1d': 600, '1wk': 3600, '1mo': 3600}

_cache, _lock = {}, threading.Lock()
# velas en TIEMPO REAL de NinjaTrader para los futuros ('MES=F'…): las manda el PC cada minuto (POST /api/futuros/velas).
# Si son de hace menos de 10 min se usan en vez de Yahoo (que da los futuros de CME con ~10 min de retraso).
NT, NT_FRESCO = {}, 600


def recibir_nt(d):
    for s, x in (d or {}).items():
        if s.endswith('=F'):
            NT[s] = {'ts': time.time(), '1m': x.get('1m') or [], '5m': x.get('5m') or []}
    return {'ok': True, 'simbolos': sorted(NT)}


def _nt(simbolo, intervalo):
    x = NT.get(simbolo)
    if not x or time.time() - x['ts'] > NT_FRESCO:
        return None
    if intervalo in ('1m', '5m'):
        return [dict(b) for b in x[intervalo]] or None
    seg = {'15m': 900}.get(intervalo)  # 1 h sigue en Yahoo: hace falta más historia que la que se manda
    if not seg or not x['5m']:
        return None
    out = []
    for b in x['5m']:  # 15 min a partir de las de 5 min
        t = b['t'] // seg * seg
        if out and out[-1]['t'] == t:
            o = out[-1]
            o['h'], o['l'], o['c'], o['v'] = max(o['h'], b['h']), min(o['l'], b['l']), b['c'], o['v'] + b['v']
        else:
            out.append({**b, 't': t})
    return out


class ErrorDatos(Exception):
    pass


def _get(ruta, timeout=12):
    ultimo = None
    for base in BASES:
        req = urllib.request.Request(base + ruta, headers={'User-Agent': UA, 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            ultimo = f'HTTP {e.code}'
            if e.code == 404:
                break
        except Exception as e:  # red caída, timeout…
            ultimo = str(e)
    raise ErrorDatos(f'Yahoo no responde ({ultimo})')


def _cacheado(clave, ttl, fn):
    ahora = time.time()
    with _lock:
        c = _cache.get(clave)
        if c and ahora - c[0] < ttl:
            return c[1]
    v = fn()
    with _lock:
        _cache[clave] = (ahora, v)
    return v


def _chart(simbolo, intervalo, rango=None, inicio=None, fin=None):
    q = {'interval': intervalo, 'includePrePost': 'false', 'events': 'div,split'}
    if inicio:
        q.update(period1=int(inicio), period2=int(fin or time.time()))
    else:
        q['range'] = rango or INTERVALOS.get(intervalo, '1y')
    d = _get(f'/v8/finance/chart/{urllib.parse.quote(simbolo)}?{urllib.parse.urlencode(q)}')
    ch = d.get('chart') or {}
    if ch.get('error'):
        raise ErrorDatos(f"{simbolo}: {ch['error'].get('description') or ch['error'].get('code')}")
    res = (ch.get('result') or [None])[0]
    if not res:
        raise ErrorDatos(f'{simbolo}: sin datos')
    return res


def mercado_abierto(meta):
    t = meta.get('instrumentType', '')
    if t == 'CRYPTOCURRENCY':
        return True
    per = (meta.get('currentTradingPeriod') or {}).get('regular') or {}
    ahora = time.time()
    if per.get('start') and per.get('end'):
        if per['start'] <= ahora <= per['end']:
            return True
        if t == 'CURRENCY':  # forex: 24/5, Yahoo da un periodo "regular" corto; vale si cotizó hace poco
            return ahora - (meta.get('regularMarketTime') or 0) < 900
        return False
    return ahora - (meta.get('regularMarketTime') or 0) < 900


# --- Coinbase (cripto en tiempo real, con compra/venta para simular el spread) ---
CB = 'https://api.exchange.coinbase.com'
CB_GRAN = {'1m': 60, '5m': 300, '15m': 900, '60m': 3600, '1h': 3600, '1d': 86400}


def _cb(ruta, timeout=8):
    req = urllib.request.Request(CB + ruta, headers={'User-Agent': UA, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def es_coinbase(simbolo):
    try:
        prods = _cacheado(('cbp',), 3600, lambda: {p['id'] for p in _cb('/products') if p.get('status') == 'online'})
    except Exception:
        return False
    return simbolo in prods


def _cotizacion_cb(simbolo):
    t = _cb(f'/products/{simbolo}/ticker')
    st = _cacheado(('cbs', simbolo), 60, lambda: _cb(f'/products/{simbolo}/stats'))
    p, ab = float(t['price']), float(st.get('open') or 0)
    import calendar
    ts = calendar.timegm(time.strptime(t['time'][:19], '%Y-%m-%dT%H:%M:%S'))
    return {'simbolo': simbolo, 'nombre': simbolo.replace('-USD', '') + ' (Coinbase)', 'precio': p, 'cierre_anterior': ab or None,
            'cambio': round(p - ab, 6) if ab else None, 'cambio_pct': round((p / ab - 1) * 100, 3) if ab else None,
            'max_dia': float(st['high']), 'min_dia': float(st['low']), 'volumen': float(st.get('volume') or 0),
            'max_52s': None, 'min_52s': None, 'moneda': 'USD', 'tipo': 'CRYPTOCURRENCY', 'bolsa': 'Coinbase', 'zona': 'UTC',
            'ts': ts, 'abierto': True, 'bid': float(t['bid']), 'ask': float(t['ask']), 'fuente': 'coinbase'}


def _velas_cb(simbolo, intervalo):
    d = _cb(f'/products/{simbolo}/candles?granularity={CB_GRAN[intervalo]}')
    return [{'t': x[0], 'o': x[3], 'h': x[2], 'l': x[1], 'c': x[4], 'v': x[5]} for x in reversed(d)]


def cotizacion(simbolo):
    """Último precio y contexto. Devuelve dict con precio, cambio %, máximos/mínimos, moneda, tipo, abierto…"""
    simbolo = simbolo.strip().upper()
    if onchain.es(simbolo):  # memecoins de Solana (GMGN)
        try:
            return onchain.cotizacion(simbolo)
        except onchain.ErrorOnchain as e:
            raise ErrorDatos(str(e))
    if es_coinbase(simbolo):
        try:
            return _cacheado(('qcb', simbolo), 3, lambda: _cotizacion_cb(simbolo))
        except Exception:
            pass  # si Coinbase falla, Yahoo

    def pedir():
        res = _chart(simbolo, '1m', '1d')
        m = res['meta']
        prev = m.get('chartPreviousClose') or m.get('previousClose')
        precio = m.get('regularMarketPrice')
        if precio is None:
            raise ErrorDatos(f'{simbolo}: sin precio')
        return {
            'simbolo': m.get('symbol', simbolo), 'nombre': m.get('longName') or m.get('shortName') or simbolo,
            'precio': precio, 'cierre_anterior': prev,
            'cambio': round(precio - prev, 6) if prev else None,
            'cambio_pct': round((precio / prev - 1) * 100, 3) if prev else None,
            'max_dia': m.get('regularMarketDayHigh'), 'min_dia': m.get('regularMarketDayLow'),
            'volumen': m.get('regularMarketVolume'), 'max_52s': m.get('fiftyTwoWeekHigh'), 'min_52s': m.get('fiftyTwoWeekLow'),
            'moneda': (m.get('currency') or 'USD').upper(), 'tipo': m.get('instrumentType', ''),
            'bolsa': m.get('fullExchangeName') or m.get('exchangeName'), 'zona': m.get('exchangeTimezoneName'),
            'ts': m.get('regularMarketTime'), 'abierto': mercado_abierto(m),
        }
    q = _cacheado(('q', simbolo), 8, pedir)
    v = _nt(simbolo, '1m') if simbolo.endswith('=F') else None
    if v:  # precio en tiempo real de NinjaTrader
        p, prev = v[-1]['c'], q.get('cierre_anterior')
        q = {**q, 'precio': p, 'ts': v[-1]['t'] + 60, 'fuente': 'ninjatrader', 'cambio': round(p - prev, 6) if prev else None,
             'cambio_pct': round((p / prev - 1) * 100, 3) if prev else None}
    return q


def velas(simbolo, intervalo='1d', rango=None, inicio=None, fin=None):
    """Lista de velas [{t,o,h,l,c,v}] (t en segundos UTC). Diario/semanal ajustado por splits por Yahoo."""
    simbolo = simbolo.strip().upper()
    if intervalo == '1h':
        intervalo = '60m'
    if onchain.es(simbolo):
        try:
            return onchain.velas(simbolo, intervalo, rango, inicio, fin)
        except onchain.ErrorOnchain as e:
            raise ErrorDatos(str(e))
    if simbolo.endswith('=F') and not (rango or inicio):
        v = _nt(simbolo, intervalo)
        if v:
            return v
    if intervalo in ('1m', '5m', '15m', '60m') and not (rango or inicio) and es_coinbase(simbolo):
        try:  # intradía de cripto: Coinbase (300 velas, al segundo)
            return _cacheado(('vcb', simbolo, intervalo), 15, lambda: _velas_cb(simbolo, intervalo))
        except Exception:
            pass

    def pedir():
        res = _chart(simbolo, intervalo, rango, inicio, fin)
        ts = res.get('timestamp') or []
        q = ((res.get('indicators') or {}).get('quote') or [{}])[0]
        out = []
        for i, t in enumerate(ts):
            o, h, l, c, v = (q.get(k, [None] * len(ts))[i] for k in ('open', 'high', 'low', 'close', 'volume'))
            if None in (o, h, l, c):
                continue
            out.append({'t': t, 'o': o, 'h': h, 'l': l, 'c': c, 'v': v or 0})
        if not out:
            raise ErrorDatos(f'{simbolo}: sin velas para {intervalo}')
        return out
    return _cacheado(('v', simbolo, intervalo, rango, inicio, fin), TTL_VELAS.get(intervalo, 300), pedir)


def buscar(texto):
    d = _cacheado(('s', texto.lower()), 3600, lambda: _get(
        '/v1/finance/search?' + urllib.parse.urlencode({'q': texto, 'quotesCount': 10, 'newsCount': 5, 'lang': 'es-ES'})))
    return {
        'resultados': [{'simbolo': x.get('symbol'), 'nombre': x.get('longname') or x.get('shortname'),
                        'tipo': x.get('quoteType'), 'bolsa': x.get('exchDisp')} for x in d.get('quotes', []) if x.get('symbol')],
        'noticias': [{'titulo': n.get('title'), 'fuente': n.get('publisher'), 'url': n.get('link'),
                      'ts': n.get('providerPublishTime')} for n in d.get('news', [])],
    }


def fx(desde, hacia):
    """Tipo de cambio (cuántas unidades de `hacia` vale 1 de `desde`)."""
    desde, hacia = (desde or 'USD').upper(), (hacia or 'USD').upper()
    if desde == hacia:
        return 1.0
    divisor = 1.0
    if desde == 'GBX':  # peniques de Londres
        desde, divisor = 'GBP', 100.0
    if desde == hacia:
        return 1 / divisor
    return cotizacion(f'{desde}{hacia}=X')['precio'] / divisor
