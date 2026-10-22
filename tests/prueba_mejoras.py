"""Prueba de las mejoras: costes, toma parcial, stop por tiempo, régimen, confirmación 1 h, walk-forward, IA con historia,
flujo, comité de veto y espejo de Alpaca (base temporal)."""
import os, sys, tempfile, json, time, random
os.environ['DMN_TRADER_DB'] = os.path.join(tempfile.mkdtemp(), 'm.db')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trader import agente as A, escuadron as E, flujo, ia, alpaca, db, datos, llm

fallos = []


def ok(cond, txt):
    print(('✔ ' if cond else '✘ ') + txt)
    if not cond:
        fallos.append(txt)


def vela(t, o, h, l, c):
    return {'t': t, 'o': o, 'h': h, 'l': l, 'c': c, 'v': 1}


# serie sintética: 60 velas planas, señal de impulso, sube a +1,2R y cae a -1R
pre = [vela(1700000000 + 60 * i, 100, 100.05, 99.95, 100 + 0.01 * (i % 2)) for i in range(60)]
pre.append(vela(1700000000 + 3600, 100, 100.55, 100, 100.5))
g = dict(A.GENOMA_BASE, regimen=0, parcial_r=0, entrada='mercado', estrategia='impulso', p={'n': 1, 'umbral': 0.3}, vol_max_pct=50,
         min_ventaja=0.1, rr=2, stop_atr=3, max_barras=60, cortos=False)
dist = A.senales(g, pre)[1][-1] * 3
camino = [100.5 + dist * (k / 20 * 1.2 if k <= 20 else 1.2 - (k - 20) / 10 * 2.3) for k in range(46)]
serie = pre + [vela(pre[-1]['t'] + 60 * (k + 1), camino[k - 1] if k else 100.5, max(camino[k], camino[k - 1] if k else 100.5) + dist * 0.01,
                    min(camino[k], camino[k - 1] if k else 100.5) - dist * 0.01, camino[k]) for k in range(46)]
sin = A.simular(dict(g), [serie], 0.05)
par = A.simular(dict(g, parcial_r=1.0, parcial_frac=0.5), [serie], 0.05)
print(f'  sin parcial {[round(x, 2) for x in sin]} · con toma parcial {[round(x, 2) for x in par]}')
ok(sin and par and sin[0] < -0.9 and 0.3 < par[0] < 0.6, 'toma parcial en +1R: la mitad gana +1R y el resto sale en breakeven')

plano = pre + [vela(pre[-1]['t'] + 60 * (k + 1), 100.5, 100.5 + dist * 0.05, 100.5 - dist * 0.05, 100.5) for k in range(40)]
ok(len(A.simular(dict(g, sin_avance=5), [plano], 0.05)) == 1, 'stop por tiempo: cierra si no arranca')

# coste mínimo: objetivo < 3× costes → no hay operación aunque el gen min_ventaja lo permita
caro = 2 * dist / 100.5 * 100 / 2.5
ok(not A.simular(dict(g, rr=2), [serie], caro), f'objetivo ≥ 3× costes es obligatorio (coste {caro:.2f} %)')

# régimen con ADX
reales = [datos.velas(s, '5m')[:-1] for s in ('BTC-USD', 'ETH-USD', 'SOL-USD')]
for est in ('ema_cruce', 'bollinger'):
    gg = A._g({'genoma': json.dumps(dict(A.GENOMA_BASE, estrategia=est, p=dict(A.ESTRATEGIAS[est]), vol_max_pct=50, marco='5m'))})
    n0 = sum(sum(1 for x in A.senales(dict(gg, regimen=0), v)[0] if x) for v in reales)
    n1 = sum(sum(1 for x in A.senales(dict(gg, regimen=1), v)[0] if x) for v in reales)
    ok(n1 <= n0, f'régimen: {est} pasa de {n0} a {n1} señales (solo en su régimen)')
v = reales[0]
_, _, _, ad = A._senales(dict(gg, regimen=1), v)
ok(sum(1 for x in ad if x is not None) > len(v) // 2 and all(0 <= x <= 100 for x in ad if x is not None), f'ADX calculado (último {ad[-1]:.1f})')

# confirmación de 1 h sin mirar el futuro: la señal de una vela solo usa horas ya cerradas
h1 = datos.velas('BTC-USD', '60m')
gg = A._g({'genoma': json.dumps(dict(A.GENOMA_BASE, estrategia='ema_cruce', p={'r': 5, 'l': 13}, vol_max_pct=50, marco='5m', regimen=0, confirmar_1h=1))})
s_full = A.senales(gg, v, h1)[0]
malas = 0
for k in range(200, len(v), 7):
    corte = [b for b in h1 if b['t'] + 3600 <= v[k]['t'] + 300]
    if A.senales(gg, v[:k + 1], corte)[0][k] != s_full[k]:
        malas += 1
ok(malas == 0, 'confirmación 1 h: no mira el futuro (mismas señales con las horas de después recortadas)')
ok(sum(1 for x in s_full if x) <= sum(1 for x in A.senales(dict(gg, confirmar_1h=0), v)[0] if x), 'confirmación 1 h filtra señales')
ok(not any(A.senales(gg, v, None)[0]), 'confirmación 1 h sin velas de 1 h: no opera')

# walk-forward
gw = dict(A.GENOMA_BASE, estrategia='ema_cruce', p={'r': 5, 'l': 13}, marco='5m', vol_max_pct=50, regimen=0)
pt, rs = A.evaluar(gw, reales, A.coste_pct(A.CUENTA_CFG, 0.02, 'CRYPTOCURRENCY'), 0.02)
a = A.simular(gw, reales, A.coste_pct(A.CUENTA_CFG, 0.02, 'CRYPTOCURRENCY'), 0.02, ventana=(0, 0.6))
b = A.simular(gw, reales, A.coste_pct(A.CUENTA_CFG, 0.02, 'CRYPTOCURRENCY'), 0.02, ventana=(0.6, 1))
ok(len(rs) == len(a) + len(b) and (pt == -9 or pt <= min(A.puntuar(a, 3), A.puntuar(b, 3)) + 1e-9), f'walk-forward: {len(a)} trades antiguos + {len(b)} recientes, nota {pt:+.2f}')

# IA con historia simulada

A.preparar()
e = A.iniciar(12, 1.0, 100.0, ('cripto',))
A.parar('prueba')
t = time.time()
filas = A.muestras_historicas(A.estado(), 3000)
print(f'  {len(filas)} operaciones simuladas en semanas de velas reales en {time.time() - t:.0f}s')
ok(len(filas) >= 300, 'muestras históricas para la IA')
ok(all(set(('atr_pct', 'adx', 'coste_pct')) <= set(f[0]) for f in filas[:50]), 'cada muestra trae sus rasgos (con ADX)')
vivas = [({'atr_pct': 0.3, 'tendencia': 1, 'hora': 3, 'coste_pct': 0.2, 'dist': 1, 'precio': 100}, A.GENOMA_BASE, 1, random.choice([1, -1])) for _ in range(250)]
m = ia.entrenar(vivas, extra=filas)
ok(m['n_sim'] == len(filas) and m['n'] == len(filas) + 250, f"IA entrena con {m['n_sim']} simuladas + 250 reales y valida solo con reales ({m['motivo']})")
ok(len(ia.rasgos({'adx': 30, 'libro': 0.5, 'funding': 1e-5}, A.GENOMA_BASE, 1)) == m['nf'], 'rasgos nuevos: ADX, libro, funding, interés abierto, genes')

# flujo
r = flujo.rasgos('BTC-USD')
ok('funding' in r and 'libro' in r and -1 <= r['libro'] <= 1, f'flujo de BTC: {r}')
ok(flujo.rasgos('AAPL') == {}, 'flujo: acciones sin datos de futuros')
ok(flujo.en_contra({'libro': -0.6}, 1) and not flujo.en_contra({'libro': 0.6}, 1) and flujo.en_contra({'funding': 0.0001}, 1), 'flujo: veta libro en contra y funding extremo')

# comité de veto (con un modelo falso: aprueba la #1, veta la #2)
ag = A.agentes()[:2]
for a_ in ag:
    db.ex('UPDATE ag_agentes SET activo=1 WHERE id=?', (a_['id'],))
    A._nuevo_intento(A.estado(), db.uno('SELECT * FROM ag_agentes WHERE id=?', (a_['id'],)), A._g(a_), 'prueba')
q = datos.cotizacion('BTC-USD')
p, at = q['precio'], q['precio'] * 0.003
for n, a_ in enumerate(ag):
    it = A._intento_actual(a_['id'])
    gx = A._g(a_)
    A._VETO[(a_['id'], 'BTC-USD')] = {'cid': a_['cuenta_id'], 'it': it['id'], 'sim': 'BTC-USD', 'd': 1, 'g': gx, 'q': 0.000001, 'dist': at * 2,
                                      'riesgo': 0.000001 * at * 2, 'p': p, 'lim': False, 'sl': p - 2 * at, 'tp': p + 4 * at, 'atr': at, 'ref': p,
                                      'rasgos': {'atr_pct': 0.3, 'tendencia': 1, 'precio': p, 'dist': at * 2}, 'ts': time.time() - 300 * n, 'aid': a_['id']}
viejo = llm.preguntar
vistos = []
llm.preguntar = lambda texto, **k: (vistos.append(texto), {'decisiones': [{'n': 1, 'ok': True, 'motivo': 'nada en contra'}, {'n': 2, 'ok': False, 'motivo': 'noticias muy bajistas'}]})[1]
A._VETO[(ag[1]['id'], 'BTC-USD')]['ts'] = time.time() - 10
E.vetar(A.estado())
llm.preguntar = viejo
filas_v = db.q('SELECT * FROM ag_vetos ORDER BY id')
ok(len(filas_v) == 2 and sorted(x['ok'] for x in filas_v) == [0, 1], 'comité de veto: una aprobada y una vetada, las dos apuntadas')
ok(vistos and '#1 BTC-USD COMPRA' in vistos[0], 'el comité ve cada operación propuesta')
aprob = [x for x in filas_v if x['ok']][0]
ok(db.uno("SELECT COUNT(*) n FROM ordenes WHERE cuenta_id=(SELECT cuenta_id FROM ag_agentes WHERE id=?) AND origen='agente'", (aprob['agente_id'],))['n'] >= 1,
   'la aprobada se envía al mercado simulado')
ok(not A._VETO, 'cola de veto vacía')
db.ex('UPDATE ag_vetos SET horizonte=?, ts=? WHERE 1', (int(time.time()) - 60, int(time.time()) - 3600))
E.evaluar_vetos()
ok(all(x['r'] is not None for x in db.q('SELECT r FROM ag_vetos')), 'las revisadas se siguen en velas reales para medir si el veto acierta')
print('  ', E.resumen_veto())
llms = [a_ for a_ in A.agentes() if E.es_llm(a_)]
ok(E.N_LLM == 0 and not llms, 'los traders LLM ya no operan (escuadrón = comité de veto)')

# Alpaca: sin claves no hace nada
ok(not alpaca.configurado() and 'sin conectar' in A._resumen_alpaca(), 'Alpaca: sin claves, apagado')
ok('Genes nuevos' in A.informe() and 'Comité de veto' in A.informe(), 'el informe cuenta cómo van los genes nuevos y el veto')
print('\nFALLOS:', fallos if fallos else 'ninguno')
sys.exit(1 if fallos else 0)
