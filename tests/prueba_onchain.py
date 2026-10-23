"""Prueba del mercado on-chain (memecoins de Solana con datos de GMGN): símbolos que conservan la dirección, filtro anti-rug,
velas paginadas, costes de DEX en el motor, especialistas del enjambre (solo memes, solo largos) y gen smart_money.
La API de GMGN se simula; con LIVE=1 además se prueba contra la API real (necesita la key en este equipo)."""
import os, sys, tempfile, time, random
os.environ['DMN_TRADER_DB'] = os.path.join(tempfile.mkdtemp(), 'o.db')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trader import onchain as O, agente as A, motor, datos, db

fallos = []


def ok(cond, txt):
    print(('✔ ' if cond else '✘ ') + txt)
    if not cond:
        fallos.append(txt)


DIR = 'CSocnzcRpgSCr1naKcNTieWZUxb6rABe45j844m7pump'
AHORA = int(time.time()) // 60 * 60


def fila(**k):
    t = {'address': DIR, 'symbol': 'Kimchi', 'name': 'Kimchi', 'liquidity': 400_000, 'volume': 900_000, 'is_wash_trading': False,
         'rug_ratio': 0.1, 'top_10_holder_rate': 0.2, 'renounced_mint': 1, 'renounced_freeze_account': 1, 'open_timestamp': AHORA - 86400,
         'smart_degen_count': 300, 'renowned_count': 20, 'price_change_percent1h': 3.0}
    t.update(k)
    return t


LLAMADAS = []


def api_falsa(ruta, **q):
    LLAMADAS.append((ruta, q))
    if ruta == '/v1/market/rank':
        return {'rank': [fila(), fila(address='Rug111' + 'x' * 38, symbol='RUG', rug_ratio=0.9),
                         fila(address='Frz111' + 'x' * 38, symbol='FRZ', renounced_freeze_account=0),
                         fila(address='New111' + 'x' * 38, symbol='NEW', open_timestamp=AHORA - 600),
                         fila(address='Kim222' + 'x' * 38, symbol='KIMCHI')]}
    if ruta == '/v1/market/token_kline':
        paso = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600, '1d': 86400}[q['resolution']]
        hasta, desde = q['to'] // 1000 // paso * paso, q['from'] // 1000
        ts = [t for t in range(hasta - 99 * paso, hasta + 1, paso) if t >= desde]
        return {'list': [{'time': t * 1000, 'open': '1.0', 'high': '1.02', 'low': '0.98', 'close': str(1 + (t % 7) / 1000), 'volume': '500'} for t in ts]}
    if ruta == '/v1/user/smartmoney':
        return {'list': [{'transaction_hash': 'a', 'timestamp': AHORA, 'base_address': DIR, 'amount_usd': 500, 'side': 'sell'},
                         {'transaction_hash': 'b', 'timestamp': AHORA, 'base_address': DIR, 'amount_usd': 200, 'side': 'buy'},
                         {'transaction_hash': 'c', 'timestamp': AHORA - 7200, 'base_address': DIR, 'amount_usd': 9999, 'side': 'buy'}]}
    raise AssertionError(ruta)


real_api, real_key = O._api, O._api_key
O._api, O._api_key = api_falsa, (lambda: 'falsa')

# 1) filtro anti-rug y símbolos
sims, det, rech = O.universo()
ok(sims == ['KIMCHI@SOL', 'KIMCHI-KIM2@SOL'], f'universo: solo pasan los sanos y el ticker repetido se distingue ({sims})')
ok(rech == {'riesgo de rug': 1, 'mint o freeze sin renunciar': 1, 'demasiado nuevo': 1}, f'motivos de descarte: {rech}')
ok(O.info('kimchi@sol')['address'] == DIR, 'el símbolo en mayúsculas recupera la dirección con sus minúsculas')
ok(O.registrar('SOL', DIR, 'otro') == 'KIMCHI@SOL', 'la misma dirección no crea un símbolo nuevo')
ok(O.revisar(fila(rug_ratio=None)) == ['riesgo de rug'], 'un dato que falta cuenta como fallo')
ok(O.es('KIMCHI@SOL') and not O.es('BTC-USD') and not O.es('A@B'), 'es() reconoce solo los símbolos on-chain')

# 2) velas y precio (por datos.py, como el resto del sistema)
v = datos.velas('KIMCHI@SOL', '5m')
ok(len(v) == 100 and v[-1]['t'] > v[0]['t'], f'velas en vivo: una página de 100 ordenada ({len(v)})')
n0 = len(LLAMADAS)
vh = datos.velas('KIMCHI@SOL', '5m', rango='5d')
ok(len(vh) > 1000 and len(LLAMADAS) - n0 > 1 and len({x['t'] for x in vh}) == len(vh), f'histórico paginado hacia atrás sin repetidas ({len(vh)} velas, {len(LLAMADAS) - n0} peticiones)')
ok(all(1440 * 60 <= q['to'] // 1000 - q['from'] // 1000 + 86400 * 10 for r, q in LLAMADAS if r.endswith('kline')) and LLAMADAS[-1][1]['to'] > 10 ** 12, 'from/to se mandan en milisegundos')
q = datos.cotizacion('KIMCHI@SOL')
ok(q['tipo'] == 'ONCHAIN' and q['bid'] < q['precio'] < q['ask'] and q['abierto'], f"cotización con spread de DEX ({q['bid']:.5f} / {q['ask']:.5f})")

# 3) costes de DEX en el agente y en el motor (también en cuentas creadas antes, sin 'ONCHAIN' en su configuración)
c = A.coste_pct(A.CUENTA_CFG, O.SPREAD_PCT, 'ONCHAIN')
ok(abs(c - (2 * (O.COMISION_PCT + A.CUENTA_CFG['deslizamiento_pct']) + O.SPREAD_PCT)) < 1e-9, f'coste ida y vuelta on-chain {c:.2f} %')
cid = motor.crear_cuenta('memes', 1000, config={'comision_pct': 0.1, 'comision_por_tipo': {}, 'riesgo_estricto': False, 'exigir_stop': False})['id']
o = motor.enviar(cid, 'KIMCHI@SOL', 'compra', importe=100)
com = 1000 - motor.resumen(cid)['efectivo'] - o['cantidad'] * o['precio_ejec']
ok(o['estado'] == 'ejecutada' and abs(com / (o['cantidad'] * o['precio_ejec'] * O.COMISION_PCT / 100) - 1) < 0.01, f'el motor cobra {O.COMISION_PCT} % on-chain ({com:.4f} $)')
ok(o['precio_ejec'] >= q['ask'], 'la compra a mercado paga el ask')

# 4) especialistas del enjambre
rnd = random.Random(3)
g = A.a_onchain(dict(A.GENOMA_BASE, marco='1m', cortos=True), rnd)
ok(g['onchain'] == 1 and g['cortos'] is False and g['marco'] in ('5m', '15m') and g['vol_max_pct'] >= 12, 'a_onchain: solo largos, 5m/15m, más volatilidad')
e = {'mercados': ['cripto', 'acciones'], 'universo': {'cripto': ['BTC-USD'], 'onchain': sims}, 'bloqueos': {}}
ok(A._universo(e, g) == sims, 'el especialista solo ve memes')
ok('KIMCHI@SOL' not in A._universo(e, A.GENOMA_BASE), 'los demás agentes no tocan memes')
ok(set(sims) <= set(A._universo(e)), 'el contexto del tick (sin genoma) sí descarga los memes')
ini = [A.genoma_inicial(i, random.Random(i)) for i in range(10)]
ok(sum(x['onchain'] for x in ini) == 2, '1 de cada 5 agentes nuevos es especialista on-chain')
O._api_key = lambda: None
ok(A._universo(e) == ['BTC-USD', *A.ACCIONES] and not A.genoma_inicial(4, random.Random(4))['onchain'], 'sin key de GMGN no aparece nada on-chain')
O._api_key = lambda: 'falsa'

# 5) smart money
O.actualizar_smart()
ok(O.smart_neto('KIMCHI@SOL') == -300, f"neto de la última hora: {O.smart_neto('KIMCHI@SOL')} $ (la venta de hace 2 h no cuenta)")
ok(O.en_contra(O.rasgos('KIMCHI@SOL'), 1) and not O.en_contra({}, 1), 'gen smart_money: no compra lo que vende el smart money; sin datos no bloquea')

# 6) límite de peticiones
O._api = real_api
O._pausa['hasta'] = time.time() + 60
try:
    O._api('/v1/market/rank'); ok(False, 'respeta la pausa por 429')
except O.ErrorOnchain:
    ok(True, 'tras un 429 espera sin martillear a GMGN')
O._pausa['hasta'] = 0

if os.environ.get('LIVE'):
    O._api_key = real_key
    O._key['ts'] = 0
    sims, det, rech = O.universo()
    ok(bool(sims), f'API real: {len(sims)} memes pasan el filtro ({", ".join(sims)})')
    if sims:
        ok(len(datos.velas(sims[0], '15m')) >= 60, 'API real: velas de 15m suficientes para las estrategias')
        ok(len(datos.velas(sims[0], '5m', rango='3d')) > 100, 'API real: histórico paginado')
    ok(O.actualizar_smart() > 0, 'API real: feed de smart money')

print('\n' + ('TODO OK' if not fallos else f'{len(fallos)} FALLOS: ' + '; '.join(fallos)))
sys.exit(1 if fallos else 0)
