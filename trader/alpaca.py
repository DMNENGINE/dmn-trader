"""Espejo en Alpaca (paper trading, dinero ficticio con ejecución real): copia las operaciones del mejor agente del enjambre
con un importe pequeño para medir lo que el simulador no ve (deslizamiento real, órdenes que no se llenan, horarios).

Se activa solo si existe datos/alpaca.json con las claves de una cuenta PAPER: {"key": "...", "secret": "...", "nominal": 10}.
Las claves las crea el usuario en alpaca.markets (Paper Trading → API Keys); nunca van al repositorio. Solo admite la URL
paper: para dinero real hará falta un cambio de código consciente, no un ajuste."""
import json, os, threading, time, urllib.error, urllib.parse, urllib.request
from . import db

CONF = os.path.join(os.path.dirname(db.RUTA), 'alpaca.json')
PAPER = 'https://paper-api.alpaca.markets'
LIDER_CADA = 1800
ESQUEMA = """CREATE TABLE IF NOT EXISTS alp_espejo (id INTEGER PRIMARY KEY, trade_id INTEGER UNIQUE, agente TEXT, simbolo TEXT, lado TEXT,
  precio_sim REAL, precio_real REAL, qty REAL, abierto INTEGER, cerrado INTEGER, salida_sim REAL, salida_real REAL, estado TEXT,
  orden_id TEXT, orden_cierre TEXT, error TEXT)"""
_lider, _lock = {'ts': 0, 'id': None}, threading.Lock()


def config():
    try:
        c = json.load(open(CONF, encoding='utf-8'))
    except Exception:
        return None
    return c if c.get('key') and c.get('secret') else None


def configurado():
    return config() is not None


def _api(metodo, ruta, cuerpo=None):
    c = config()
    req = urllib.request.Request(PAPER + ruta, method=metodo, data=json.dumps(cuerpo).encode() if cuerpo is not None else None,
                                 headers={'APCA-API-KEY-ID': c['key'], 'APCA-API-SECRET-KEY': c['secret'], 'Content-Type': 'application/json'})
    try:
        r = urllib.request.urlopen(req, timeout=15).read()
        return json.loads(r) if r else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Alpaca {e.code}: {e.read()[:200].decode(errors="replace")}')


def _simbolo(s):
    return s.replace('-USD', '/USD') if s.endswith('-USD') else s


def lider():
    """El mejor agente del enjambre (≥ 10 operaciones); se mantiene 30 min para no saltar de uno a otro."""
    from . import agente as A
    if time.time() - _lider['ts'] < LIDER_CADA and _lider['id']:
        a = db.uno('SELECT * FROM ag_agentes WHERE id=? AND activo=1', (_lider['id'],))
        if a:
            return a
    cand = [(f, a) for f, n, a in ((*A.aptitud(a), a) for a in A.agentes() if not A._llm(a)) if f is not None and n >= 10]
    if not cand:
        return None
    f, a = max(cand, key=lambda x: x[0])
    _lider.update(ts=time.time(), id=a['id'])
    return a


def espejo():
    """Un paso: abre en Alpaca lo nuevo del líder, cierra lo que el simulador ya cerró y apunta los precios reales de ejecución."""
    if not _lock.acquire(blocking=False):
        return
    try:
        from . import motor
        db.ex(ESQUEMA)
        nominal = float(config().get('nominal', 10))
        for x in db.q("SELECT * FROM alp_espejo WHERE estado IN ('enviada','cerrando')"):  # precios reales de ejecución
            try:
                o = _api('GET', f"/v2/orders/{x['orden_cierre'] if x['estado'] == 'cerrando' else x['orden_id']}")
            except Exception:
                continue
            if o.get('status') == 'filled':
                if x['estado'] == 'enviada':
                    db.ex("UPDATE alp_espejo SET precio_real=?, qty=?, estado='abierta' WHERE id=?", (float(o['filled_avg_price']), float(o['filled_qty']), x['id']))
                else:
                    db.ex("UPDATE alp_espejo SET salida_real=?, estado='cerrada' WHERE id=?", (float(o['filled_avg_price']), x['id']))
            elif o.get('status') in ('canceled', 'expired', 'rejected'):
                db.ex("UPDATE alp_espejo SET estado=?, error=? WHERE id=?", ('no llenada', o.get('status'), x['id']))
        for x in db.q("SELECT e.*, t.estado te, t.salida FROM alp_espejo e JOIN trades t ON t.id=e.trade_id WHERE e.estado='abierta'"):
            if x['te'] != 'cerrado':
                continue
            try:
                o = _api('DELETE', '/v2/positions/' + urllib.parse.quote(_simbolo(x['simbolo']).replace('/', ''), safe=''))
                db.ex("UPDATE alp_espejo SET salida_sim=?, cerrado=?, orden_cierre=?, estado='cerrando' WHERE id=?", (x['salida'], int(time.time()), o.get('id'), x['id']))
            except Exception as err:
                db.ex('UPDATE alp_espejo SET error=? WHERE id=?', (str(err)[:200], x['id']))
        a = lider()
        if not a:
            return
        hechos = {r['trade_id'] for r in db.q('SELECT trade_id FROM alp_espejo')}
        for p in motor.posiciones(a['cuenta_id'], False):
            t = db.uno('SELECT * FROM trades WHERE id=?', (p['trade_id'],))
            if not t or t['id'] in hechos or time.time() - t['abierto'] > 300:  # solo entradas recientes: si no, el precio ya no es comparable
                continue
            lado = 'buy' if p['cantidad'] > 0 else 'sell'
            fila = (t['id'], a['nombre'], p['simbolo'], lado, t['entrada'], int(time.time()))
            if lado == 'sell' and p['simbolo'].endswith('-USD'):
                db.ex("INSERT INTO alp_espejo(trade_id,agente,simbolo,lado,precio_sim,abierto,estado) VALUES(?,?,?,?,?,?,'omitida')", fila)
                continue  # Alpaca no deja abrir cortos en cripto
            cuerpo = {'symbol': _simbolo(p['simbolo']), 'side': lado, 'type': 'market', 'time_in_force': 'gtc' if p['simbolo'].endswith('-USD') else 'day'}
            if lado == 'sell':  # los cortos de acciones no admiten fracciones
                n = int(nominal // t['entrada'])
                if n < 1:
                    continue
                cuerpo['qty'] = str(n)
            else:
                cuerpo['notional'] = str(round(nominal, 2))
            try:
                o = _api('POST', '/v2/orders', cuerpo)
                db.ex("INSERT INTO alp_espejo(trade_id,agente,simbolo,lado,precio_sim,abierto,estado,orden_id) VALUES(?,?,?,?,?,?,'enviada',?)", (*fila, o.get('id')))
            except Exception as err:
                db.ex("INSERT INTO alp_espejo(trade_id,agente,simbolo,lado,precio_sim,abierto,estado,error) VALUES(?,?,?,?,?,?,'error',?)", (*fila, str(err)[:200]))
    finally:
        _lock.release()


def resumen():
    try:
        db.ex(ESQUEMA)
        f = db.q("SELECT * FROM alp_espejo WHERE abierto > ?", (int(time.time()) - 7 * 86400,))
    except Exception:
        return 'Alpaca (paper): sin datos.'
    if not f:
        return 'Alpaca (paper): conectado, esperando la primera operación del líder.'
    d = lambda real, sim, lado: (real / sim - 1) * 100 * (1 if lado == 'buy' else -1)  # + = peor que el simulador
    ent = [d(x['precio_real'], x['precio_sim'], x['lado']) for x in f if x['precio_real'] and x['precio_sim']]
    sal = [-d(x['salida_real'], x['salida_sim'], x['lado']) for x in f if x['salida_real'] and x['salida_sim']]
    err = sum(1 for x in f if x['estado'] in ('error', 'no llenada'))
    return (f"Alpaca (paper, 7 días): {len(f)} operaciones copiadas del líder" +
            (f" · deslizamiento real al entrar {sum(ent) / len(ent):+.3f} %" if ent else '') +
            (f", al salir {sum(sal) / len(sal):+.3f} %" if sal else '') + (f" · {err} fallidas" if err else '') +
            ' (positivo = peor que el simulador).')
