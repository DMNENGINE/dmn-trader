"""Prueba del agente con datos reales (base temporal): pasos en vivo, simulación de soluciones, autopsia y quiebra."""
import os, sys, tempfile, json, time
os.environ['DMN_TRADER_DB'] = os.path.join(tempfile.mkdtemp(), 'a.db')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trader import agente, motor, datos, db

agente.preparar()
r = agente.iniciar(1, 100, ['cripto'])
agente._parar.set()  # sin hilo: llamamos paso() a mano
cid = r['cuenta_id']
print('iniciado', r['intento']['id'], r['genoma']['estrategia'])
# forzar una señal: genoma muy activo
e = agente.estado(); e['genoma'].update(estrategia='impulso', p={'n': 2, 'umbral': 0.0}, min_ventaja=0.1, vol_min_pct=0, vol_max_pct=50); agente.guardar(e)
agente.paso(agente.estado())
pos = motor.posiciones(cid)
print('posiciones tras paso:', [(p['simbolo'], p['lado'], round(p['valor'], 4), p['stop'], p['objetivo']) for p in pos])
print('trades agente:', db.q('SELECT trade_id,simbolo,direccion FROM ag_trades'))
# simulación de candidatos
v = [datos.velas(s, '1m')[:-1] for s in ['BTC-USD', 'ETH-USD']]
for est in agente.ESTRATEGIAS:
    g = dict(agente.GENOMA_BASE); g['estrategia'] = est; g['p'] = dict(agente.ESTRATEGIAS[est])
    rs = agente.simular(g, v, agente.coste_pct(agente.CUENTA_CFG, 0.01))
    print(f'sim {est:10s} trades {len(rs):3d} media {sum(rs)/len(rs) if rs else 0:+.3f}R puntos {agente.puntuar(rs):+.3f}')
# cerrar con pérdida simulada y autopsia (forzamos que pasaron 15 min)
for p in pos:
    motor.cerrar(cid, p['simbolo'])
db.ex('UPDATE trades SET cerrado=cerrado-1000, pnl=-0.01 WHERE cuenta_id=?', (cid,))
e = agente.estado(); it = agente._intento_actual(); agente._registrar_cerrados(e, it); agente.autopsias(agente.estado())
for l in agente.lecciones(): print('LECCIÓN', l['tipo'], '·', l['texto'][:160])
# quiebra forzada
db.ex('UPDATE cuentas SET efectivo=0.01 WHERE id=?', (cid,))
for p in motor.posiciones(cid, False): motor.cerrar(cid, p['simbolo'])
db.ex('UPDATE cuentas SET efectivo=0.01 WHERE id=?', (cid,))
agente.paso(agente.estado())
print('intentos:', [(i['id'], i['resultado'], i['causa']) for i in agente.intentos()])
print([l['texto'][:400] for l in agente.lecciones() if l['tipo'] == 'quiebra'])
print(agente.informe())
print('equity nuevo intento:', motor.resumen(cid)['equity'])
