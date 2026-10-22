"""Prueba de los genes nuevos: rejilla (grid), entrada con orden límite y tramos DCA. Sintética + datos reales + ciclo en vivo."""
import os, sys, tempfile, json, time, math
os.environ['DMN_TRADER_DB'] = os.path.join(tempfile.mkdtemp(), 'r.db')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trader import agente as A, motor, datos, db

fallos = []


def ok(cond, txt):
    print(('✔ ' if cond else '✘ ') + txt)
    if not cond:
        fallos.append(txt)


# 1) rejilla en un rango lateral sintético: compra abajo, vende arriba
v = [{'t': 1700000000 + 60 * i, 'o': 100 + 2 * math.sin(i / 6), 'h': 100.3 + 2 * math.sin(i / 6), 'l': 99.7 + 2 * math.sin(i / 6), 'c': 100 + 2 * math.sin((i + 1) / 6)} for i in range(300)]
g = A._g({'genoma': json.dumps(dict(A.GENOMA_BASE, estrategia='rejilla', p=dict(A.ESTRATEGIAS['rejilla']), vol_max_pct=50))})
s, atr, _ = A.senales(g, v)
largos = [v[i]['c'] for i in range(len(s)) if s[i] == 1]
cortos = [v[i]['c'] for i in range(len(s)) if s[i] == -1]
ok(largos and cortos and max(largos) < 99 and min(cortos) > 101, f'rejilla: {len(largos)} compras (≤{max(largos or [0]):.2f}) y {len(cortos)} ventas (≥{min(cortos or [0]):.2f})')
tend = [{'t': 1700000000 + 60 * i, 'o': 100 + i * 0.5, 'h': 100.4 + i * 0.5, 'l': 99.8 + i * 0.5, 'c': 100.2 + i * 0.5} for i in range(300)]
ok(not any(A.senales(g, tend)[0]), 'rejilla: en tendencia fuerte no opera')

# 2) simulador: límite ahorra costes; DCA cuenta R frente al riesgo de todos los tramos
base = dict(A.GENOMA_BASE, estrategia='rejilla', p=dict(A.ESTRATEGIAS['rejilla']), vol_max_pct=50, min_ventaja=0.1, rr=1.0, stop_atr=3)
m = A.simular(dict(base), [v], 0.2)
l = A.simular(dict(base, entrada='limite', retroceso=0.1), [v], 0.2, 0.1)
d3 = A.simular(dict(base, tramos=3), [v], 0.2, 0.1)
media = lambda x: sum(x) / len(x) if x else 0
print(f'  mercado {len(m)} trades {media(m):+.3f}R · límite {len(l)} {media(l):+.3f}R · DCA×3 {len(d3)} {media(d3):+.3f}R')
ok(m and l and d3, 'simulador: las tres variantes operan')
ok(all(r >= -1.6 for r in d3), 'DCA: ninguna pérdida pasa de ~-1R del riesgo planeado (+costes)')
ok(A.niveles_dca({'tramos': 3}) == [0.0, 0.3, 0.6] and A.niveles_dca({'tramos': 1}) == [0.0], 'niveles DCA 0 / 30 % / 60 % hacia el stop')

# 3) datos reales: todas las estrategias × mercado/límite/DCA en BTC/ETH/SOL 5m
reales = [datos.velas(x, '5m')[:-1] for x in ['BTC-USD', 'ETH-USD', 'SOL-USD']]
coste = A.coste_pct(A.CUENTA_CFG, 0.01, 'CRYPTOCURRENCY')
ahorro = A.CUENTA_CFG['deslizamiento_pct'] + 0.005
for est in A.ESTRATEGIAS:
    fila = []
    for nombre, extra in (('mercado', {}), ('límite', {'entrada': 'limite'}), ('DCA×3', {'tramos': 3})):
        gg = dict(A.GENOMA_BASE, estrategia=est, p=dict(A.ESTRATEGIAS[est]), marco='5m', **extra)
        rs = A.simular(gg, reales, coste, ahorro)
        fila.append(f'{nombre} {len(rs):3d}/{media(rs):+.2f}R')
    print(f'  {est:10s} ' + ' · '.join(fila))

# 4) motor: una orden límite no se llena con el mínimo de la vela anterior a la orden
cid = motor.crear_cuenta('t', 1000, 'USD', A.CUENTA_CFG, tipo='agente')['id']
b = motor.barra(motor._fila(cid), 'BTC-USD')
if b['l'] < b['c']:
    o = motor.enviar(cid, 'BTC-USD', 'buy', 0.001, 'limite', limite=(b['l'] + b['c']) / 2, sl=b['l'] * 0.95)
    motor.procesar(cid)
    ok(db.uno('SELECT estado FROM ordenes WHERE id=?', (o['id'],))['estado'] == 'pendiente', 'motor: límite entre el mínimo previo y el precio actual NO se llena')
o = motor.enviar(cid, 'BTC-USD', 'buy', 0.001, 'limite', limite=b['c'] * 1.01, sl=b['c'] * 0.95)
motor.procesar(cid)
ok(db.uno('SELECT estado FROM ordenes WHERE id=?', (o['id'],))['estado'] == 'ejecutada', 'motor: límite por encima del precio se llena al momento')

# 5) ciclo en vivo: 1 agente con entrada límite + 3 tramos
A.preparar()
A.iniciar(1, 100, 1000, ['cripto'])
A._parar.set()
a = A.agentes()[0]
g = dict(A._g(a), estrategia='impulso', p={'n': 2, 'umbral': 0.0}, marco='15m', min_ventaja=0.1, vol_min_pct=0, vol_max_pct=50,
         filtro_tendencia=False, entrada='limite', retroceso=-0.3, tramos=3, stop_atr=3, horas_bloqueadas=[])  # retroceso<0: límite «a favor» para que se llene ya
A._poner(a['id'], genoma=json.dumps(g))
A.CUENTA_CFG['max_posiciones'] = 1
e = A.estado()
for k in range(12):  # hasta que salga una señal y se coloque la orden
    A.paso_agente(e, db.uno('SELECT * FROM ag_agentes WHERE id=?', (a['id'],)), A.contexto(e, {'15m'}))
    if A._PEND or motor.posiciones(a['cuenta_id'], False):
        break
    time.sleep(5)
pend = db.q("SELECT * FROM ordenes WHERE cuenta_id=? AND reduce=0 AND padre_id IS NULL", (a['cuenta_id'],))
ok(pend and pend[0]['tipo'] == 'limite', f"vivo: la entrada es una orden límite ({pend[0]['estado'] if pend else '-'})")
A.paso_agente(e, db.uno('SELECT * FROM ag_agentes WHERE id=?', (a['id'],)), A.contexto(e, {'15m'}))
pos = motor.posiciones(a['cuenta_id'], False)
tramos = db.q("SELECT * FROM ordenes WHERE cuenta_id=? AND nota LIKE 'tramo%' AND estado='pendiente'", (a['cuenta_id'],))
agt = db.q('SELECT * FROM ag_trades WHERE agente_id=?', (a['id'],))
ok(pos and len(agt) == 1, 'vivo: el límite se llenó y quedó registrado para aprender')
ok(len(tramos) == 2, f'vivo: 2 tramos DCA puestos ({[round(t["limite"], 2) for t in tramos]})')
t = db.uno('SELECT * FROM trades WHERE id=?', (agt[0]['trade_id'],)) if agt else None
ok(t and abs(t['riesgo'] - pend[0]['cantidad'] * json.loads(agt[0]['rasgos'])['dist'] * 2.1) < 1e-6 * max(1, t['riesgo']),'vivo: R frente al riesgo de los 3 tramos (1 + 0,7 + 0,4)')
for p in pos:
    motor.cerrar(a['cuenta_id'], p['simbolo'], 100, 'prueba')
A.paso_agente(e, db.uno('SELECT * FROM ag_agentes WHERE id=?', (a['id'],)), A.contexto(e, {'15m'}))
ok(all(db.uno('SELECT estado FROM ordenes WHERE id=?', (x['id'],))['estado'] == 'cancelada' for x in tramos), 'vivo: al cerrar la posición se cancelan los tramos sobrantes')

# 6) genomas viejos (sin genes nuevos) siguen funcionando y mutan/cruzan
viejo = {k: v for k, v in A.GENOMA_BASE.items() if k not in ('entrada', 'retroceso', 'espera', 'tramos')}
gv = A._g({'genoma': json.dumps(viejo)})
import random
ok(gv['entrada'] == 'mercado' and A._cruce(viejo, gv, random.Random(1)) and A._mutar(gv, random.Random(2)), 'genoma antiguo: valores por defecto, cruce y mutación OK')
print('\nFALLOS:', fallos if fallos else 'ninguno')
sys.exit(1 if fallos else 0)
