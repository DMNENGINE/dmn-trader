"""Prueba del simulador Up/Down de Polymarket: comisión oficial, modelo de probabilidad, compra recorriendo el libro, decisiones
de cada estrategia, ciclo completo apuesta → resolución oficial → cobro, quiebra, evolución e informe. Polymarket y Coinbase se
simulan; con LIVE=1 además se lee un mercado real."""
import os, sys, tempfile, time, json, random
os.environ['DMN_TRADER_DB'] = os.path.join(tempfile.mkdtemp(), 'u.db')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trader import updown as U, datos, db

fallos = []


def ok(cond, txt):
    print(('✔ ' if cond else '✘ ') + txt)
    if not cond:
        fallos.append(txt)


# 1) comisión: tabla oficial (100 acciones a 0,45 → 0,69 $; máximo 1,56 % a 50 ¢)
ok(abs(U.comision(100, 0.45) - 0.69) < 0.005, f'comisión 100 acc. a 0,45 = {U.comision(100, 0.45):.3f} $')
ok(abs(U.comision(100, 0.5) / 50 - 0.015625) < 1e-9 and U.comision(100, 0.99) < 0.005, 'máximo 1,56 % a 50 ¢ y casi nada en los extremos')

# 2) modelo
ok(U.p_subida(100, 100, 0.001, 600) == 0.5 and U.p_subida(101, 100, 0.001, 600) > 0.9 and U.p_subida(99.99, 100, 0.001, 600) < 0.5, 'p_subida: 50 % en el empate, alta si va ganando')
ok(U.p_subida(100.05, 100, 0.001, 60) > U.p_subida(100.05, 100, 0.001, 600), 'con menos tiempo restante, la ventaja actual pesa más')
ok(U.p_subida(101, 100, 0.001, 0) == 1.0 and U.p_subida(99, 100, 0.001, 0) == 0.0, 'al final: 1 o 0')

# 3) compra recorriendo el libro
acc, coste, pm = U.comprar([(0.40, 2), (0.42, 100)], 2.0)
ok(abs(coste - 2.0) < 1e-9 and abs(acc - (2 + 1.2 / 0.42)) < 1e-9 and 0.40 < pm < 0.42, f'compra de 2 $ recorre el libro ({acc:.2f} acc. a {pm:.3f})')
ok(U.comprar([], 1) is None, 'sin libro no compra')


def merc(S, K, up, down, frac=0.6, segs=300):
    return {'slug': 'btc-updown-15m-1', 'activo': 'btc', 'marco': '15m', 'S': S, 'K': K, 'sigma': 0.0008, 'segs': segs, 'frac': frac,
            'libros': {'Up': {'asks': [(up, 500)], 'bids': []}, 'Down': {'asks': [(down, 500)], 'bids': []}}}


g = dict(U.genoma_inicial(0, random.Random(1)), estrategia='valor', margen=0.05, desde=0.0, hasta=0.99, precio_max=0.95, sigma_mult=1.0)
p = U.p_subida(100.2, 100, 0.0008, 300)
ok(U.decidir(g, merc(100.2, 100, 0.55, 0.47))[0] == 'Up', f'valor: compra Up barato (modelo {p:.2f} frente a 0,55)')
ok(U.decidir(g, merc(100.2, 100, round(p, 2), 0.2)) is None, 'valor: sin ventaja tras la comisión, no apuesta')
ok(U.decidir(dict(g, desde=0.8), merc(100.2, 100, 0.55, 0.47)) is None, 'fuera de su ventana de entrada no apuesta')
gm = dict(g, estrategia='momento', umbral_mov=0.001)
ok(U.decidir(gm, merc(99.8, 100, 0.2, 0.8))[0] == 'Down' and U.decidir(gm, merc(100.01, 100, 0.5, 0.5)) is None, 'momento: sigue al que gana si se ha movido lo bastante')
gc = dict(g, estrategia='contra', precio_contra=0.25)
ok(U.decidir(gc, merc(99.8, 100, 0.2, 0.8))[0] == 'Up' and U.decidir(gc, merc(100, 100, 0.5, 0.5)) is None, 'contra: compra el lado barato')

# 4) ciclo completo con Polymarket y Coinbase simulados
AHORA = int(time.time())
INI = AHORA // 900 * 900
RESUELTOS = {}


def get_falso(url, cuerpo=None, timeout=12):
    if url.startswith(U.GAMMA):
        slug = url.split('slug=')[1]
        act, _, marco, ini = slug.split('-')
        m = {'outcomes': '["Up", "Down"]', 'clobTokenIds': json.dumps([f'{slug}-up', f'{slug}-down']), 'closed': slug in RESUELTOS,
             'outcomePrices': json.dumps(['1', '0'] if RESUELTOS.get(slug) == 'Up' else ['0', '1'] if slug in RESUELTOS else ['0.5', '0.5'])}
        return [{'title': slug, 'markets': [m]}]
    if url.endswith('/books'):  # Up siempre infravalorado: 0,40 cuando el precio va por encima del inicio
        return [{'asset_id': x['token_id'], 'bids': [{'price': '0.38', 'size': '100'}],
                 'asks': [{'price': '0.40' if x['token_id'].endswith('-up') else '0.62', 'size': '1000'}]} for x in cuerpo]
    raise AssertionError(url)


U._get = get_falso
datos.velas = lambda s, i='1m', **k: [{'t': INI - 3600 + 60 * j, 'o': 100.0, 'h': 100.1, 'l': 99.9, 'c': 100.0 + (j % 3 - 1) * 0.05} for j in range(61)] + \
    [{'t': INI + 60 * j, 'o': 100.0, 'h': 100.3, 'l': 99.9, 'c': 100.2} for j in range(15)]
datos.cotizacion = lambda s: {'precio': 100.25}
U.MARCOS = {'15m': 900}
U.arrancar_hilo = lambda: None  # sin hilo: los pasos se dan a mano
U.iniciar(8, 5.0)
for a in U.agentes():  # todos con valor, ventana completa y solo BTC para que el ciclo sea determinista
    gg = dict(U._g(a), estrategia='valor', desde=0.0, hasta=0.99, margen=0.05, precio_max=0.95, sigma_mult=1.0, activos=['btc'], marcos=['15m'], tamano_pct=20)
    db.ex('UPDATE pm_agentes SET genoma=? WHERE id=?', (json.dumps(gg), a['id']))
U.paso()
aps = db.q('SELECT * FROM pm_apuestas')
ok(len(aps) == 8 and all(x['lado'] == 'Up' and abs(x['precio'] - 0.40) < 1e-9 for x in aps), f'cada apostador compra Up a 0,40 ({len(aps)} apuestas)')
ok(all(abs(x['coste'] - 1.0) < 1e-9 and abs(x['comision'] - U.comision(x['acciones'], 0.4)) < 1e-12 for x in aps), 'apuesta de 1 $ (20 % de 5 $, mínimo 1 $) con su comisión')
U.paso()
ok(len(db.q('SELECT * FROM pm_apuestas')) == 8, 'no repite apuesta en el mismo mercado')
db.ex('UPDATE pm_apuestas SET fin=?', (AHORA - 60,))
U.resolver()
ok(db.uno('SELECT COUNT(*) n FROM pm_apuestas WHERE resultado IS NULL')['n'] == 8, 'sin resolución oficial, no se cobra')
RESUELTOS[aps[0]['slug']] = 'Up'
U.resolver()
a0 = U.agentes()[0]
esperado = 5.0 - 1.0 - aps[0]['comision'] + aps[0]['acciones']
ok(abs(a0['capital'] - esperado) < 1e-9 and a0['ganadas'] == 1, f"gana: cobra 1 $ por acción ({a0['capital']:.3f} $)")
ok(abs(a0['pnl'] - (aps[0]['acciones'] - 1.0 - aps[0]['comision'])) < 1e-9, 'pnl = pago − coste − comisión')

# 5) quiebra y evolución
db.ex('UPDATE pm_agentes SET capital=0.3 WHERE id=?', (a0['id'],))
U.resolver()
a0 = db.uno('SELECT * FROM pm_agentes WHERE id=?', (a0['id'],))
ok(a0['quiebras'] == 1 and a0['capital'] == 5.0 and a0['generacion'] == 1, 'quiebra: renace con 5 $ y el sistema mutado de uno de los mejores')
for i, a in enumerate(U.agentes()):
    db.ex('UPDATE pm_agentes SET apuestas=10, capital=? WHERE id=?', (1 + i, a['id']))
peor = U.agentes()[0]['id']
U.evolucionar()
ok(db.uno('SELECT generacion FROM pm_agentes WHERE id=?', (peor,))['generacion'] >= 1 and db.q('SELECT * FROM pm_lecciones'), 'evolución: el 25 % peor se sustituye y queda la lección')

# 6) informe
inf = U.informe()
ok('necesita >' in inf and 'valor' in inf, 'el informe dice el acierto necesario para no perder')
print(inf)

if os.environ.get('LIVE'):
    import importlib
    importlib.reload(datos)
    U.datos = datos
    U._get = get_real = importlib.reload(U)._get
    ctx = U.contexto()
    ok(bool(ctx), f'API real: {len(ctx)} mercados Up/Down en vivo')
    for m in ctx[:3]:
        print(f"  {m['slug']}: inicio {m['K']:.4f} ahora {m['S']:.4f} modelo P(up)={m['p_up']:.2f} · Up {m['libros']['Up']['asks'][:1]} Down {m['libros']['Down']['asks'][:1]} · {m['segs']:.0f} s")

print('\n' + ('TODO OK' if not fallos else f'{len(fallos)} FALLOS: ' + '; '.join(fallos)))
sys.exit(1 if fallos else 0)
