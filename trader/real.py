"""Cuenta REAL en GMGN (solo lectura): qué tiene la wallet, cómo van las posiciones y qué hace el bot de GMGN (las órdenes
condicionales de take-profit / stop-loss que vigila él). Lo consulta el equipo que tiene gmgn-cli con la key (el PC) cada
pocos segundos, usando la CLI oficial (la clave privada nunca sale de ~/.config/gmgn ni pasa por aquí), y manda la foto a la
central (la Pi) para verla en el panel desde cualquier sitio. Aquí NO se compra ni se vende nada."""
import json, os, shutil, subprocess, threading, time, urllib.request
from . import datos, db, onchain

CADA, CADA_LENTO = 10, 30
CLAVE = 'real_estado'
SOL = 'So11111111111111111111111111111111111111112'
_ultimo = {'lento': 0, 'info': None, 'actividad': [], 'holdings': []}
_hilo = {'t': None}


def disponible():
    return bool(shutil.which('gmgn-cli')) and onchain.disponible()


def _cli(*args, timeout=25):
    exe = shutil.which('gmgn-cli')
    r = subprocess.run([exe, *args, '--raw'], capture_output=True, text=True, encoding='utf-8', timeout=timeout,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1][:200] if (r.stderr or r.stdout).strip() else f'gmgn-cli {args[0]} falló')
    return json.loads(r.stdout)


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _wallet():
    w = next((x for x in (_ultimo['info'] or {}).get('wallets', []) if x['chain'] == 'sol'), None)
    return w and w['address']


def _ordenes(chain='sol'):
    out = []
    for tag in ('STMix', 'LimitOrder'):
        try:
            out += (_cli('order', 'strategy', 'list', '--chain', chain, '--group-tag', tag) or {}).get('list') or []
        except Exception:
            pass
    return out


def foto():
    """Estado actual de la cuenta real. Las posiciones salen del saldo real de cada token (las holdings de GMGN a veces vienen
    vacías): tokens con estrategia abierta, comprados en la actividad reciente o listados en holdings."""
    ahora = time.time()
    if ahora - _ultimo['lento'] > CADA_LENTO or not _ultimo['info']:
        _ultimo['info'] = onchain._api('/v1/user/info')
        w = _wallet()
        if w:
            try:
                _ultimo['actividad'] = (onchain._api('/v1/user/wallet_activity', chain='sol', wallet_address=w) or {}).get('activities') or []
            except Exception:
                pass
            try:
                _ultimo['holdings'] = (_cli('portfolio', 'holdings', '--chain', 'sol', '--wallet', w) or {}).get('list') or []
            except Exception:
                pass
        _ultimo['lento'] = ahora
    w = _wallet()
    if not w:
        return {'ts': int(ahora), 'error': 'no hay wallet de Solana vinculada a la API key'}
    ords = _ordenes()
    hold = {(h.get('token') or {}).get('token_address'): h for h in _ultimo.get('holdings') or []}
    sol = next((_f(b['balance']) for x in _ultimo['info']['wallets'] if x['chain'] == 'sol' for b in x['balances'] if b['symbol'] == 'SOL'), 0.0)
    try:
        sol_usd = datos.cotizacion('SOL-USD')['precio']
    except Exception:
        sol_usd = None
    compras = {}
    for a in _ultimo['actividad']:
        tk = (a.get('token') or {}).get('address')
        if tk and tk != SOL:
            c = compras.setdefault(tk, {'cant': 0.0, 'coste': 0.0, 'fees': 0.0, 'desde': a.get('timestamp')})
            sg = 1 if a.get('event_type') == 'buy' else -1
            c['cant'] += sg * _f(a.get('token_amount'))
            c['coste'] += sg * _f(a.get('cost_usd'))
            c['fees'] += _f(a.get('gas_usd')) + _f(a.get('dex_usd'))
            c['desde'] = min(c['desde'] or 9e12, a.get('timestamp') or 9e12)
    tokens = list(dict.fromkeys([o.get('base_token') for o in ords if o.get('status') in ('open', 'prepare')] + [k for k, c in compras.items() if c['cant'] > 0] + list(hold)))
    pos = []
    for tk in [t for t in tokens if t]:
        try:
            bal = _f(((onchain._api('/v1/user/wallet_token_balance', chain='sol', wallet_address=w, token_address=tk) or {}).get('balances') or [{}])[0].get('balance'))
        except Exception:
            bal = _f((hold.get(tk) or {}).get('balance'))
        if bal <= 0:
            continue
        try:
            t = onchain._api('/v1/token/info', chain='sol', address=tk) or {}
        except Exception:
            t = {}
        pr = t.get('price')
        precio = _f(pr.get('price') if isinstance(pr, dict) else pr) or _f(((hold.get(tk) or {}).get('token') or {}).get('price'))
        h, c = hold.get(tk) or {}, compras.get(tk) or {}
        coste = _f(h.get('accu_cost')) or c.get('coste') or 0.0
        cant_c = _f(h.get('accu_amount')) or c.get('cant') or 0.0
        entrada = coste / cant_c if cant_c else None
        valor = bal * precio
        st = next((o for o in ords if o.get('base_token') == tk and o.get('status') in ('open', 'prepare')), None)
        conds = [{'tipo': x['order_type'], 'estado': x.get('status'), 'precio': _f(x.get('check_price')), 'pct': _f(x.get('price_scale')),
                  'venta': _f(x.get('sell_ratio'))} for x in (st or {}).get('condition_orders') or []]
        tp = next((x['precio'] for x in conds if x['tipo'].startswith('profit') and x['estado'] == 'check'), None)
        sl = next((x['precio'] for x in conds if x['tipo'].startswith('loss') and x['estado'] == 'check'), None)
        pos.append({'token': tk, 'simbolo': t.get('symbol') or (h.get('token') or {}).get('symbol'), 'nombre': t.get('name'), 'logo': t.get('logo'),
                    'cantidad': bal, 'precio': precio, 'valor': valor, 'entrada': entrada, 'coste': coste,
                    'comisiones': _f(h.get('accu_fee')) or c.get('fees') or 0.0, 'pnl': valor - coste if coste else None,
                    'pnl_pct': (valor / coste - 1) * 100 if coste else None, 'realizado': _f(h.get('realized_profit')),
                    'desde': h.get('start_holding_at') or c.get('desde'), 'liquidez': _f(t.get('liquidity')),
                    'tp': tp, 'sl': sl, 'condiciones': conds,
                    'dist_tp': (tp / precio - 1) * 100 if tp and precio else None, 'dist_sl': (sl / precio - 1) * 100 if sl and precio else None,
                    'caduca': int(st['expire_time'] / 1000) if st and st.get('expire_time') else None})
    act = actividad(_ultimo['actividad'][:40])
    return {'ts': int(ahora), 'wallet': w, 'sol': sol, 'sol_usd': sol_usd, 'posiciones': pos, 'actividad': act,
            'estrategias': [{'token': o.get('base_token'), 'simbolo': _simbolo(o.get('base_token')), 'estado': o.get('status'),
                             'creada': int(_f(o.get('create_time')) / 1000), 'caduca': int(_f(o.get('expire_time')) / 1000) or None,
                             'invertir_sol': _f(o.get('quote_investment')) / 1e9 or None,
                             'cerrada': int(_f(o.get('close_time')) / 1000) or None, 'condiciones': [
                                 {'tipo': c['order_type'], 'estado': c.get('status'), 'precio': _f(c.get('check_price')), 'pct': _f(c.get('price_scale'))}
                                 for c in o.get('condition_orders') or []]} for o in ords][:20]}


_SIMB = {}


def _simbolo(tk):
    """Ticker de un token (con caché: no cambia)."""
    if tk and tk not in _SIMB:
        try:
            _SIMB[tk] = (onchain._api('/v1/token/info', chain='sol', address=tk) or {}).get('symbol')
        except Exception:
            return None
    return _SIMB.get(tk)


def actividad(lista):
    """Una fila por operación. Si GMGN enruta por tokens intermedios (p. ej. DACITY → TTWO → USDC → SOL), el indexador da
    una fila por tramo en la misma tx: se quitan los tramos cuyo token es la moneda de cotización de otro tramo y se
    guarda la ruta."""
    por_tx = {}
    for a in lista:
        por_tx.setdefault(a.get('tx_hash'), []).append(a)
    out = []
    for tx, tramos in por_tx.items():
        intermedios = {a.get('quote_address') or (a.get('quote_token') or {}).get('token_address') for a in tramos}
        for a in tramos:
            tk = a.get('token') or {}
            if len(tramos) > 1 and tk.get('address') in intermedios:
                continue
            ruta = [(x.get('token') or {}).get('symbol') for x in tramos if (x.get('token') or {}).get('address') in intermedios]
            out.append({'tx': tx, 'ts': a.get('timestamp'), 'tipo': a.get('event_type'), 'simbolo': tk.get('symbol'), 'token': tk.get('address'),
                        'cantidad': _f(a.get('token_amount')), 'usd': _f(a.get('cost_usd')), 'precio': _f(a.get('price_usd')),
                        'gas_usd': _f(a.get('gas_usd')), 'dex_usd': _f(a.get('dex_usd')), 'ruta': ruta})
    return out


def _eventos(viejo, nuevo):
    """Lo que ha cambiado entre dos fotos, contado en lenguaje normal."""
    ev = []
    vistos = {a['tx'] for a in (viejo or {}).get('actividad') or []}
    for a in reversed(nuevo.get('actividad') or []):
        if viejo and a['tx'] not in vistos:
            verbo = {'buy': '🟢 Compra', 'sell': '🔴 Venta'}.get(a['tipo'], a['tipo'])
            via = f" · vía {' → '.join(a['ruta'])}" if a.get('ruta') else ''
            ev.append(f"{verbo} de {a['cantidad']:,.2f} {a['simbolo']} por {a['usd']:.2f} $ (a {a['precio']:.6g} $){via}")
    antes = {(e['token'], c['tipo']): c['estado'] for e in (viejo or {}).get('estrategias') or [] for c in e['condiciones']}
    for e in nuevo.get('estrategias') or []:
        for c in e['condiciones']:
            k = (e['token'], c['tipo'])
            if viejo and k in antes and antes[k] != c['estado']:
                nom = 'take-profit' if c['tipo'].startswith('profit') else 'stop-loss'
                ev.append(f"🎯 El bot de GMGN: {nom} ({c['pct']:g} %) pasa de «{antes[k]}» a «{c['estado']}»")
    pv = {p['token'] for p in (viejo or {}).get('posiciones') or []}
    for p in nuevo.get('posiciones') or []:
        if viejo and p['token'] not in pv:
            ev.append(f"📥 Nueva posición: {p['simbolo']} ({p['valor']:.2f} $)")
    for p in (viejo or {}).get('posiciones') or []:
        if p['token'] not in {x['token'] for x in nuevo.get('posiciones') or []}:
            ev.append(f"📤 Posición cerrada: {p['simbolo']}")
    return ev


def guardar(snap):
    viejo = db.ajuste(CLAVE) or {}
    evs = viejo.get('eventos') or []
    if not snap.get('eventos'):  # foto nueva de este equipo: calcula qué ha cambiado
        evs = (evs + [[snap['ts'], t] for t in _eventos(viejo if viejo.get('ts') else None, snap)])[-80:]
        snap['eventos'] = evs
    db.poner_ajuste(CLAVE, snap)
    return snap


def recibir(snap):
    """En la central: guarda la foto que manda el nodo con la key."""
    if isinstance(snap, dict) and snap.get('ts'):
        snap['recibido'] = int(time.time())
        db.poner_ajuste(CLAVE, snap)
    return {'ok': True}


def estado():
    s = db.ajuste(CLAVE) or {}
    if s.get('ts'):
        s['edad'] = int(time.time() - s['ts'])
    s['vigilando_aqui'] = bool(_hilo['t'] and _hilo['t'].is_alive())
    return s


def _enviar(snap):
    from . import agente
    if not agente.CENTRAL:
        return
    req = urllib.request.Request(agente.CENTRAL + '/api/real/nodo', data=json.dumps(snap, default=str).encode(), method='POST',
                                 headers={'content-type': 'application/json', **({'x-token': agente.CENTRAL_TOKEN} if agente.CENTRAL_TOKEN else {})})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def _bucle():
    while True:
        t0 = time.time()
        try:
            snap = guardar(foto())
            _enviar(snap)
        except Exception as e:
            s = db.ajuste(CLAVE) or {}
            s['error'] = str(e)[:200]
            s['error_ts'] = int(time.time())
            db.poner_ajuste(CLAVE, s)
        time.sleep(max(2, CADA - (time.time() - t0)))


def arrancar():
    if disponible() and not (_hilo['t'] and _hilo['t'].is_alive()):
        _hilo['t'] = threading.Thread(target=_bucle, daemon=True)
        _hilo['t'].start()
    return bool(_hilo['t'])
