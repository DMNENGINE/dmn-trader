"""Prueba del sentimiento: Fear & Greed real, titulares reales, puntuación con gemma, sincronía nodo→central y que la IA lo aprende."""
import os, sys, tempfile, json, time, random
os.environ['DMN_TRADER_DB'] = os.path.join(tempfile.mkdtemp(), 's.db')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trader import agente as A, sentimiento as S, ia, db

fallos = []


def ok(cond, txt):
    print(('✔ ' if cond else '✘ ') + txt)
    if not cond:
        fallos.append(txt)


f = S.fear_greed()
ok(0 <= f['valor'] <= 100, f"Fear & Greed real: {f['valor']} ({f['clase']})")
h = S.titulares('BTC-USD')
ok(h, f'titulares BTC: {len(h)} · «{h[0][1][:70]}»' if h else 'titulares BTC')
llm = S.llm_disponible()
ok(llm, f'modelo de lenguaje disponible ({S.MODELO})')
if llm:
    n = S.puntuar({'AAA': ['Company AAA beats earnings, raises guidance, shares jump 12%'],
                   'BBB': ['BBB hit by SEC fraud probe, CEO resigns, shares plunge'],
                   'CCC': ['Weather forecast: sunny weekend ahead']})
    print('  puntuaciones:', n)
    ok(n.get('AAA', 0) > 0.3 and n.get('BBB', 0) < -0.3 and abs(n.get('CCC', 1)) < 0.5, 'gemma: positiva > 0, negativa < 0, irrelevante ≈ 0')
    t = time.time()
    st = S.actualizar(['BTC-USD', 'ETH-USD', 'NVDA', 'TSLA'])
    print(f'  actualizar en {time.time() - t:.1f}s ·', S.resumen())
    ok(st.get('simbolos'), 'actualizar: noticias reales puntuadas y guardadas')
    ok('fng' in S.rasgos('BTC-USD'), f"rasgos de una operación: {S.rasgos('BTC-USD')}")

# central: adopta lo más reciente de un nodo, ignora lo viejo
db.poner_ajuste(S.CLAVE, {'ts': 100, 'simbolos': {}})
S.recibir({'ts': 200, 'simbolos': {'SOL-USD': {'s': 0.7, 'ts': int(time.time())}}, 'modelo': 'gemma'})
S.recibir({'ts': 150, 'simbolos': {'SOL-USD': {'s': -0.9, 'ts': int(time.time())}}})
ok(S.rasgos('SOL-USD').get('sent') == 0.7, 'central: adopta las noticias más recientes del nodo')

# la IA aprende de las noticias si de verdad predicen
rnd, filas = random.Random(4), []
for _ in range(900):
    d, sent = rnd.choice([1, -1]), rnd.uniform(-1, 1)
    ras = {'atr_pct': rnd.uniform(0.02, 0.3), 'tendencia': rnd.choice([-1, 0, 1]), 'hora': rnd.randrange(24), 'coste_pct': 0.22, 'dist': 1, 'precio': 100,
           'fng': rnd.randrange(100), 'sent': sent}
    gana = (sent * d > 0.2) ^ (rnd.random() < 0.15)
    filas.append((ras, dict(A.GENOMA_BASE), d, 1.0 if gana else -1.0))
m = ia.entrenar(filas)
ok(m['ok'] and m['nf'] == len(ia.rasgos({}, A.GENOMA_BASE, 1)), 'IA: se activa cuando las noticias predicen')
base = {'atr_pct': 0.1, 'tendencia': 0, 'hora': 3, 'coste_pct': 0.22, 'dist': 1, 'precio': 100, 'fng': 50}
ok(ia.probabilidad(dict(base, sent=0.8), A.GENOMA_BASE, 1) > 0.6 > ia.probabilidad(dict(base, sent=-0.8), A.GENOMA_BASE, 1), 'IA: compra con noticias buenas sí, con malas no')
db.poner_ajuste(ia.CLAVE, dict(m, nf=m['nf'] - 2))
ia._modelo = None
ok(ia.probabilidad(base, A.GENOMA_BASE, 1) is None, 'IA: un modelo con rasgos antiguos no filtra hasta reentrenar')
print('\nFALLOS:', fallos if fallos else 'ninguno')
sys.exit(1 if fallos else 0)
