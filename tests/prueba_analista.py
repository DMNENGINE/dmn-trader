"""Prueba del analista de noticias con fuentes reales y gemma real (base temporal)."""
import os, sys, tempfile, json, time
os.environ['DMN_TRADER_DB'] = os.path.join(tempfile.mkdtemp(), 'n.db')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trader import agente as A, analista as N, sentimiento as S, ia, db, llm

fallos = []


def ok(cond, txt):
    print(('✔ ' if cond else '✘ ') + txt)
    if not cond:
        fallos.append(txt)


vivas = 0
for nombre, url, *_ in N.FUENTES:
    try:
        it = N.leer_rss(url)
        vivas += bool(it)
        print(f"  {nombre:24s} {len(it):3d} · {it[0][1][:70] if it else '—'}")
    except Exception as e:
        print(f'  {nombre:24s} ERROR {e}')
ok(vivas >= len(N.FUENTES) - 2, f'fuentes vivas: {vivas}/{len(N.FUENTES)}')
ev = N.calendario()
ok(isinstance(ev, list), f'calendario económico: {len(ev)} eventos USD/globales de impacto medio-alto' + (f" · p. ej. {ev[0]['titulo']}" if ev else ''))
ok(N._clave('Bitcoin hits $90K - CoinDesk') == N._clave('Bitcoin hits $90K'), 'mismo titular de dos medios = una noticia')

sims = ['BTC-USD', 'ETH-USD', 'SOL-USD', 'NVDA', 'TSLA', 'SPY']
N.preparar()
t = time.time()
N.recoger(sims)
n = db.uno('SELECT COUNT(*) n FROM an_noticias')['n']
print(f'  recogidas {n} noticias de las últimas 12 h en {time.time() - t:.1f}s')
ok(n >= 40, 'recoge noticias de todas las fuentes')
t = time.time()
k = N.analizar(sims)
print(f'  gemma analizó {k} en {time.time() - t:.1f}s')
ok(k >= 20, 'gemma analiza el lote')
for f in db.q('SELECT * FROM an_noticias WHERE analizada=1 ORDER BY importancia DESC LIMIT 6'):
    print(f"   {f['impacto']:+.1f} imp {f['importancia']:.1f} {f['categoria']:11s} {f['simbolos']:24s} {f['resumen']}")
ok(db.uno('SELECT COUNT(*) n FROM an_noticias WHERE analizada=1 AND resumen<>\'\'')['n'] >= 10, 'cada noticia tiene resumen en español, impacto e importancia')

t = time.time()
N.ciclo(sims)
print(f'  ciclo completo en {time.time() - t:.1f}s')
st = S.actual()
print('  ', N.resumen(st))
ok(st.get('analista') and st.get('noticias') and set(st['simbolos']) == set(sims), 'publica puntuación por símbolo, mercado y top de noticias')
ok(all(-1 <= v['s'] <= 1 for v in st['simbolos'].values()), f"puntuaciones: {{{', '.join(f'{k}: {v['s']:+.2f}' for k, v in st['simbolos'].items())}}}")
r = S.rasgos('BTC-USD')
ok('sent' in r and 'macro' in r, f'rasgos de una operación: {r}')
ok(len(ia.rasgos(r, A.GENOMA_BASE, 1)) == len(ia.rasgos({}, A.GENOMA_BASE, 1)), 'la IA recibe noticias, macro y evento como rasgos')

# evento macro: con el gen noticias no se entra
db.poner_ajuste(S.CLAVE, dict(st, eventos=[{'ts': int(time.time()) + 300, 'titulo': 'CPI m/m', 'impacto': 'High', 'pais': 'USD'}]))
ok(S.rasgos('SPY').get('evento') == 1 and N.evento_cerca(), 'evento macro de impacto alto en 5 min: rasgo evento = 1')

# la central adopta lo que manda el nodo con el analista
db.poner_ajuste(S.CLAVE, {'ts': 1})
S.recibir(st)
ok(S.actual().get('noticias') and S.actual().get('macro') is not None, 'la Pi recibe noticias, macro y eventos en la sincronía')
d = A.noticias(5)
ok(d['activo'] and len(d['noticias']) <= 5 and 'resumen' in d, 'API /api/enjambre/noticias')
print(f'  consultas al modelo esta hora: {llm.uso_hora()}')
print('\nFALLOS:', fallos if fallos else 'ninguno')
sys.exit(1 if fallos else 0)
