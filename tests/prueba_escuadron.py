"""Prueba del escuadrón LLM y del jefe de mesa con gemma real y precios reales (base temporal)."""
import os, sys, tempfile, json, time, random
os.environ['DMN_TRADER_DB'] = os.path.join(tempfile.mkdtemp(), 'q.db')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trader import agente as A, escuadron as E, motor, db, llm

fallos = []


def ok(cond, txt):
    print(('✔ ' if cond else '✘ ') + txt)
    if not cond:
        fallos.append(txt)


E.N_LLM = 6  # la prueba sigue cubriendo a los traders LLM aunque ahora estén retirados (N_LLM = 0)
ok(E.activo(), f'modelo disponible ({llm.MODELO})')
c = E._limpiar({'rr': 99, 'marco': '4h', 'estrategia': 'rejilla', 'foo': 1, 'tramos': 2.6, 'filtro_tendencia': True, 'roi_min': None})
ok(c.get('rr') == 5.0 and 'marco' not in c and c.get('estrategia') == 'rejilla' and 'p' in c and 'foo' not in c and c.get('tramos') == 3
   and c.get('filtro_tendencia') is True and 'roi_min' in c, f'jefe: solo genes válidos y dentro de rango {c}')

A.preparar()
A.iniciar(12, 1, 100, ['cripto', 'acciones'])
A._parar.set()
e = A.estado()
filas, num = E.mercado(e)
print('  mercado:', len(filas), 'símbolos ·', filas[0] if filas else '-')
ok(len(filas) >= 6, 'tabla de mercado con precios e indicadores reales')

t = time.time()
E.ronda(e)
llms = [a for a in A.agentes() if E.es_llm(a)]
print(f'  ronda en {time.time() - t:.1f}s · consultas esta hora: {llm.uso_hora()}')
for a in llms:
    print(f"   {a['nombre']:18s} {a['ultimo']}")
ok(len(llms) == E.N_LLM and all(a['nombre'].startswith('IA-') for a in llms), f'{E.N_LLM} traders LLM creados')
ok(all((a['ultimo'] or '').startswith('🧠') for a in llms), 'cada uno decidió (operar o esperar) con su razonamiento')
abiertos = db.q("SELECT ag.simbolo, ag.rasgos FROM ag_trades ag JOIN ag_agentes x ON x.id=ag.agente_id WHERE x.genoma LIKE '%\"llm\"%'")
print('  operaciones abiertas por el LLM:', [(x['simbolo'], json.loads(x['rasgos']).get('motivo', '')[:60]) for x in abiertos])
E.ronda(e)  # segunda ronda: ya ve sus posiciones
ok(len([a for a in A.agentes() if E.es_llm(a)]) == E.N_LLM, 'no duplica traders en la segunda ronda')

# el LLM no opera por señales técnicas, ni lo toca la evolución
a = llms[0]
antes = db.uno('SELECT COUNT(*) n FROM ordenes WHERE cuenta_id=?', (a['cuenta_id'],))['n']
A.paso_agente(e, db.uno('SELECT * FROM ag_agentes WHERE id=?', (a['id'],)), A.contexto(e, {'15m'}))
ok(db.uno('SELECT COUNT(*) n FROM ordenes WHERE cuenta_id=?', (a['cuenta_id'],))['n'] == antes, 'paso técnico no abre nada en un trader LLM')
for x in A.agentes():
    for i in range(6):
        tid = db.ex("INSERT INTO trades(cuenta_id,simbolo,direccion,estado,abierto,cerrado,cantidad,entrada,pnl) VALUES(?,?,?,?,?,?,1,1,0)", (x['cuenta_id'], 'BTC-USD', 'largo', 'cerrado', int(time.time()), int(time.time())))
        db.ex('INSERT INTO ag_trades(trade_id,agente_id,simbolo,genoma,rasgos,abierto,r,revisado,estrategia) VALUES(?,?,?,?,?,?,?,1,?)',
              (tid, x['id'], 'BTC-USD', x['genoma'], '{"hora":3,"atr_pct":0.1}', int(time.time()) + 5, -2.0 if E.es_llm(x) else random.gauss(0, 1), A._g(x)['estrategia']))
A.evolucionar(A.estado())
ok(all(db.uno('SELECT genoma FROM ag_agentes WHERE id=?', (x['id'],))['genoma'] == x['genoma'] for x in llms), 'la evolución no reemplaza a los LLM aunque pierdan')

t = time.time()
j = E.jefe(A.estado())
print(f'  jefe en {time.time() - t:.1f}s\n  {E.resumen_jefe()[:900]}')
ok(j and j['analisis'], 'jefe de mesa: escribe un análisis')
ok(all('APLICADA' in p['resultado'] or 'rechazada' in p['resultado'] or 'descartada' in p['resultado'] for p in j['propuestas']), 'cada propuesta pasa por el simulador antes de aplicarse')
ok('Jefe de mesa' in A.informe(), 'el informe incluye al jefe de mesa')
print('\nFALLOS:', fallos if fallos else 'ninguno')
sys.exit(1 if fallos else 0)
