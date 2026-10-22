"""Prueba de humo del motor con datos reales (usa una base temporal)."""
import os, sys, tempfile, json
os.environ['DMN_TRADER_DB'] = os.path.join(tempfile.mkdtemp(), 't.db')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trader import motor, datos, backtest, analisis

ok = lambda c, m: print(('OK  ' if c else 'FAIL') + ' ' + m) or c
fallos = 0
c = motor.crear_cuenta('test', 10000)
cid = c['id']
p = datos.cotizacion('BTC-USD')['precio']
o = motor.enviar(cid, 'BTC-USD', 'compra', importe=1000, sl=p * 0.95, tp=p * 1.1)
fallos += not ok(o['estado'] == 'ejecutada', f"compra mercado BTC ejecutada a {o['precio_ejec']:.2f}")
st = motor.cuenta(cid)
fallos += not ok(len(st['posiciones']) == 1 and st['posiciones'][0]['stop'] and st['posiciones'][0]['objetivo'], 'bracket SL/TP activo')
fallos += not ok(abs(st['equity'] - 10000) < 5, f"equity ≈ 10000 ({st['equity']})")
try:
    motor.enviar(cid, 'BTC-USD', 'compra', importe=9000, sl=p * 0.5); fallos += not ok(False, 'riesgo excesivo rechazado')
except motor.ErrorOrden as e:
    ok(True, f'riesgo excesivo rechazado: {e}')
t = motor.tamano_por_riesgo(cid, 'BTC-USD', p, p * 0.97)
fallos += not ok(abs(t['riesgo'] - 100) < 1, f"tamaño por riesgo 1%: {t['cantidad']} BTC")
lim = motor.enviar(cid, 'ETH-USD', 'compra', 0.1, 'limite', limite=datos.cotizacion('ETH-USD')['precio'] * 0.5)
fallos += not ok(lim['estado'] == 'pendiente', 'límite lejano queda pendiente')
motor.cancelar(cid, lim['id'])
motor.cerrar(cid, 'BTC-USD', nota='prueba')
st = motor.cuenta(cid)
d = motor.diario(cid)
fallos += not ok(not st['posiciones'] and not st['ordenes'], 'cierre cancela hijos y deja la cuenta plana')
fallos += not ok(d[0]['estado'] == 'cerrado' and d[0]['r'] is not None, f"trade en diario pnl={d[0]['pnl']} R={d[0]['r']}")
# corto
motor.enviar(cid, 'ETH-USD', 'venta', importe=500)
st = motor.cuenta(cid)
fallos += not ok(st['posiciones'][0]['cantidad'] < 0, 'corto abierto')
motor.cerrar(cid, 'ETH-USD')
# acción en euros convertida
eu = motor.enviar(cid, 'SAP.DE', 'compra', 1)
st = motor.cuenta(cid)
ok(True, f"SAP.DE ({eu['estado']}) valor en USD: {[x.get('valor') for x in st['posiciones']]}")
motor.cerrar(cid, 'SAP.DE') if st['posiciones'] else None
# replay
r = motor.crear_cuenta('replay', 10000, replay={'simbolo': 'AAPL', 'intervalo': '1d', 'rango': '2y', 'calentamiento': 100})
rid = r['id']
o = motor.enviar(rid, 'AAPL', 'compra', 4, sl=motor.precio(motor._fila(rid), 'AAPL') * 0.9)
fallos += not ok(o['estado'] == 'pendiente', 'replay: mercado espera a la vela siguiente')
a = motor.replay_avanzar(rid, 1)
fallos += not ok(a['ejecuciones'] == 1 and len(a['velas']) == 102, 'replay: ejecuta en la apertura siguiente y no enseña el futuro')
motor.mover_stop(rid, 'AAPL', trail_pct=5)
a = motor.replay_avanzar(rid, 200)
st = motor.cuenta(rid)
ok(True, f"replay tras 200 velas: equity {st['equity']} posiciones {len(st['posiciones'])} trades {len(motor.diario(rid))}")
s = motor.estadisticas(rid); ok(True, f"stats replay: {json.dumps({k: s.get(k) for k in ('trades','beneficio_neto','r_medio','max_drawdown_pct')})}")
e = motor.examen(cid); ok(True, f"examen: {e['cumplidos']}/{e['total']}")
# backtest
v = datos.velas('SPY', '1d', '5y')
for est in backtest.ESTRATEGIAS:
    b = backtest.ejecutar(v, est, stop_atr=2 if est != 'comprar_mantener' else 0)
    ok(True, f"bt {est:17s} ret {b['retorno_pct']:8.2f}% dd {b['max_drawdown_pct']:6.2f}% sharpe {b['sharpe']} trades {b['trades']}")
op = backtest.optimizar(v, 'cruce_medias', {'rapida': [10, 20], 'lenta': [50, 100]})
ok(True, f"optimizar: {op['probadas']} combos, mejor {op['top'][0]['params']} aviso={op['aviso']}")
an = analisis.analizar('NVDA')
ok(True, f"analisis NVDA: {an['sesgo_tecnico']} · {an['senales']}")
print('FALLOS:', fallos)
sys.exit(1 if fallos else 0)
