"""Mercado ON-CHAIN (memecoins de Solana) con datos de GMGN (openapi.gmgn.ai, solo lectura: basta la API key, nunca la clave
privada; aquí no se compra nada de verdad). Da al enjambre un universo de tokens en tendencia que pasan un filtro anti-rug,
velas y precio con costes de DEX realistas, y el flujo del smart money (compras − ventas de las wallets listas en la última hora).
Símbolos: 'TICKER@SOL' (las direcciones de Solana distinguen mayúsculas y el motor lo pone todo en mayúsculas, así que el
símbolo se traduce a la dirección con un mapa guardado en la base de datos). Solo funciona en el equipo que tiene la key
(~/.config/gmgn/.env o GMGN_API_KEY); en los demás, disponible() es False y el enjambre sigue como siempre."""
import collections, json, os, re, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from . import db

HOST = 'https://openapi.gmgn.ai'
CADENAS = {'SOL': 'sol'}
RESOL = {'1m': '1m', '5m': '5m', '15m': '15m', '60m': '1h', '1h': '1h', '1d': '1d'}
VENTANA = {'1m': 6 * 3600, '5m': 2 * 86400, '15m': 5 * 86400, '1h': 20 * 86400, '1d': 365 * 86400}
TTL = {'1m': 15, '5m': 45, '15m': 90, '1h': 300, '1d': 3600}
# Costes MEDIDOS en la primera operación real (2026-10-22, DACITY con 1 $): el precio subió un 15,1 % y la wallet ganó ≈ 9 %
# (recuperando el alquiler de las cuentas de token) → ≈ 6 % ida y vuelta. Por lado: 1 % de GMGN + ≈ 1-3 % de comisiones del
# DEX/launchpad + prioridad y propina, que con importes tan pequeños pesan. Rutas raras (DACITY → TTWO → USDC → SOL) suman más.
SPREAD_PCT = 1.0        # spread + impacto + rutas (ida y vuelta lo paga entero)
COMISION_PCT = 2.5      # por lado → 2 × (2,5 + 0,01) + 1,0 ≈ 6 % ida y vuelta, como lo medido
N_TOKENS, CADA, PAGINAS = 8, 900, 12
FILTRO = {'liquidez_min': 150_000, 'volumen_min': 500_000, 'rug_max': 0.3, 'top10_max': 0.4, 'edad_min_h': 3}
SMART_VENTANA = 3600
MAPA = 'onchain_mapa'

_cache, _lock = {}, threading.Lock()
_pausa = {'hasta': 0}
# GMGN admite ≈ 20 peticiones/s por key (x-ratelimit-limit, medido 2026-10-25: se recupera en < 1 s) y puede banear la key si
# se abusa. Todo el motor (este cliente y gmgn-cli) comparte este cupo; las ráfagas de la fábrica o el laboratorio esperan turno.
POR_SEG = 12
_turnos, _turno_lock = collections.deque(), threading.Lock()
_smart = {'ts': 0, 'trades': {}}  # tx → (ts, dirección, ±usd)
_key = {'v': None, 'ts': 0}


class ErrorOnchain(Exception):
    pass


def _api_key():
    if time.time() - _key['ts'] < 300:
        return _key['v']
    k = os.environ.get('GMGN_API_KEY')
    if not k:
        try:
            with open(os.path.join(os.path.expanduser('~'), '.config', 'gmgn', '.env'), encoding='utf-8') as f:
                for ln in f:
                    if ln.startswith('GMGN_API_KEY='):
                        k = ln.split('=', 1)[1].strip().strip('"\'')
        except OSError:
            pass
    _key.update(v=k or None, ts=time.time())
    return _key['v']


def disponible():
    return bool(_api_key())


def turno(espera_max=20.0):
    """Reserva un hueco del cupo compartido; espera (como mucho espera_max s) en vez de fallar, también si GMGN pidió pausa."""
    fin = time.time() + espera_max
    while True:
        with _turno_lock:
            ahora = time.time()
            if ahora >= _pausa['hasta']:
                while _turnos and ahora - _turnos[0] >= 1:
                    _turnos.popleft()
                if len(_turnos) < POR_SEG:
                    _turnos.append(ahora)
                    return
                dormir = 1 - (ahora - _turnos[0])
            else:
                dormir = _pausa['hasta'] - ahora
        if time.time() + dormir > fin:
            raise ErrorOnchain('GMGN: límite de peticiones, esperando')
        time.sleep(min(dormir, 1) + 0.01)


def _api(ruta, **q):
    turno()
    k = _api_key()
    if not k:
        raise ErrorOnchain('GMGN: sin API key en este equipo')
    q.update(timestamp=int(time.time()), client_id=str(uuid.uuid4()))
    req = urllib.request.Request(f'{HOST}{ruta}?{urllib.parse.urlencode(q, doseq=True)}',
                                 headers={'X-APIKEY': k, 'Content-Type': 'application/json', 'User-Agent': 'dmn-trader'})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                _pausa['hasta'] = max(time.time() + 5, float(e.headers.get('x-ratelimit-reset') or 0))
            except ValueError:
                _pausa['hasta'] = time.time() + 30
        raise ErrorOnchain(f'GMGN {ruta}: HTTP {e.code}')
    except Exception as e:
        raise ErrorOnchain(f'GMGN {ruta}: {e}')
    if d.get('code') != 0:
        raise ErrorOnchain(f"GMGN {ruta}: {d.get('message') or d.get('error') or d.get('code')}")
    d = d.get('data')
    while isinstance(d, dict) and 'code' in d and 'data' in d:  # algunas rutas vienen envueltas dos veces
        d = d['data']
    return d


def _cacheado(clave, ttl, fn):
    with _lock:
        c = _cache.get(clave)
        if c and time.time() - c[0] < ttl:
            return c[1]
    v = fn()
    with _lock:
        _cache[clave] = (time.time(), v)
    return v

# ---------------- símbolos ----------------


def mapa():
    return _cacheado(('mapa',), 30, lambda: db.ajuste(MAPA, {}) or {})


def es(simbolo):
    return '@' in simbolo and simbolo.rsplit('@', 1)[1].upper() in CADENAS


def info(simbolo):
    x = mapa().get(simbolo.upper())
    if not x:
        raise ErrorOnchain(f'{simbolo}: token on-chain desconocido')
    return x


def registrar(cadena, direccion, ticker, nombre=''):
    """Devuelve el símbolo 'TICKER@SOL' de esa dirección (lo crea si es nuevo; si el ticker ya está cogido, añade la dirección)."""
    m = db.ajuste(MAPA, {}) or {}
    for s, x in m.items():
        if x['address'] == direccion:
            return s
    base = re.sub(r'[^A-Z0-9]', '', (ticker or '').upper())[:12] or 'TOKEN'
    suf = cadena.upper()
    s = f'{base}@{suf}'
    if s in m:
        s = f'{base}-{direccion[:4].upper()}@{suf}'
    m[s] = {'chain': CADENAS[suf], 'address': direccion, 'nombre': (nombre or ticker or '')[:60], 'alta': int(time.time())}
    db.poner_ajuste(MAPA, m)
    with _lock:
        _cache.pop(('mapa',), None)
    return s

# ---------------- velas y precio ----------------


def _dias(rango):
    m = re.fullmatch(r'(\d+)(d|mo|y)', rango or '')
    return int(m[1]) * {'d': 1, 'mo': 30, 'y': 365}[m[2]] if m else None


def velas(simbolo, intervalo='1m', rango=None, inicio=None, fin=None):
    """[{t,o,h,l,c,v}] (t en segundos, v en dólares) como datos.velas."""
    x = info(simbolo)
    res = RESOL.get(intervalo)
    if not res:
        raise ErrorOnchain(f'{simbolo}: intervalo {intervalo} no disponible on-chain')
    historico = bool(rango or inicio)
    fin = int(fin or time.time())
    inicio = int(inicio or fin - ((_dias(rango) or 0) * 86400 or VENTANA[res]))

    def pedir():  # GMGN da como mucho 100 velas por petición (las más recientes de la ventana): se pagina hacia atrás
        velas_, hasta = {}, fin
        for _ in range(PAGINAS if historico else 1):
            d = _api('/v1/market/token_kline', chain=x['chain'], address=x['address'], resolution=res, **{'from': inicio * 1000, 'to': hasta * 1000})
            lista = ((d or {}).get('list') if isinstance(d, dict) else d) or []
            for v in lista:
                velas_[int(v['time']) // 1000] = {'t': int(v['time']) // 1000, 'o': float(v['open']), 'h': float(v['high']), 'l': float(v['low']),
                                                  'c': float(v['close']), 'v': float(v.get('volume') or 0)}
            if len(lista) < 100 or not velas_:
                break
            hasta = min(velas_) - 1
            if hasta <= inicio:
                break
        if not velas_:
            raise ErrorOnchain(f'{simbolo}: sin velas {intervalo}')
        return [velas_[t] for t in sorted(velas_)]
    # las peticiones "en vivo" se cachean por intervalo; las históricas, por ventana exacta
    clave = ('v', simbolo.upper(), res, inicio // 60, fin // 60) if historico else ('v', simbolo.upper(), res)
    return _cacheado(clave, max(TTL[res], 600) if historico else TTL[res], pedir)


def cotizacion(simbolo):
    x = info(simbolo)
    v = velas(simbolo, '1m')
    p, ult = v[-1]['c'], v[-1]['t']
    ref = next((b['c'] for b in v if b['t'] >= ult - 3600), v[0]['c'])
    return {'simbolo': simbolo.upper(), 'nombre': f"{x['nombre']} (GMGN · {x['chain']})", 'precio': p, 'cierre_anterior': ref,
            'cambio': round(p - ref, 12), 'cambio_pct': round((p / ref - 1) * 100, 3) if ref else None,
            'max_dia': max(b['h'] for b in v), 'min_dia': min(b['l'] for b in v), 'volumen': sum(b['v'] for b in v[-60:]),
            'max_52s': None, 'min_52s': None, 'moneda': 'USD', 'tipo': 'ONCHAIN', 'bolsa': 'GMGN', 'zona': 'UTC',
            'ts': ult, 'abierto': time.time() - ult < 900,  # sin una sola operación en 15 min: muerto o sin liquidez
            'bid': p * (1 - SPREAD_PCT / 200), 'ask': p * (1 + SPREAD_PCT / 200), 'fuente': 'gmgn', 'address': x['address']}

# ---------------- universo con filtro anti-rug ----------------


def revisar(t):
    """Motivos para NO operar un token del ranking ([] = pasa). Lo que no viene en los datos cuenta como fallo."""
    f, mal = FILTRO, []
    if (t.get('liquidity') or 0) < f['liquidez_min']:
        mal.append('poca liquidez')
    if (t.get('volume') or 0) < f['volumen_min']:
        mal.append('poco volumen')
    if t.get('is_wash_trading'):
        mal.append('volumen falso')
    if t.get('rug_ratio') is None or t['rug_ratio'] > f['rug_max']:
        mal.append('riesgo de rug')
    if t.get('top_10_holder_rate') is None or t['top_10_holder_rate'] > f['top10_max']:
        mal.append('top 10 concentrado')
    if t.get('renounced_mint') != 1 or t.get('renounced_freeze_account') != 1:
        mal.append('mint o freeze sin renunciar')
    if time.time() - (t.get('open_timestamp') or t.get('creation_timestamp') or time.time()) < f['edad_min_h'] * 3600:
        mal.append('demasiado nuevo')
    return mal


def universo(n=N_TOKENS, cadena='SOL'):
    """Tokens en tendencia (volumen de 24 h) que pasan el filtro → (símbolos, detalle, rechazados {motivo: nº})."""
    d = _api('/v1/market/rank', chain=CADENAS[cadena], interval='24h', limit=100, order_by='volume', direction='desc',
             filters=['not_wash_trading'], min_liquidity=FILTRO['liquidez_min'], min_volume=FILTRO['volumen_min'])
    filas = (d or {}).get('rank') or []
    elegidos, detalle, rech = [], [], {}
    for t in filas:
        mal = revisar(t)
        for m in mal:
            rech[m] = rech.get(m, 0) + 1
        if mal or len(elegidos) >= n:
            continue
        s = registrar(cadena, t['address'], t.get('symbol'), t.get('name'))
        elegidos.append(s)
        detalle.append({'s': s, 'liq': round(t['liquidity']), 'vol': round(t['volume']), 'rug': round(t['rug_ratio'], 2),
                        'top10': round(t['top_10_holder_rate'], 2), 'smart': t.get('smart_degen_count'), 'kol': t.get('renowned_count'),
                        'cambio_1h': t.get('price_change_percent1h')})
    return elegidos, detalle, rech

# ---------------- smart money ----------------


def actualizar_smart(cadena='SOL'):
    """Acumula las operaciones de las wallets smart money (el feed solo trae los últimos ~200: llamarlo cada minuto)."""
    d = _api('/v1/user/smartmoney', chain=CADENAS[cadena], limit=200)
    lista = (d or {}).get('list') if isinstance(d, dict) else d
    ahora = time.time()
    tr = _smart['trades']
    for x in lista or []:
        try:
            tr[x['transaction_hash']] = (int(x['timestamp']), x['base_address'], float(x['amount_usd']) * (1 if x['side'] == 'buy' else -1))
        except (KeyError, TypeError, ValueError):
            pass
    for k in [k for k, v in tr.items() if ahora - v[0] > SMART_VENTANA]:
        del tr[k]
    _smart['ts'] = ahora
    return len(tr)


def smart_neto(simbolo):
    """Compras − ventas en $ del smart money en este token en la última hora (None si no hay datos recientes del feed)."""
    if time.time() - _smart['ts'] > 300:
        return None
    try:
        dir_ = info(simbolo)['address']
    except ErrorOnchain:
        return None
    return round(sum(v for t, a, v in _smart['trades'].values() if a == dir_ and time.time() - t <= SMART_VENTANA), 2)


def rasgos(simbolo):
    if not es(simbolo):
        return {}
    sm = smart_neto(simbolo)
    return {} if sm is None else {'smart_neto': sm}


def en_contra(ras, d):
    """Gen smart_money: no comprar si las wallets listas están vendiendo ese token (neto < 0) en la última hora."""
    return ras.get('smart_neto') is not None and ras['smart_neto'] * d < 0


def resumen(det=None, rech=None):
    partes = []
    if det:
        partes.append('memes: ' + ', '.join(x['s'].split('@')[0] for x in det))
    if rech:
        partes.append('descartados por ' + ', '.join(f'{k} ({v})' for k, v in sorted(rech.items(), key=lambda x: -x[1])[:4]))
    if time.time() - _smart['ts'] < 300:
        partes.append(f"smart money: {len(_smart['trades'])} operaciones en 1 h")
    return 'On-chain (GMGN): ' + (' · '.join(partes) if partes else 'sin datos') + '.'
