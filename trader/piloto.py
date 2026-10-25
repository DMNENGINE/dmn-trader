"""PILOTO REAL: bot que opera SOLO con dinero real en GMGN dentro de los límites que le da el usuario (tarea + montos).
Cada pocos minutos escanea los memes de Solana que pasan el filtro anti-rug, puntúa los retrocesos dentro de tendencia
(+ smart money) y, si uno llega al umbral, deja en GMGN una orden smart_trade: compra límite en retroceso + TP + SL que
vigila el propio GMGN. Solo una orden suya a la vez; la anula si no entra a tiempo; lleva la cuenta de cada operación.
Frenos duros: pérdida máxima del total (se apaga), pausa tras 2 stops seguidos, tope de operaciones al día, reserva de SOL
para comisiones, enfriamiento por token tras un stop. Corre donde está gmgn-cli con la key (el PC); la configuración la
pone el panel de la central y llega aquí con la respuesta de la sincronía de real.py."""
import json, threading, time, math, urllib.request
from decimal import Decimal, InvalidOperation
from . import conciencia, db, onchain, real, contabilidad_gmgn as CG, supervision_gmgn as SG
from .presupuesto_gmgn import EntradaNoEnviada, presupuesto, PRIORIDAD, PROPINA

CFG, EST = 'piloto_cfg', 'piloto_estado'
CADA = 300            # escaneo
CADA_ORDEN = 30       # seguimiento de la orden abierta
CADA_IA = 300         # revisión de la posición por el modelo
CADA_VELAS = 300
DEFECTO = {'activo': False, 'tarea': 'memes_sol', 'por_op_sol': 0.01, 'perdida_max_sol': 0.008, 'reserva_sol': 0.005,
           'max_ops_dia': 6, 'umbral': 6, 'atr_max': 8.0, 'espera_min': 45, 'pausa_stops_h': 3, 'enfriar_token_h': 6,
           'gestion': True, 'ia': True, 'smart_salida_usd': 300.0, 'ia_conf_min': 0.7, 'fuente': 'fabrica',
           'conciliado': 0}
# conciliado: marca de tiempo del botón «Ya lo revisé» del panel (desbloquea entradas tras revisar GMGN a mano)
# fuente 'fabrica': solo opera las estrategias que la fábrica ha PROMOVIDO (ganan en vivo); 'retroceso': el escáner antiguo
_hilo = {'t': None}
_ult = {'scan': 0, 'smart': 0}


def cfg():
    c = {**DEFECTO, **(db.ajuste(CFG) or {})}
    invalida = any(not isinstance(c[k], bool) for k in ('activo', 'gestion', 'ia'))
    invalida = invalida or any(isinstance(c[k], bool) or not isinstance(c[k], (int, float)) or not math.isfinite(c[k]) or c[k] < 0 for k, v in DEFECTO.items() if isinstance(v, (int, float)) and not isinstance(v, bool))
    if invalida or c['perdida_max_sol'] <= 0 or c['max_ops_dia'] <= 0:
        return {**DEFECTO, 'activo': False, 'error_cfg': 'configuracion persistida invalida; piloto bloqueado'}
    return c


def poner_cfg(nuevo):
    """Desde el panel / APOLO. Al activar se fija el saldo de partida para medir la pérdida máxima."""
    c = cfg()
    if not isinstance(nuevo, dict):
        raise ValueError('configuracion debe ser un objeto')
    for k, v in (nuevo or {}).items():
        if k in DEFECTO:
            defecto = DEFECTO[k]
            if isinstance(defecto, bool):
                if not isinstance(v, bool):
                    raise ValueError(k + ' debe ser booleano')
            elif isinstance(defecto, (int, float)):
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 or (isinstance(defecto, int) and not isinstance(v, int)):
                    raise ValueError(k + ' debe ser numerico, finito y no negativo')
            elif not isinstance(v, str):
                raise ValueError(k + ' debe ser texto')
            c[k] = v
    if c['perdida_max_sol'] <= 0 or c['max_ops_dia'] <= 0:
        raise ValueError('perdida maxima y operaciones diarias deben ser positivas')
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
    try:
        con = conciencia.resumen() if _hilo['t'] else None
    except Exception:
        con = None
    from . import piloto_fut
    try:
        fut = piloto_fut.estado() if _hilo['t'] else None
    except Exception:
        fut = None
    actual = db.ajuste(EST) or {}
    operaciones = [dict(o) for o in actual.get('ops') or []]
    for o in operaciones:
        if not o.get('pnl_confirmado'):
            o['pnl_sin_verificar_sol'] = o.get('pnl_sol')
            o['pnl_sol'] = None
    for i in range(max(0,len(operaciones)-20),len(operaciones)):
        operaciones[i] = {**operaciones[i], 'contabilidad': CG.resumen(operaciones[i])}
    from . import observatorio_gmgn as O
    informe = O.informe()
    return {**actual, 'observatorio': informe, 'observatorio_servicio': db.ajuste('gmgn_observatorio_servicio'), 'ops': operaciones, 'cfg': cfg(), 'lab': lab, 'fabrica': fab, 'conciencia': con, 'futuros': fut,
            'supervision': SG.estado(actual, cfg(), fab), 'contabilidad_orden': CG.resumen(actual['orden']) if actual.get('orden') else None}


def _est():
    return db.ajuste(EST) or {'ops': [], 'log': []}


def _log(e, txt):
    e['log'] = (e.get('log') or [])[-59:] + [[int(time.time()), txt]]


def _saldo():
    if not real._wallet():
        real._ultimo['info'] = onchain._api('/v1/user/info')
    w = real._wallet()
    d = onchain._api('/v1/user/wallet_token_balance', chain='sol', wallet_address=w, token_address='So11111111111111111111111111111111111111111')
    valor = float(d['balances'][0]['balance'])
    if not w or not math.isfinite(valor) or valor < 0:
        raise ValueError('saldo SOL no verificable')
    return valor, w


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

def _crear(w, c, precio, compra=None, tp=None, sl=None, config=None):
    config = cfg() if config is None else config
    def vigente():
        if cfg() != config:
            raise EntradaNoEnviada('configuracion modificada antes del envio')
    vigente()
    if isinstance(precio, bool) or not isinstance(precio, (int, float)) or not math.isfinite(precio) or precio <= 0:
        raise EntradaNoEnviada('precio no verificable')
    sl = sl or max(8.0, min(2.5 * c['atr'], 18.0))
    tp = tp or 2 * sl
    compra = compra or precio * (1 - c['atr'] / 200)  # medio ATR por debajo: no perseguir
    for valor in (compra, tp, sl):
        if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not math.isfinite(valor) or valor <= 0:
            raise EntradaNoEnviada('parametros de orden no verificables')
    if Decimal('%.10g' % compra) >= Decimal('%.10g' % precio):
        raise EntradaNoEnviada('buy_low requiere precio inferior al actual; entrada inmediata no implementada')
    lam = str(presupuesto(config)['importe_lamports'])
    bp = '{"slippage":10,"priority_fee":"%s","tip_fee":"%s"}' % (PRIORIDAD, PROPINA)
    co = ('[{"order_type":"buy_low","side":"buy","check_price":"%.10g"},'
          '{"order_type":"profit_stop","side":"sell","price_scale":"%g","sell_ratio":"100"},'
          '{"order_type":"loss_stop","side":"sell","price_scale":"%g","sell_ratio":"100"}]') % (compra, round(tp, 1), round(sl, 1))
    r = real._cli('order', 'strategy', 'create', '--chain', 'sol', '--from', w, '--base-token', c['token'], '--quote-token', real.SOL,
                  '--order-type', 'smart_trade', '--sub-order-type', 'mix_trade', '--open-price', '%.10g' % precio,
                  '--amount-in', lam, '--quote-investment', lam, '--slippage', '10', '--priority-fee', PRIORIDAD,
                  '--tip-fee', PROPINA, '--buy-param', bp, '--sell-param', bp, '--condition-orders', co, preflight=vigente)
    return r['order_id'], compra, tp, sl


def _crear_seguro(e, w, candidato, precio, **kw):
    config = _autorizar_entrada(e, w)
    if e.get('intencion_entrada'):
        raise RuntimeError('entrada pendiente de conciliacion; no se reenvia')
    e['intencion_entrada'] = {'ts': int(time.time()), 'wallet': w, 'token': candidato['token'], 'estado': 'enviando'}
    db.poner_ajuste(EST, e)
    from . import ejecutor
    try:
        resultado = _crear(w, candidato, precio, config=config, **kw)
    except (ejecutor.Bloqueado, EntradaNoEnviada):
        e.pop('intencion_entrada', None)
        db.poner_ajuste(EST, e)
        raise
    oid, compra, tp, sl = resultado
    # Guardar el identificador antes de devolver el control al bucle.
    e['orden'] = {**candidato, 'id': oid, 'creada': int(time.time()), 'wallet': w, 'compra': compra, 'tp': tp, 'sl': sl,
                  'sol': presupuesto(config)['importe_lamports'] / 1e9, 'saldo_antes': e.get('saldo', 0), 'pts': candidato.get('pts', 0)}
    e.pop('intencion_entrada', None)
    CG.operacion(e['orden'])
    db.poner_ajuste(EST, e)
    return resultado


def _autorizar_entrada(e, wallet):
    from . import observatorio_gmgn as O
    try:
        return _autorizar_entrada_base(e, wallet)
    except Exception as ex:
        try:
            message = str(ex).lower()
            categories = [('desactivado', 'piloto_apagado'), ('incidencia', 'incidencia_pendiente'), ('wallet', 'wallet_no_conciliada'), ('base de riesgo', 'base_de_riesgo_no_verificable'), ('presupuesto', 'presupuesto_insuficiente'), ('previa', 'operacion_previa_pendiente'), ('configuracion', 'configuracion_modificada')]
            reason = next((value for key, value in categories if key in message), 'prevalidacion_no_superada')
            O.decision('entrada_bloqueada', motivo=reason, datos={'error_tipo': type(ex).__name__})
        except Exception:
            pass
        raise


def _autorizar_entrada_base(e, wallet):
    """Revalidar despues de escaner/IA: apagar el piloto debe impedir el envio tardio."""
    c = cfg()
    if c.get('activo') is not True:
        raise ValueError('piloto desactivado antes del envio')
    SG.admitir(wallet)
    saldo, actual = _saldo()
    if actual != wallet or (e.get('riesgo_wallet') and e['riesgo_wallet'] != wallet):
        raise ValueError('wallet distinta de la sesion de riesgo')
    base = e.get('saldo_inicio')
    if base is None or isinstance(base, bool) or not isinstance(base, (int, float)) or not math.isfinite(base):
        raise ValueError('base de riesgo no verificable')
    plan = presupuesto(c)
    saldo_lamports = Decimal(str(saldo)) * 1000000000
    if base - saldo >= c['perdida_max_sol'] or saldo_lamports < plan['necesario_lamports']:
        raise ValueError('entrada fuera del presupuesto de riesgo')
    if e.get('orden') or e.get('intencion_entrada'):
        raise ValueError('operacion previa sin conciliar')
    if cfg() != c:
        raise EntradaNoEnviada('configuracion modificada durante consulta de saldo')
    e['riesgo_wallet'] = wallet
    return c


def _buscar(oid, wallet=None, token=None, grupo='STMix'):
    """Ausencia no confirma cancelacion; pagina sin mezclar wallets ni tokens."""
    errores = False
    for tipo in ('open', 'history'):
        cursor, vistos = None, set()
        for _ in range(10):
            args = ['order', 'strategy', 'list', '--chain', 'sol', '--group-tag', grupo, '--type', tipo, '--limit', '100']
            if wallet:
                args += ['--from', wallet]
            if token:
                args += ['--base-token', token]
            if cursor:
                args += ['--page-token', cursor]
            try:
                pagina = real._cli(*args)
                if not isinstance(pagina, dict) or not isinstance(pagina.get('list'), list):
                    raise ValueError('respuesta de ordenes incompleta')
                for orden in pagina['list']:
                    if orden.get('order_id') != oid:
                        continue
                    if wallet and orden.get('wallet_address') != wallet:
                        raise ValueError('wallet de la orden no coincide')
                    if token and orden.get('base_token') != token:
                        raise ValueError('token de la orden no coincide')
                    return tipo, orden
                cursor = pagina.get('next_page_token')
                if not cursor or cursor in vistos:
                    break
                vistos.add(cursor)
            except Exception:
                errores = True
                break
    return ('desconocida' if errores else 'no_encontrada'), None


def _comprada(orden):
    if orden.get('open_sign_hash'):
        return True
    try:
        cantidad = float((orden.get('order_statistic') or {}).get('buy_amount') or 0)
        if math.isfinite(cantidad) and cantidad > 0:
            return True
    except (ValueError, TypeError, OverflowError):
        pass
    return any(k.get('order_type') == 'buy_low' and k.get('status') == 'success' for k in orden.get('condition_orders') or [])


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
    if not _comprada(o):
        return 'no_entro'
    return 'cerrada'


# ---------------- gestión activa (1: stop que sube · 2: salida anticipada · 3: revisión con IA) ----------------

def _precio(tok):
    return float(onchain._api('/v1/token/info', chain='sol', address=tok)['price']['price'])


def _tokens(w, tok):
    import math
    d = onchain._api('/v1/user/wallet_token_balance', chain='sol', wallet_address=w, token_address=tok)
    valor = float(d['balances'][0]['balance'])
    if not math.isfinite(valor) or valor < 0:
        raise ValueError('saldo de tokens desconocido')
    return valor


def _vender(o):
    """Vende la cantidad propia en unidades atomicas, sin liquidar todo el token de la wallet."""
    cantidad = str(o.get('cantidad_salida_atomica') or '')
    if not cantidad.isdigit() or int(cantidad) <= 0:
        raise EntradaNoEnviada('cantidad propia no conciliada: no enviar venta')
    return real._cli('swap', '--chain', 'sol', '--from', o['wallet'], '--input-token', o['token'], '--output-token', real.SOL,
              '--amount', cantidad, '--slippage', '15', '--priority-fee', PRIORIDAD, '--tip-fee', PROPINA, timeout=60)


def _cantidad_propia(remota):
    st = remota.get('order_statistic') or {}
    try:
        compra = Decimal(str(st['buy_amount']))
        venta = Decimal(str(st['success_sell_amount']))
        if not compra.is_finite() or not venta.is_finite() or compra < 0 or venta < 0 or venta > compra:
            raise ValueError('cantidades de la orden incoherentes')
        if compra != compra.to_integral_value() or venta != venta.to_integral_value() or compra > Decimal(2**256-1):
            raise ValueError('cantidad atomica no entera')
        return int(compra) - int(venta)
    except (KeyError, InvalidOperation, TypeError):
        raise ValueError('cantidad propia de la orden no verificable')


def _salir(e, o, motivo):
    from . import ejecutor
    try:
        # Si no se puede leer el saldo se mantienen las protecciones del broker.
        saldo = _tokens(o['wallet'], o['token'])
        if not o.get('salida'):
            _, previa = _buscar(o['id'], o['wallet'], o['token'], **({'grupo':o['grupo_proteccion']} if o.get('grupo_proteccion') else {}))
            if not previa:
                raise ValueError('orden no conciliada antes de retirar protecciones')
            _cantidad_propia(previa)
            o['salida_solicitada'] = o.get('salida_solicitada') or motivo
            o['cancelacion_estado'] = 'solicitada'
            db.poner_ajuste(EST, e)
            _cancelar(o['wallet'], o['id'])
            tipo, remota = _buscar(o['id'], o['wallet'], o['token'], **({'grupo':o['grupo_proteccion']} if o.get('grupo_proteccion') else {}))
            if not remota or remota.get('status') != 'closed' or remota.get('strategy_status') != 'stopped':
                o['cancelacion_estado'] = 'pendiente_confirmacion'
                return
            o['cancelacion_estado'] = 'confirmada'
            o['salida'] = motivo
            o['cantidad_salida_atomica'] = str(_cantidad_propia(remota))
            db.poner_ajuste(EST, e)
        # Consultar de nuevo: el TP/SL pudo ejecutarse mientras se cancelaba.
        saldo = _tokens(o['wallet'], o['token'])
        if saldo == 0 or o.get('cantidad_salida_atomica') == '0':
            o['vendido'] = int(time.time())
            o['venta_estado'] = 'saldo_cero_confirmado'
        elif o.get('venta_intento'):
            from . import salida_gmgn
            salida_gmgn.conciliar(o)
        else:
            if o.get('venta_envios', 0) >= 3:
                o['venta_estado'] = 'limite_salidas_requiere_revision'
                db.poner_ajuste(EST, e)
                return
            o['venta_intento'] = int(time.time())
            o['venta_envios'] = o.get('venta_envios', 0) + 1
            o['venta_estado'] = 'enviando'
            db.poner_ajuste(EST, e)
            try:
                respuesta = _vender(o)
            except (EntradaNoEnviada, ejecutor.Bloqueado):
                o.pop('venta_intento', None)
                o['venta_envios'] -= 1
                raise
            if isinstance(respuesta, dict) and respuesta.get('order_id'):
                o['venta_orden_id'] = respuesta['order_id']
            o['venta_estado'] = 'pendiente_conciliacion'
        db.poner_ajuste(EST, e)
    except (EntradaNoEnviada, ejecutor.Bloqueado) as ex:
        o['venta_estado'] = 'no_enviada'
        _log(e, f"salida no enviada de {o['simbolo']}: {type(ex).__name__}")
        db.poner_ajuste(EST, e)
    except Exception as ex:
        o['venta_estado'] = 'pendiente_conciliacion'
        SG.registrar(o['wallet'], CG.identificador(o), 'salida', 'Salida no conciliada; no repetir la venta.')
        o['reintentos'] = o.get('reintentos', 0) + 1
        _log(e, f"salida pendiente de {o['simbolo']}: {type(ex).__name__}; no se duplica el envio")
        db.poner_ajuste(EST, e)


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


def _apolo(tipo, simbolo, propuesta, datos):
    """Decide el cerebro de APOLO (modelo del cerebro + su memoria de trading): POST /v1/trading/decidir en este equipo."""
    tok = open(conciencia.TOKEN_APOLO, encoding='utf-8').read().strip()
    rq = urllib.request.Request(conciencia.APOLO + '/v1/trading/decidir', method='POST',
                                data=json.dumps({'tipo': tipo, 'simbolo': simbolo, 'propuesta': propuesta, 'datos': datos}).encode(),
                                headers={'content-type': 'application/json', 'x-robot-token': tok})
    with urllib.request.urlopen(rq, timeout=130) as r:
        return json.loads(r.read().decode('utf-8'))


def _ia_entrada(c, b):
    """(True, motivo) entra · (False, motivo) APOLO veta · (None, motivo) APOLO no contesta: con dinero real no se entra sin él."""
    if not c['ia']:
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
        r = _apolo('entrada', b['simbolo'], prop, _contexto(b['token'], b['simbolo']))
    except Exception as ex:
        return None, f'APOLO no contestó ({str(ex)[:60]}): no entro'
    veto = str(r.get('decision', '')).lower().startswith('vet') and real._f(r.get('confianza')) >= 0.6
    return (not veto), str(r.get('motivo') or '')[:160]


def _gestionar(e, c, o):
    """Posición abierta: máximo, stop que sube, salidas anticipadas y revisión del modelo."""
    ahora = time.time()
    if o.get('salida') or o.get('salida_solicitada'):
        if not o.get('vendido'):
            _salir(e, o, o.get('salida') or o['salida_solicitada'])
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
    stop = max(stop or 0, o.get('stop_dyn') or 0) or None
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
    if c['ia'] and ahora - o.get('ia_ts', 0) >= CADA_IA:
        o['ia_ts'] = ahora
        try:
            r = _apolo('posicion', o['simbolo'],
                       f"Posición LARGA: entrada {ent:.6g} $, ahora {o['pnl_pct']:+.1f} %, máximo {(o['max'] / ent - 1) * 100:+.1f} %, "
                       f"TP +{o['tp']:.0f} %, SL −{o['sl']:.0f} %, abierta hace {int((ahora - o['entro']) / 60)} min. ¿Mantener hasta TP/SL o salir ya?",
                       _contexto(o['token'], o['simbolo']))
            o['ia'] = {'accion': r.get('accion'), 'confianza': r.get('confianza'), 'motivo': str(r.get('motivo') or '')[:160], 'ts': int(ahora)}
            if str(r.get('accion', '')).lower().startswith('sal') and real._f(r.get('confianza')) >= c['ia_conf_min']:
                return _salir(e, o, f"APOLO lo ve mal: {o['ia']['motivo']}")
        except Exception as ex:
            o['ia'] = {'error': str(ex)[:80], 'ts': int(ahora)}


def _seguir(e, c):
    if e['orden'].get('modo') == 'mercado':
        from . import mercado_gmgn
        return mercado_gmgn.seguir(e, c)
    return _seguir_estrategia(e, c)


def _seguir_estrategia(e, c, conciliada=None):
    """Orden abierta del piloto: ¿entró?, ¿cerró?, ¿caducó sin entrar?"""
    o = e['orden']
    tipo, x = conciliada if conciliada is not None else _buscar(o['id'], o['wallet'], o['token'], **({'grupo':o['grupo_proteccion']} if o.get('grupo_proteccion') else {}))
    if not x:
        o['estado'] = 'pendiente_conciliacion'
        return
    compro = _comprada(x)
    if compro and x.get('open_sign_hash'):
        cantidad = (x.get('order_statistic') or {}).get('buy_amount')
        CG.anotar(o, x['open_sign_hash'], 'entrada', esperado=cantidad if isinstance(cantidad,str) and cantidad.isdigit() else None)
    if compro and not o.get('entro'):
        o['entro'] = int(time.time())
        o['precio_entrada'] = real._f(x.get('open_price'))
        _log(e, f"✅ Compró {o['simbolo']} a {o['precio_entrada']:.6g} $ — TP +{o['tp']:.0f} % / SL −{o['sl']:.0f} %")
    if (compro and tipo == 'open') or ((o.get('salida') or o.get('salida_solicitada')) and not o.get('vendido')):
        try:
            _gestionar(e, c, o)
        except Exception as ex:
            e['error'], e['error_ts'] = f'gestión: {str(ex)[:150]}', int(time.time())
        if (o.get('salida') or o.get('salida_solicitada')) and not o.get('vendido'):
            return
    if tipo == 'open' and not compro and time.time() - o['creada'] > c['espera_min'] * 60:
        try:
            _cancelar(o['wallet'], o['id'])
            _log(e, f"⌛ {o['simbolo']} no bajó a {o['compra']:.6g} $ en {c['espera_min']} min: orden anulada")
        except Exception as ex:
            _log(e, f"⚠️ No pude anular {o['id'][:8]}: {str(ex)[:80]}")
        return
    if tipo == 'history':
        if x.get('status') != 'closed':
            o['estado'] = 'pendiente_conciliacion'
            return
        # Una estrategia archivada no demuestra que no queden tokens.
        if compro and _cantidad_propia(x) > 0 and _tokens(o['wallet'], o['token']) > 0 and not o.get('salida'):
            o['estado'] = 'pendiente_conciliacion'
            return
        res = _resultado(x)
        if o.get('salida'):
            if o.get('cantidad_salida_atomica') != '0' and _tokens(o['wallet'], o['token']) > 0:
                o['vendido'] = None  # seguir conciliando sin duplicar el swap
                return _salir(e, o, o['salida'])
            res = 'salida'
        saldo, _ = _saldo()
        variacion = saldo - o['saldo_antes']
        pnl = None
        op = {**o, 'cerrada': int(time.time()), 'resultado': res, 'pnl_sol': None, 'pnl_confirmado': False, 'variacion_saldo_sol': round(variacion, 9)}
        CG.operacion(op)
        if x.get('close_sign_hash'):
            CG.anotar(op, x['close_sign_hash'], 'salida')
        SG.resolver_posicion(op['wallet'], CG.identificador(op))
        e['ops'] = (e.get('ops') or [])[-199:] + [op]
        if op.get('pnl_confirmado'):
            try:
                conciencia.operacion_real(op)
            except Exception:
                pass
        e['orden'] = None
        # el saldo incluye ≈0,002 SOL de alquiler de la cuenta del token (recuperable al cerrarla): para contar stops manda el precio
        if res == 'sl' or (res in ('salida', 'cerrada') and (o.get('pnl_pct') is not None and o['pnl_pct'] < 0)):
            e['stops_seguidos'] = e.get('stops_seguidos', 0) + 1
            e.setdefault('enfriar', {})[o['token']] = int(time.time()) + c['enfriar_token_h'] * 3600
        elif res == 'tp':
            e['stops_seguidos'] = 0
        icono = {'tp': '🎯', 'sl': '🛑', 'fallo_compra': '⚠️', 'no_entro': '⌛', 'cerrada': '📤', 'salida': '🚪'}[res]
        _log(e, f"{icono} {o['simbolo']}: {res}{' (' + o['salida'] + ')' if o.get('salida') else ''} · resultado neto pendiente de conciliar")
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
    if c.get('conciliado', 0) > e.get('conciliado_hecho', 0):
        _conciliado_a_mano(e, c['conciliado'])
    if e.get('orden'):
        _seguir(e, c)
    if e.get('intencion_entrada'):
        e['error'] = 'entrada GMGN sin confirmacion: conciliar en el broker antes de continuar'
        db.poner_ajuste(EST, e)
        return e
    if c['activo'] and not e.get('orden'):
        saldo, w = _saldo()
        if e.get('saldo_inicio') is None:
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
                if ok is None:  # APOLO cerrado o sin modelo: no se entra ni se castiga al token
                    _log(e, f"🧠 {motivo}")
                    break
                if ok:
                    b = x
                    if motivo != 'sin IA':
                        _log(e, f"🧠 APOLO aprueba {x['simbolo']}: {motivo}")
                    break
                from . import observatorio_gmgn as O
                O.decision('entrada_vetada', x.get('est_id', ''), x['token'], 'veto_ia')
                _log(e, f"🧠 APOLO veta {x['simbolo']}: {motivo}")
                e.setdefault('enfriar', {})[x['token']] = ahora + 3600
            if b and b.get('estrategia'):
                # Compra a mercado con protecciones adjuntas y conciliacion independiente.
                from . import mercado_gmgn
                oid, compra, tp, sl = mercado_gmgn.abrir(e, w, b, b['precio'])
                e['orden'].update(estrategia=b['estrategia'], est_id=b['est_id'], hold=b['hold'])
                _log(e, f"📝 {b['simbolo']} por «{b['estrategia']}» (en vivo {b['vivo']['medio']:+.1f} %/op en {b['vivo']['n']}): compra con {c['por_op_sol']} SOL, "
                        f"TP +{tp:.0f} % / SL −{sl:.0f} % / máx. {b['hold'] * 5} min")
            elif b:
                oid, compra, tp, sl = _crear_seguro(e, w, b, b['precio'])
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
        try:
            CG.procesar()
            _confirmar_ops()
        except Exception as ex:
            e = _est()
            _log(e, 'Contabilidad pendiente de revisar: ' + type(ex).__name__)
            db.poner_ajuste(EST, e)
        time.sleep(CADA_ORDEN)


def _conciliado_a_mano(e, ts):
    """El usuario revisó GMGN: se olvida la entrada sin confirmar y se resuelven las incidencias. La orden seguida (si hay) sigue."""
    intencion = e.pop('intencion_entrada', None)
    n = SG.resolver_todas()
    if e.get('orden'):
        e['orden'].pop('incidencia', None)
    e['conciliado_hecho'], e['error'] = ts, None
    _log(e, f"✅ Revisado a mano en GMGN: {n} incidencia(s) resuelta(s)" + (f", entrada sin confirmar de {intencion.get('token', '?')[:8]} olvidada" if intencion else ''))
    db.poner_ajuste(EST, e)


def _confirmar_ops():
    """Fija el PnL de las operaciones cerradas cuando sus recibos quedan verificados y se lo pasa a la conciencia."""
    e = _est()
    nuevas = []
    for op in e.get('ops') or []:
        if op.get('pnl_confirmado') or 'pnl_confirmado' not in op:  # las antiguas (sin libro) se quedan como estaban
            continue
        pnl = CG.confirmar(op)
        if pnl is None:
            continue
        op['pnl_sol'], op['pnl_confirmado'] = round(pnl, 9), True
        nuevas.append(op)
    if not nuevas:
        return
    for op in nuevas:
        _log(e, f"🧾 {op.get('simbolo')}: neto confirmado por recibos {op['pnl_sol']:+.6f} SOL")
    db.poner_ajuste(EST, e)
    for op in nuevas:
        try:
            conciencia.operacion_real(op)
        except Exception:
            pass


def arrancar():
    if real.disponible() and not (_hilo['t'] and _hilo['t'].is_alive()):
        _hilo['t'] = threading.Thread(target=_bucle, daemon=True)
        _hilo['t'].start()
        from . import laboratorio_ia
        laboratorio_ia.arrancar()
        from . import fabrica
        fabrica.arrancar()
        from . import observatorio_gmgn
        observatorio_gmgn.arrancar()
        from . import piloto_fut
        piloto_fut.arrancar()  # futuros por NinjaTrader (Sim101; la real solo si está en puente.json y en su cfg)
    return bool(_hilo['t'])
