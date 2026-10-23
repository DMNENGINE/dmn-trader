"""PILOTO REAL: bot que opera SOLO con dinero real en GMGN dentro de los límites que le da el usuario (tarea + montos).
Cada pocos minutos escanea los memes de Solana que pasan el filtro anti-rug, puntúa los retrocesos dentro de tendencia
(+ smart money) y, si uno llega al umbral, deja en GMGN una orden smart_trade: compra límite en retroceso + TP + SL que
vigila el propio GMGN. Solo una orden suya a la vez; la anula si no entra a tiempo; lleva la cuenta de cada operación.
Frenos duros: pérdida máxima del total (se apaga), pausa tras 2 stops seguidos, tope de operaciones al día, reserva de SOL
para comisiones, enfriamiento por token tras un stop. Corre donde está gmgn-cli con la key (el PC); la configuración la
pone el panel de la central y llega aquí con la respuesta de la sincronía de real.py."""
import threading, time
from . import db, llm, onchain, real

CFG, EST = 'piloto_cfg', 'piloto_estado'
CADA = 300            # escaneo
CADA_ORDEN = 30       # seguimiento de la orden abierta
CADA_IA = 300         # revisión de la posición por el modelo
CADA_VELAS = 300
DEFECTO = {'activo': False, 'tarea': 'memes_sol', 'por_op_sol': 0.01, 'perdida_max_sol': 0.008, 'reserva_sol': 0.005,
           'max_ops_dia': 6, 'umbral': 6, 'atr_max': 8.0, 'espera_min': 45, 'pausa_stops_h': 3, 'enfriar_token_h': 6,
           'gestion': True, 'ia': True, 'smart_salida_usd': 300.0, 'ia_conf_min': 0.7, 'fuente': 'fabrica'}
# fuente 'fabrica': solo opera las estrategias que la fábrica ha PROMOVIDO (ganan en vivo); 'retroceso': el escáner antiguo
_hilo = {'t': None}
_ult = {'scan': 0, 'smart': 0}


def cfg():
    return {**DEFECTO, **(db.ajuste(CFG) or {})}


def poner_cfg(nuevo):
    """Desde el panel / APOLO. Al activar se fija el saldo de partida para medir la pérdida máxima."""
    c = cfg()
    for k, v in (nuevo or {}).items():
        if k in DEFECTO:
            c[k] = type(DEFECTO[k])(v) if not isinstance(DEFECTO[k], bool) else bool(v)
    c['por_op_sol'] = max(0.005, min(c['por_op_sol'], 0.1))  # tope duro: nunca más de 0,1 SOL por operación desde aquí
    c['cambiado'] = int(time.time())
    db.poner_ajuste(CFG, c)
    return c


def estado():
    from . import laboratorio_ia
    try:
        lab = laboratorio_ia.informe() if _hilo['t'] else None
    except Exception:
        lab = None
    from . import fabrica
    try:
        fab = fabrica.resumen() if _hilo['t'] else None
    except Exception:
        fab = None
    return {**(db.ajuste(EST) or {}), 'cfg': cfg(), 'lab': lab, 'fabrica': fab}


def _est():
    return db.ajuste(EST) or {'ops': [], 'log': []}


def _log(e, txt):
    e['log'] = (e.get('log') or [])[-59:] + [[int(time.time()), txt]]


def _saldo():
    if not real._wallet():
        real._ultimo['info'] = onchain._api('/v1/user/info')
    w = real._wallet()
    d = onchain._api('/v1/user/wallet_token_balance', chain='sol', wallet_address=w, token_address='So11111111111111111111111111111111111111111')
    return float(d['balances'][0]['balance']), w


# ---------------- elegir moneda ----------------

def _ema(xs, n):
    k, e = 2 / (n + 1), xs[0]
    for x in xs[1:]:
        e = x * k + e * (1 - k)
    return e


def _rsi(xs, n=14):
    g = [max(0, b - a) for a, b in zip(xs[-n - 1:-1], xs[-n:])]
    p = [max(0, a - b) for a, b in zip(xs[-n - 1:-1], xs[-n:])]
    return 100 - 100 / (1 + (sum(g) / n) / (sum(p) / n or 1e-12))


def candidatos(evitar=()):
    """Retrocesos sanos dentro de tendencia, puntuados 0-9 (misma lógica que el escáner probado a mano)."""
    filas = {}
    for intervalo, vol in (('1h', 100_000), ('24h', 500_000)):
        d = onchain._api('/v1/market/rank', chain='sol', interval=intervalo, limit=100, order_by='volume', direction='desc',
                         filters=['not_wash_trading'], min_liquidity=onchain.FILTRO['liquidez_min'], min_volume=vol)
        for t in (d or {}).get('rank') or []:
            filas.setdefault(t['address'], t)
    try:
        onchain.actualizar_smart()
    except Exception:
        pass
    viejo, onchain.FILTRO['volumen_min'] = onchain.FILTRO['volumen_min'], 100_000
    try:
        sanos = [t for t in filas.values() if not onchain.revisar(t) and t['address'] not in evitar]
    finally:
        onchain.FILTRO['volumen_min'] = viejo
    res = []
    for t in sanos:
        s = onchain.registrar('SOL', t['address'], t.get('symbol'), t.get('name'))
        try:
            v15, v1h = onchain.velas(s, '15m'), onchain.velas(s, '1h')
        except Exception:
            continue
        if len(v15) < 40 or len(v1h) < 24:
            continue
        c15, c1h = [x['c'] for x in v15], [x['c'] for x in v1h]
        p = c15[-1]
        ret = (p / max(x['h'] for x in v15[-16:]) - 1) * 100
        t24 = (p / c1h[-24] - 1) * 100
        sobre = p > _ema(c1h[-50:], 20)
        r15 = _rsi(c15)
        vol = sum(x['v'] for x in v15[-4:]) / (sum(x['v'] for x in v15[-96:]) / 24 or 1)
        sm = onchain.smart_neto(s) or 0
        atr = sum((x['h'] - x['l']) / x['c'] for x in v15[-16:]) / 16 * 100
        pts = ((2 if sobre else 0) + (2 if -25 <= ret <= -6 else 0) + (1 if 35 <= r15 <= 52 else 0) + (1 if vol >= 0.7 else 0)
               + (1 if t24 > 0 else 0) + (1 if sm > 0 else -1 if sm < 0 else 0))
        res.append({'simbolo': s.split('@')[0], 'token': t['address'], 'pts': pts, 'precio': p, 'atr': round(atr, 2),
                    'ret': round(ret, 1), 't24': round(t24, 1), 'rsi': round(r15), 'sobre_ema': sobre, 'vol': round(vol, 2), 'smart': sm})
    return sorted(res, key=lambda x: (-x['pts'], x['atr']))


# ---------------- órdenes ----------------

def _crear(w, c, precio, compra=None, tp=None, sl=None):
    sl = sl or max(8.0, min(2.5 * c['atr'], 18.0))
    tp = tp or 2 * sl
    compra = compra or precio * (1 - c['atr'] / 200)  # medio ATR por debajo: no perseguir
    lam = str(int(cfg()['por_op_sol'] * 1e9))
    bp = '{"slippage":10,"priority_fee":"0.00001","tip_fee":"0.00001"}'
    co = ('[{"order_type":"buy_low","side":"buy","check_price":"%.10g"},'
          '{"order_type":"profit_stop","side":"sell","price_scale":"%g","sell_ratio":"100"},'
          '{"order_type":"loss_stop","side":"sell","price_scale":"%g","sell_ratio":"100"}]') % (compra, round(tp, 1), round(sl, 1))
    r = real._cli('order', 'strategy', 'create', '--chain', 'sol', '--from', w, '--base-token', c['token'], '--quote-token', real.SOL,
                  '--order-type', 'smart_trade', '--sub-order-type', 'mix_trade', '--open-price', '%.10g' % precio,
                  '--amount-in', lam, '--quote-investment', lam, '--slippage', '10', '--priority-fee', '0.00001',
                  '--tip-fee', '0.00001', '--buy-param', bp, '--sell-param', bp, '--condition-orders', co)
    return r['order_id'], compra, tp, sl


def _buscar(oid):
    for tipo in ('open', 'history'):
        try:
            for o in (real._cli('order', 'strategy', 'list', '--chain', 'sol', '--group-tag', 'STMix', '--type', tipo, '--limit', '20') or {}).get('list') or []:
                if o.get('order_id') == oid:
                    return tipo, o
        except Exception:
            pass
    return None, None


def _cancelar(w, oid):
    real._cli('order', 'strategy', 'cancel', '--chain', 'sol', '--from', w, '--order-id', oid, '--order-type', 'smart_trade')


def _resultado(o):
    st = {x['order_type']: x.get('status') for x in o.get('condition_orders') or []}
    if st.get('profit_stop') == 'success':
        return 'tp'
    if st.get('loss_stop') == 'success':
        return 'sl'
    if st.get('buy_low') == 'failed':
        return 'fallo_compra'
    if st.get('buy_low') != 'success':
        return 'no_entro'
    return 'cerrada'


# ---------------- gestión activa (1: stop que sube · 2: salida anticipada · 3: revisión con IA) ----------------

def _precio(tok):
    return float(onchain._api('/v1/token/info', chain='sol', address=tok)['price']['price'])


def _tokens(w, tok):
    try:
        d = onchain._api('/v1/user/wallet_token_balance', chain='sol', wallet_address=w, token_address=tok)
        return float(d['balances'][0]['balance'])
    except Exception:
        return 0.0


def _vender(o):
    """Venta a mercado del 100 % (tras anular la orden de GMGN para que su TP/SL no choque)."""
    real._cli('swap', '--chain', 'sol', '--from', o['wallet'], '--input-token', o['token'], '--output-token', real.SOL,
              '--percent', '100', '--slippage', '15', '--priority-fee', '0.00001', '--tip-fee', '0.00001', timeout=60)


def _salir(e, o, motivo):
    if not o.get('salida'):
        try:
            _cancelar(o['wallet'], o['id'])
        except Exception as ex:
            _log(e, f"⚠️ Quería salir de {o['simbolo']} ({motivo}) pero no pude anular la orden de GMGN: {str(ex)[:80]}")
            return
        o['salida'] = motivo
        _log(e, f"🚪 Saliendo de {o['simbolo']}: {motivo}")
    try:
        if _tokens(o['wallet'], o['token']) > 0:
            _vender(o)
        o['vendido'] = int(time.time())
    except Exception as ex:
        o['reintentos'] = o.get('reintentos', 0) + 1
        _log(e, f"⚠️ Venta de {o['simbolo']} falló (reintento {o['reintentos']}): {str(ex)[:80]}")


def _contexto(tok, simbolo):
    """Resumen compacto de la moneda para el modelo: precio, flujo, holders y velas recientes."""
    t = onchain._api('/v1/token/info', chain='sol', address=tok)
    p, st = t['price'], t.get('stat') or {}
    s = onchain.registrar('SOL', tok, simbolo, t.get('name'))
    v15, v1h = onchain.velas(s, '15m')[-16:], onchain.velas(s, '1h')[-24:]
    r = lambda x: float('%.4g' % x)
    pr = float(p['price'])
    return {'simbolo': simbolo, 'precio': r(pr),
            'cambio_%': {k: round((pr / float(p[f'price_{k}']) - 1) * 100, 1) for k in ('5m', '1h', '6h', '24h') if float(p.get(f'price_{k}') or 0)},
            'compras_vs_ventas_$': {k: [round(float(p.get(f'buy_volume_{k}') or 0)), round(float(p.get(f'sell_volume_{k}') or 0))] for k in ('5m', '1h', '6h')},
            'liquidez_$': round(float(t.get('liquidity') or 0)), 'holders': t.get('holder_count'),
            'top10_%': round(float(st.get('top_10_holder_rate') or 0) * 100, 1), 'bundlers_%': round(float(st.get('top_bundler_trader_percentage') or 0) * 100, 1),
            'smart_money_neto_1h_$': onchain.smart_neto(s),
            'velas_15m_ohlcv': [[r(x['o']), r(x['h']), r(x['l']), r(x['c']), round(x['v'])] for x in v15],
            'cierres_1h': [r(x['c']) for x in v1h]}


REGLAS_IA = ('Eres un trader cuantitativo de memecoins de Solana, frío y escéptico. Costes reales ≈6 % ida y vuelta: solo vale la pena '
             'si el movimiento esperado lo supera con claridad. Peligros: ventas > compras sostenidas, smart money vendiendo, mechas largas '
             'arriba, volumen cayendo tras un pump, holders concentrados. Responde SOLO JSON.')


def _ia_entrada(c, b):
    if not c['ia'] or not llm.disponible():
        return True, 'sin IA'
    sl = max(8, min(2.5 * b['atr'], 18))
    if b.get('estrategia'):
        v = b['vivo']
        prop = (f"La estrategia «{b['estrategia']}» ({b['regla']}) propone COMPRAR YA; en simulación en vivo lleva {v['n']} operaciones, "
                f"{v['medio']:+.1f} % neto de media, {v['gana_pct']} % ganadoras. TP +{b['tp']:.0f} %, SL −{b['sl']:.0f} %.")
    else:
        prop = (f"El escáner propone COMPRAR en retroceso ({b['pts']}/9 puntos, ATR15 {b['atr']} %, compra límite medio ATR "
                f"por debajo, TP +{2 * sl:.0f} %, SL −{sl:.0f} %).")
    try:
        r = llm.preguntar(f"{REGLAS_IA}\n{prop}\nDatos: {_contexto(b['token'], b['simbolo'])}\n"
                          'Contesta {"decision":"entrar"|"vetar","confianza":0-1,"motivo":"una frase en español"}', timeout=90)
    except Exception as ex:
        return True, f'IA no contestó ({str(ex)[:40]})'
    veto = str(r.get('decision', '')).lower().startswith('vet') and real._f(r.get('confianza')) >= 0.6
    return (not veto), str(r.get('motivo') or '')[:160]


def _gestionar(e, c, o):
    """Posición abierta: máximo, stop que sube, salidas anticipadas y revisión del modelo."""
    ahora = time.time()
    if o.get('salida'):
        if not o.get('vendido') and o.get('reintentos', 0) < 20:
            _salir(e, o, o['salida'])
        return
    pr = _precio(o['token'])
    ent = o.get('precio_entrada') or o['compra']
    o['precio'], o['max'] = pr, max(o.get('max') or ent, pr)
    o['pnl_pct'] = round((pr / ent - 1) * 100, 2)
    if o.get('hold') and ahora - o['entro'] > o['hold'] * 300:
        return _salir(e, o, f"tiempo máximo de la estrategia ({o['hold'] * 5} min, {o['pnl_pct']:+.1f} %)")
    if not c['gestion']:
        return
    # 1) stop que sube: a mitad de camino del TP protege la entrada (+2 %); a 3/4 persigue el máximo a 1,5 ATR
    stop = None
    if o['max'] >= ent * (1 + o['tp'] / 200):
        stop = ent * 1.02
    if o['max'] >= ent * (1 + o['tp'] * 0.75 / 100):
        stop = max(stop or 0, o['max'] * (1 - 1.5 * o['atr'] / 100))
    if stop and stop != o.get('stop_dyn'):
        o['stop_dyn'] = stop
        _log(e, f"🔒 {o['simbolo']} llegó a {(o['max'] / ent - 1) * 100:+.1f} %: stop subido a {stop:.6g} $ ({(stop / ent - 1) * 100:+.1f} %)")
    if stop and pr <= stop:
        return _salir(e, o, f"stop dinámico ({(pr / ent - 1) * 100:+.1f} %)")
    # 2) salida anticipada: smart money vendiendo fuerte con la posición en rojo, o sin volumen ni avance tras 1 h
    s = onchain.registrar('SOL', o['token'], o['simbolo'])
    sm = onchain.smart_neto(s)
    o['smart'] = sm
    if sm is not None and sm <= -c['smart_salida_usd'] and pr < ent:
        return _salir(e, o, f'smart money vendiendo ({sm:.0f} $ en 1 h)')
    if ahora - o.get('velas_ts', 0) >= CADA_VELAS:
        o['velas_ts'] = ahora
        try:
            v = onchain.velas(s, '15m')
            o['vol_rel'] = round(sum(x['v'] for x in v[-2:]) / 2 / (sum(x['v'] for x in v[-26:-2]) / 24 or 1), 2)
        except Exception:
            pass
    if ahora - o['entro'] > 3600 and o['pnl_pct'] < 2 and (o.get('vol_rel') or 1) < 0.3:
        return _salir(e, o, f"sin volumen ni avance tras 1 h (vol {o.get('vol_rel')}×, {o['pnl_pct']:+.1f} %)")
    # 3) revisión con el modelo cada 5 min
    if c['ia'] and ahora - o.get('ia_ts', 0) >= CADA_IA and llm.disponible():
        o['ia_ts'] = ahora
        try:
            r = llm.preguntar(f"{REGLAS_IA}\nPosición LARGA: entrada {ent:.6g} $, ahora {o['pnl_pct']:+.1f} %, máximo {(o['max'] / ent - 1) * 100:+.1f} %, "
                              f"TP +{o['tp']:.0f} %, SL −{o['sl']:.0f} %, abierta hace {int((ahora - o['entro']) / 60)} min.\n"
                              f"Datos: {_contexto(o['token'], o['simbolo'])}\n"
                              '¿Mantener hasta TP/SL o salir ya? Contesta {"accion":"mantener"|"salir","confianza":0-1,"motivo":"una frase en español"}', timeout=90)
            o['ia'] = {'accion': r.get('accion'), 'confianza': r.get('confianza'), 'motivo': str(r.get('motivo') or '')[:160], 'ts': int(ahora)}
            if str(r.get('accion', '')).lower().startswith('sal') and real._f(r.get('confianza')) >= c['ia_conf_min']:
                return _salir(e, o, f"la IA lo ve mal: {o['ia']['motivo']}")
        except Exception as ex:
            o['ia'] = {'error': str(ex)[:80], 'ts': int(ahora)}


def _seguir(e, c):
    """Orden abierta del piloto: ¿entró?, ¿cerró?, ¿caducó sin entrar?"""
    o = e['orden']
    tipo, x = _buscar(o['id'])
    if not x:
        return
    compro = any(k['order_type'] == 'buy_low' and k.get('status') == 'success' for k in x.get('condition_orders') or [])
    if compro and not o.get('entro'):
        o['entro'] = int(time.time())
        o['precio_entrada'] = real._f(x.get('open_price'))
        _log(e, f"✅ Compró {o['simbolo']} a {o['precio_entrada']:.6g} $ — TP +{o['tp']:.0f} % / SL −{o['sl']:.0f} %")
    if (compro and tipo == 'open') or (o.get('salida') and not o.get('vendido')):
        try:
            _gestionar(e, c, o)
        except Exception as ex:
            e['error'], e['error_ts'] = f'gestión: {str(ex)[:150]}', int(time.time())
        if o.get('salida') and not o.get('vendido'):
            return
    if tipo == 'open' and not compro and time.time() - o['creada'] > c['espera_min'] * 60:
        try:
            _cancelar(o['wallet'], o['id'])
            _log(e, f"⌛ {o['simbolo']} no bajó a {o['compra']:.6g} $ en {c['espera_min']} min: orden anulada")
        except Exception as ex:
            _log(e, f"⚠️ No pude anular {o['id'][:8]}: {str(ex)[:80]}")
        return
    if tipo == 'history':
        res = _resultado(x)
        if o.get('salida'):
            if (_tokens(o['wallet'], o['token']) > 0 and o.get('reintentos', 0) < 20
                    and (not o.get('vendido') or time.time() - o['vendido'] > 90)):
                o['vendido'] = None  # anulada pero aún con monedas: se reintenta la venta
                return _salir(e, o, o['salida'])
            res = 'salida'
        saldo, _ = _saldo()
        pnl = saldo - o['saldo_antes'] if res in ('tp', 'sl', 'cerrada', 'salida') else 0.0
        op = {**o, 'cerrada': int(time.time()), 'resultado': res, 'pnl_sol': round(pnl, 6)}
        e['ops'] = (e.get('ops') or [])[-199:] + [op]
        e['orden'] = None
        # el saldo incluye ≈0,002 SOL de alquiler de la cuenta del token (recuperable al cerrarla): para contar stops manda el precio
        if res == 'sl' or (res in ('salida', 'cerrada') and (o['pnl_pct'] if o.get('pnl_pct') is not None else pnl) < 0):
            e['stops_seguidos'] = e.get('stops_seguidos', 0) + 1
            e.setdefault('enfriar', {})[o['token']] = int(time.time()) + c['enfriar_token_h'] * 3600
        elif res == 'tp':
            e['stops_seguidos'] = 0
        icono = {'tp': '🎯', 'sl': '🛑', 'fallo_compra': '⚠️', 'no_entro': '⌛', 'cerrada': '📤', 'salida': '🚪'}[res]
        _log(e, f"{icono} {o['simbolo']}: {res}{' (' + o['salida'] + ')' if o.get('salida') else ''} · {pnl:+.4f} SOL")
        if e.get('stops_seguidos', 0) >= 2:
            e['pausa_hasta'] = int(time.time()) + c['pausa_stops_h'] * 3600
            e['stops_seguidos'] = 0
            _log(e, f"⏸️ 2 stops seguidos: pausa de {c['pausa_stops_h']} h para no perseguir pérdidas")


def paso():
    c, e = cfg(), _est()
    ahora = int(time.time())
    if ahora - _ult['smart'] >= 60:  # smart money fresco: lo usan la salida anticipada y la IA
        _ult['smart'] = ahora
        try:
            onchain.actualizar_smart()
        except Exception:
            pass
    if e.get('orden'):
        _seguir(e, c)
    if c['activo'] and not e.get('orden'):
        saldo, w = _saldo()
        if not e.get('saldo_inicio') or e.get('inicio_cfg') != c.get('cambiado'):
            e['saldo_inicio'], e['inicio_cfg'] = saldo, c.get('cambiado')
            _log(e, f"▶️ Piloto en marcha: {c['por_op_sol']} SOL por operación, pérdida máx. {c['perdida_max_sol']} SOL (saldo {saldo:.4f})")
        e['saldo'] = saldo
        hoy = time.strftime('%Y-%m-%d')
        n_hoy = sum(1 for o in e.get('ops') or [] if time.strftime('%Y-%m-%d', time.localtime(o['creada'])) == hoy and o['resultado'] != 'no_entro')
        motivo = None
        if e['saldo_inicio'] - saldo >= c['perdida_max_sol']:
            motivo = 'pérdida máxima alcanzada → piloto APAGADO'
            poner_cfg({'activo': False})
        elif saldo < c['por_op_sol'] + c['reserva_sol']:
            motivo = f'saldo {saldo:.4f} SOL no cubre {c["por_op_sol"]} + reserva {c["reserva_sol"]}'
        elif e.get('pausa_hasta', 0) > ahora:
            motivo = 'en pausa tras 2 stops'
        elif n_hoy >= c['max_ops_dia']:
            motivo = 'tope de operaciones de hoy'
        if motivo:
            if e.get('motivo') != motivo:
                _log(e, f"⏹️ {motivo}")
            e['motivo'] = motivo
        elif ahora - _ult['scan'] >= CADA:
            _ult['scan'] = ahora
            e['motivo'] = None
            evitar = {k for k, v in (e.get('enfriar') or {}).items() if v > ahora}
            if c.get('fuente', 'fabrica') == 'fabrica':
                from . import fabrica
                prom = fabrica.promovidas()
                cand = fabrica.senales_ahora(prom, evitar) if prom else []
                e['ultimo_scan'] = {'ts': ahora, 'top': cand[:6], 'promovidas': len(prom)}
                buenos = cand
            else:
                cand = candidatos(evitar)
                e['ultimo_scan'] = {'ts': ahora, 'top': cand[:6]}
                buenos = [x for x in cand if x['pts'] >= c['umbral'] and x['atr'] <= c['atr_max']]
            b = None
            for x in buenos[:3]:
                ok, motivo = _ia_entrada(c, x)
                if ok:
                    b = x
                    if motivo != 'sin IA':
                        _log(e, f"🧠 IA aprueba {x['simbolo']}: {motivo}")
                    break
                _log(e, f"🧠 IA veta {x['simbolo']}: {motivo}")
                e.setdefault('enfriar', {})[x['token']] = ahora + 3600
            if b and b.get('estrategia'):
                # estrategia de la fábrica: entra ya (compra límite un 1 % por ENCIMA = se ejecuta al momento) con su TP/SL/tiempo
                oid, compra, tp, sl = _crear(w, b, b['precio'], compra=b['precio'] * 1.01, tp=b['tp'], sl=b['sl'])
                e['orden'] = {'id': oid, 'token': b['token'], 'simbolo': b['simbolo'], 'creada': ahora, 'wallet': w, 'compra': compra,
                              'tp': tp, 'sl': sl, 'pts': 0, 'atr': b['atr'], 'sol': c['por_op_sol'], 'saldo_antes': saldo,
                              'estrategia': b['estrategia'], 'est_id': b['est_id'], 'hold': b['hold']}
                _log(e, f"📝 {b['simbolo']} por «{b['estrategia']}» (en vivo {b['vivo']['medio']:+.1f} %/op en {b['vivo']['n']}): compra con {c['por_op_sol']} SOL, "
                        f"TP +{tp:.0f} % / SL −{sl:.0f} % / máx. {b['hold'] * 5} min")
            elif b:
                oid, compra, tp, sl = _crear(w, b, b['precio'])
                e['orden'] = {'id': oid, 'token': b['token'], 'simbolo': b['simbolo'], 'creada': ahora, 'wallet': w, 'compra': compra,
                              'tp': tp, 'sl': sl, 'pts': b['pts'], 'atr': b['atr'], 'sol': c['por_op_sol'], 'saldo_antes': saldo}
                _log(e, f"📝 {b['simbolo']} ({b['pts']} pts): compra límite a {compra:.6g} $ con {c['por_op_sol']} SOL, TP +{tp:.0f} % / SL −{sl:.0f} %")
            elif not e.get('ultimo_sin') or ahora - e['ultimo_sin'] > 3600:
                e['ultimo_sin'] = ahora
                mejor = cand[0] if cand else None
                if c.get('fuente', 'fabrica') == 'fabrica':
                    _log(e, f"🏭 {e['ultimo_scan']['promovidas']} estrategias con ventaja en vivo" + (f"; ninguna da señal ahora" if e['ultimo_scan']['promovidas'] else
                            ': ninguna ha demostrado ganar todavía, el dinero real espera'))
                else:
                    _log(e, '🔎 Sin entrada buena' + (f" (mejor: {mejor['simbolo']} {mejor['pts']} pts)" if mejor else ''))
    e['ts'] = ahora
    db.poner_ajuste(EST, e)
    return e


def _bucle():
    while True:
        try:
            paso()
        except Exception as ex:
            e = _est()
            e['error'], e['error_ts'] = str(ex)[:200], int(time.time())
            db.poner_ajuste(EST, e)
        time.sleep(CADA_ORDEN)


def arrancar():
    if real.disponible() and not (_hilo['t'] and _hilo['t'].is_alive()):
        _hilo['t'] = threading.Thread(target=_bucle, daemon=True)
        _hilo['t'].start()
        from . import laboratorio_ia
        laboratorio_ia.arrancar()
        from . import fabrica
        fabrica.arrancar()
    return bool(_hilo['t'])
