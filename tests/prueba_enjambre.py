"""Prueba del enjambre con datos reales (base temporal): N agentes, ticks, autopsia, quiebra, evolución y nodo→central."""
import os, sys, tempfile, json, time
os.environ['DMN_TRADER_DB'] = os.path.join(tempfile.mkdtemp(), 'e.db')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trader import agente as A, motor, db

A.preparar()
t = time.time(); r = A.iniciar(20, 1, 100, ['cripto']); A._parar.set()
print(f'creados {r["agentes"]} agentes en {time.time() - t:.1f}s')
for k in range(3):
    t = time.time(); A.paso_enjambre(A.estado()); print(f'tick {k}: {time.time() - t:.2f}s · trades abiertos {db.uno("SELECT COUNT(*) n FROM ag_trades")["n"]}')
v = A._vista_agentes()
print('estados:', {x['estado'] for x in v}, '· ejemplo:', v[0]['nombre'], v[0]['marco'], v[0]['estrategia'], v[0]['ultimo'])
# forzar cierres con pérdida + autopsia
for a in A.agentes()[:5]:
    for p in motor.posiciones(a['cuenta_id'], False): motor.cerrar(a['cuenta_id'], p['simbolo'])
db.ex('UPDATE trades SET cerrado=cerrado-1000, pnl=-0.01')
e = A.estado(); ctx = A.contexto(e, {'1m', '5m', '15m'})
for a in A.agentes()[:5]:
    A._registrar_cerrados(e, a, A._intento_actual(a['id'])); A._autopsias(a)
print('lecciones:', [l['texto'][:90] for l in A.lecciones(4)])
# quiebra de un agente
a = A.agentes()[0]
for p in motor.posiciones(a['cuenta_id'], False): motor.cerrar(a['cuenta_id'], p['simbolo'])
db.ex('UPDATE cuentas SET efectivo=0.01 WHERE id=?', (a['cuenta_id'],))
A.paso_agente(A.estado(), db.uno('SELECT * FROM ag_agentes WHERE id=?', (a['id'],)), ctx)
print('quiebra:', [l['texto'][:300] for l in A.lecciones(10) if l['tipo'] == 'quiebra'])
# evolución: inventar aptitudes
import random
for x in A.agentes():
    for i in range(6):
        tid = db.ex("INSERT INTO trades(cuenta_id,simbolo,direccion,estado,abierto,cerrado,cantidad,entrada,pnl) VALUES(?,?,?,?,?,?,1,1,0)", (x['cuenta_id'], 'BTC-USD', 'largo', 'cerrado', int(time.time()), int(time.time())))
        db.ex('INSERT INTO ag_trades(trade_id,agente_id,simbolo,genoma,rasgos,abierto,r,revisado) VALUES(?,?,?,?,?,?,?,1)', (tid, x['id'], 'BTC-USD', x['genoma'], '{"hora":3,"atr_pct":0.1}', int(time.time()) + 5, random.gauss(0, 1) + (x['id'] % 3 - 1) * 0.5))
A.evolucionar(A.estado())
print('evolución:', [l['texto'][:300] for l in A.lecciones(3) if l['tipo'] == 'evolucion'])
# nodo → central
c = A.recibir_nodo({'nodo': 'pc', 'kpis': A.kpis(), 'agentes': A._vista_agentes(), 'lecciones': [], 'pedir': True, 'migrantes': []})
m = A.mapa()
print('mapa nodos:', [(n['nodo'], len(n['agentes'])) for n in m['nodos']], '· migrantes devueltos:', len(c.get('migrantes', [])))
print(A.informe())
