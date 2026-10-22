"""Prueba de las ideas de freqtrade: análisis de lookahead (que ninguna señal mire el futuro), tabla ROI, trailing,
enfriamiento, protecciones, mercados por volumen y filtro de IA."""
import os, sys, tempfile, json, time, math, random
os.environ['DMN_TRADER_DB'] = os.path.join(tempfile.mkdtemp(), 'f.db')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trader import agente as A, motor, datos, db, ia

fallos = []


def ok(cond, txt):
    print(('✔ ' if cond else '✘ ') + txt)
    if not cond:
        fallos.append(txt)


def paseo(n=400, semilla=1, deriva=0.0):
    rnd, p, v = random.Random(semilla), 100.0, []
    for i in range(n):
        o = p
        p = max(1, p * (1 + rnd.gauss(deriva, 0.004)))
        v.append({'t': 1700000000 + 60 * i, 'o': o, 'h': max(o, p) * (1 + abs(rnd.gauss(0, 0.001))), 'l': min(o, p) * (1 - abs(rnd.gauss(0, 0.001))), 'c': p})
    return v


# 1) lookahead-analysis: la señal de la vela k no puede cambiar al añadir velas futuras
reales = [datos.velas('BTC-USD', '5m')[:-1], paseo(), paseo(semilla=5)]
for est in A.ESTRATEGIAS:
    g = A._g({'genoma': json.dumps(dict(A.GENOMA_BASE, estrategia=est, p=dict(A.ESTRATEGIAS[est]), vol_max_pct=50, filtro_tendencia=True))})
    malas = 0
    for v in reales:
        s, atr, tend = A.senales(g, v)
        for k in range(80, len(v), 23):
            s2, atr2, tend2 = A.senales(g, v[:k + 1])
            malas += (s2[-1] != s[k]) + (atr2[-1] != atr[k]) + (tend2[-1] != tend[k])
    ok(malas == 0, f'lookahead {est}: la señal no mira el futuro ({malas} diferencias)')

# 2) simulador: ROI decreciente y trailing
pre = [{'t': 1700000000 + 60 * i, 'o': 100, 'h': 100.05, 'l': 99.95, 'c': 100 + 0.01 * (i % 2)} for i in range(60)]
pre.append({'t': 1700000000 + 3600, 'o': 100, 'h': 100.55, 'l': 100, 'c': 100.5})  # impulso: señal de compra
g = dict(A.GENOMA_BASE, estrategia='impulso', p={'n': 1, 'umbral': 0.3}, vol_max_pct=50, min_ventaja=0.1, rr=2, stop_atr=3, max_barras=40, cortos=False)
dist = A.senales(g, pre)[1][-1] * 3
camino = [100.5 + dist * (k / 20 if k <= 20 else 1 - (k - 20) / 10) for k in range(0, 46)]  # sube a +1R y cae a -1,5R
serie = pre + [{'t': pre[-1]['t'] + 60 * (k + 1), 'o': camino[k - 1] if k else 100.5, 'h': max(camino[k], camino[k - 1] if k else 100.5) + dist * 0.01,
                'l': min(camino[k], camino[k - 1] if k else 100.5) - dist * 0.01, 'c': camino[k]} for k in range(46)]
sin = A.simular(dict(g), [serie], 0.05)
roi = A.simular(dict(g, roi_min=0.0), [serie], 0.05)
tr = A.simular(dict(g, trail_r=0.8, trail_frac=0.6), [serie], 0.05)
print(f'  objetivo fijo {[round(x, 2) for x in sin]} · ROI {[round(x, 2) for x in roi]} · trailing {[round(x, 2) for x in tr]}')
ok(sin and roi and tr and sin[0] < -0.9 and roi[0] > 0.5 and tr[0] > 0.2, 'ROI y trailing guardan la ganancia que el objetivo fijo devuelve')
v = paseo(1500, 3)
g2 = dict(A.GENOMA_BASE, estrategia='impulso', p={'n': 2, 'umbral': 0.0}, vol_max_pct=50, min_ventaja=0.1, max_barras=5)
ok(len(A.simular(dict(g2, enfriar=5), [v], 0.01)) < len(A.simular(dict(g2, enfriar=0), [v], 0.01)), 'enfriamiento: menos reentradas seguidas')

# 3) IA: aprende una regla real y no se activa con ruido
def filas(regla, n=900, semilla=2):
    rnd, out = random.Random(semilla), []
    for _ in range(n):
        ras = {'atr_pct': rnd.uniform(0.02, 0.3), 'tendencia': rnd.choice([-1, 0, 1]), 'hora': rnd.randrange(24), 'coste_pct': 0.22, 'dist': rnd.uniform(0.5, 2), 'precio': 100}
        gg = dict(A.GENOMA_BASE, estrategia=rnd.choice(list(A.ESTRATEGIAS)), marco=rnd.choice(list(A.MARCOS)))
        d = rnd.choice([1, -1])
        gana = regla(ras, d, rnd)
        out.append((ras, gg, d, rnd.uniform(0.3, 1.5) if gana else -1.0))
    return out
t0 = time.time()
m = ia.entrenar(filas(lambda r, d, rnd: (r['tendencia'] * d > 0) ^ (rnd.random() < 0.2)))
print(f'  entrenado en {time.time() - t0:.1f}s · {ia.resumen()}')
ok(m['ok'] and m['r_filtrado'] > m['r_todos'], 'IA: aprende «a favor de la tendencia gana» y mejora el R de lo que aprueba')
ok(ia.probabilidad({'atr_pct': 0.1, 'tendencia': 1, 'hora': 3, 'coste_pct': 0.22, 'dist': 1, 'precio': 100}, A.GENOMA_BASE, 1) >
   ia.probabilidad({'atr_pct': 0.1, 'tendencia': -1, 'hora': 3, 'coste_pct': 0.22, 'dist': 1, 'precio': 100}, A.GENOMA_BASE, 1), 'IA: da más probabilidad a favor de tendencia')
m = ia.entrenar(filas(lambda r, d, rnd: rnd.random() < 0.45, semilla=9))
ok(not m['ok'] and ia.probabilidad({'atr_pct': 0.1, 'tendencia': 1, 'hora': 3}, A.GENOMA_BASE, 1) is None, f'IA: con ruido no se activa ({m["motivo"]})')
ok(not ia.entrenar([])['ok'], 'IA: sin datos no filtra')

# 4) mercados por volumen
A.preparar()
A.iniciar(1, 100, 1000, ['cripto'])
A._parar.set()
e = A._refrescar_universo(A.estado())
u = A.estado().get('universo') or {}
print('  universo:', u.get('detalle'))
ok(len(u.get('cripto', [])) >= 4 and all(x[2] <= A.SPREAD_MAX for x in u['detalle']), 'mercados: top por volumen con spread bajo')

# 5) protecciones
a = A.agentes()[0]
it = A._intento_actual(a['id'])
g = A._g(a)
g.update(guardia=3, marco='1m')
cid = a['cuenta_id']
for k in range(3):
    tid = db.ex("INSERT INTO trades(cuenta_id,simbolo,direccion,estado,abierto,cerrado,cantidad,entrada,pnl) VALUES(?,?,?,?,?,?,1,1,0)", (cid, 'DOGE-USD', 'largo', 'cerrado', int(time.time()), int(time.time())))
    db.ex('INSERT INTO ag_trades(trade_id,agente_id,simbolo,genoma,rasgos,abierto,r,revisado) VALUES(?,?,?,?,?,?,?,1)', (tid, a['id'], 'DOGE-USD', json.dumps(A.GENOMA_BASE), '{}', int(time.time()), -1.0))
a = db.uno('SELECT * FROM ag_agentes WHERE id=?', (a['id'],))
db.ex('UPDATE ag_agentes SET genoma_desde=0 WHERE id=?', (a['id'],))
mot = A._proteccion(a, g, it)
ok(mot and '3 pérdidas' in mot, f'StoplossGuard: {mot}')
A._PAUSA[a['id']] = (0, '', A._PAUSA[a['id']][2])  # la pausa termina
ok(A._proteccion(a, g, it) is None, 'tras la pausa no se vuelve a pausar por las mismas pérdidas')
ok('DOGE-USD' in A._vetados(a, cid, dict(g, enfriar=2)), 'LowProfitPairs + enfriamiento: DOGE vetado para este agente')

# 6) gestión en vivo: ROI y trailing
A.CUENTA_CFG['max_posiciones'] = 3
q = datos.cotizacion('BTC-USD')
dist = q['precio'] * 0.01
o = motor.enviar(cid, 'BTC-USD', 'buy', 0.0001, 'mercado', sl=q['precio'] - dist, tp=q['precio'] + 5 * dist, origen='agente')
pz = db.uno('SELECT trade_id FROM posiciones WHERE cuenta_id=? AND simbolo=?', (cid, 'BTC-USD'))
db.ex('INSERT INTO ag_trades(trade_id,agente_id,simbolo,genoma,rasgos,abierto) VALUES(?,?,?,?,?,?)', (pz['trade_id'], a['id'], 'BTC-USD', json.dumps(A.GENOMA_BASE), json.dumps({'dist': dist}), int(time.time())))
antes = motor.posiciones(cid)[0]['stop']
A._gestionar(a, dict(g, roi_min=None, trail_r=-5, trail_frac=0.5, be_en_r=0))
despues = motor.posiciones(cid)[0]['stop']
ok(despues > antes, f'trailing en vivo: stop {antes:.2f} → {despues:.2f}')
db.ex('UPDATE trades SET abierto=abierto-600 WHERE id=?', (pz['trade_id'],))
A._gestionar(a, dict(g, roi_min=-5, trail_r=0, be_en_r=0))
t = db.uno('SELECT estado, nota_cierre FROM trades WHERE id=?', (pz['trade_id'],))
ok(t['estado'] == 'cerrado' and t['nota_cierre'] == 'roi', f'tabla ROI en vivo: cerrado por {t["nota_cierre"]}')

# 7) informe con IA y mercados
inf = A.informe()
ok('Filtro de IA' in inf and 'Mercados cripto' in inf, 'informe muestra IA y mercados')
print('\nFALLOS:', fallos if fallos else 'ninguno')
sys.exit(1 if fallos else 0)
