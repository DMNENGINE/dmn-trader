"""CONCIENCIA DEL TRADER: grafo de memoria propio + línea de progreso, enganchado a la memoria principal de APOLO.

Todo lo que aprende queda grabado (SQLite, tablas con_*):
- VELAS: cada lote de muestras que la fábrica completa (qué moneda, cuántas velas, qué pasó en las 2 h siguientes).
- RONDAS: cada evolución de la fábrica (qué estrategias simuló, con qué resultado y en qué monedas operó en la simulación).
- PROMOCIONES y OPERACIONES REALES del piloto.
Nodos (ronda, estrategia, moneda, operación) y aristas (simuló, operó, aprendió, sigue_a) forman el grafo; cada ronda enlaza
con la anterior, así que el grafo ES la línea de progreso. El "nivel" (0-100) resume cuánto sabe: datos vistos, exploración y
ventaja demostrada (fuera de muestra y en vivo; la ventaja pesa lo que más porque es lo único que da dinero).

Memoria principal: los hitos (récord, promoción, resumen diario) y un recuerdo vivo con el estado se mandan a APOLO
(POST /v1/memoria del daemon local); su grafo los enlaza con la entidad «DMN Trader». Si APOLO no está, quedan pendientes."""
import json, math, os, threading, time, urllib.request
from . import db

ESQUEMA = """
CREATE TABLE IF NOT EXISTS con_eventos (id INTEGER PRIMARY KEY, ts INTEGER, tipo TEXT, titulo TEXT, datos TEXT, nivel REAL,
  apolo TEXT);
CREATE INDEX IF NOT EXISTS ix_con_ev ON con_eventos(tipo, ts);
CREATE TABLE IF NOT EXISTS con_nodos (id TEXT PRIMARY KEY, tipo TEXT, nombre TEXT, datos TEXT, creado INTEGER, actualizado INTEGER,
  veces INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS con_aristas (de TEXT, a TEXT, tipo TEXT, peso REAL DEFAULT 0, datos TEXT, ts INTEGER,
  PRIMARY KEY(de, a, tipo));
"""
APOLO = os.environ.get('DMN_APOLO_URL', 'http://127.0.0.1:47900')
TOKEN_APOLO = os.environ.get('DMN_APOLO_TOKEN_FILE', os.path.join(os.environ.get('APPDATA', ''), 'robot-companion', 'nucleo', 'token'))
_lock = threading.Lock()
_listo = {'ok': False}


def preparar():
    if not _listo['ok']:
        db.con().executescript(ESQUEMA)
        _listo['ok'] = True

# ---------------- grafo ----------------


def nodo(id_, tipo, nombre, datos=None):
    ahora = int(time.time())
    db.ex('INSERT INTO con_nodos(id,tipo,nombre,datos,creado,actualizado,veces) VALUES(?,?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET '
          'nombre=excluded.nombre, datos=COALESCE(excluded.datos, con_nodos.datos), actualizado=excluded.actualizado, veces=con_nodos.veces+1',
          (id_, tipo, nombre, json.dumps(datos) if datos is not None else None, ahora, ahora))
    return id_


def arista(de, a, tipo, peso=1.0, datos=None):
    db.ex('INSERT INTO con_aristas(de,a,tipo,peso,datos,ts) VALUES(?,?,?,?,?,?) ON CONFLICT(de,a,tipo) DO UPDATE SET '
          'peso=con_aristas.peso+excluded.peso, datos=COALESCE(excluded.datos, con_aristas.datos), ts=excluded.ts',
          (de, a, tipo, peso, json.dumps(datos) if datos is not None else None, int(time.time())))


MERCADO = "COALESCE(json_extract(datos,'$.mercado'),'memes')"


def _nivel_actual(mercado='memes'):
    r = db.uno(f"SELECT nivel FROM con_eventos WHERE tipo='ronda' AND {MERCADO}=? ORDER BY id DESC LIMIT 1", (mercado,))
    return r['nivel'] if r else 0.0


def evento(tipo, titulo, datos=None, nivel=None, apolo=False):
    """apolo=True: es un hito que también va a la memoria principal (queda 'pendiente' hasta que APOLO lo acepte)."""
    preparar()
    return db.ex('INSERT INTO con_eventos(ts,tipo,titulo,datos,nivel,apolo) VALUES(?,?,?,?,?,?)',
                 (int(time.time()), tipo, titulo, json.dumps(datos or {}), nivel if nivel is not None else _nivel_actual(),
                  'pendiente' if apolo else None))


def calcular_nivel(muestras, probadas, mejor_prueba, mejor_vivo, escala=5.0):
    """0-100. Datos: 25 (llega al máximo con 100 000 muestras). Exploración: 15 (50 000 estrategias simuladas).
    Ventaja: 60 = 25 fuera de muestra + 35 en vivo, llenos con +escala por operación (memes 5 %, futuros 20 $/contrato).
    Sin ventaja nunca pasa de 40."""
    datos = 25 * min(1, math.log10(max(muestras, 1)) / 5)
    expl = 15 * min(1, math.log10(max(probadas, 1)) / math.log10(50_000))
    v1 = 25 * min(1, max(0, mejor_prueba or 0) / escala)
    v2 = 35 * min(1, max(0, mejor_vivo or 0) / escala)
    return round(datos + expl + v1 + v2, 1)

# ---------------- ganchos (los llama la fábrica y el piloto) ----------------


def velas(lote):
    """lote = [{token, simbolo, n, max_med, cierre_med}] de una pasada de completar(): lo que la fábrica acaba de aprender."""
    if not lote:
        return
    preparar()
    n = sum(x['n'] for x in lote)
    eid = evento('velas', f"Aprendió {n} momentos de {len(lote)} monedas (2 h de velas de 5 min después de cada uno)",
                 {'monedas': lote[:40], 'n': n})
    ev = nodo(f'velas:{eid}', 'aprendizaje', f'{n} muestras', {'n': n})
    for x in lote:
        m = nodo(f"moneda:{x['token']}", 'moneda', x['simbolo'] or x['token'][:6])
        arista(ev, m, 'aprendió', x['n'], {'max_med': x['max_med'], 'cierre_med': x['cierre_med']})
    total = db.uno("SELECT COALESCE(SUM(json_extract(datos,'$.n')),0) s FROM con_eventos WHERE tipo='velas'")['s']
    db.poner_ajuste('con_velas_total', total)


def ronda(r):
    """r = {gen, muestras, vivas, tokens, probadas, pasan, top:[{id, regla, nombre?, entreno, prueba, vivo, ops:[{token, simbolo, ts, res}]}]}"""
    preparar()
    m = r.get('mercado', 'memes')
    suf = '' if m == 'memes' else '_' + m
    ud = ' %' if m == 'memes' else ' $'
    pre = '' if m == 'memes' else m.capitalize() + ' · '
    probadas = (db.ajuste('con_probadas' + suf) or 0) + r['probadas']
    db.poner_ajuste('con_probadas' + suf, probadas)
    mejor_p = max([t['prueba']['medio'] for t in r['top'] if t['prueba']['n'] >= 15 and t['prueba']['medio'] is not None] or [None], key=lambda x: x if x is not None else -1e9)
    mejor_v = max([t['vivo']['medio'] for t in r['top'] if t['vivo']['n'] >= 10 and t['vivo']['medio'] is not None] or [None], key=lambda x: x if x is not None else -1e9)
    nivel = calcular_nivel(r['muestras'], probadas, mejor_p, mejor_v, r.get('escala', 5.0))
    previo = db.uno(f"SELECT id, datos FROM con_eventos WHERE tipo='ronda' AND {MERCADO}=? ORDER BY id DESC LIMIT 1", (m,))
    titulo = (f"{pre}Ronda {r['gen']}: {r['muestras']} muestras, {r['probadas']} estrategias simuladas, {r['pasan']} pasan la validación"
              + (f"; mejor fuera de muestra {mejor_p:+.1f}{ud}/op" if mejor_p is not None else ''))
    datos = {k: r[k] for k in ('gen', 'muestras', 'vivas', 'tokens', 'probadas', 'pasan')}
    datos['mercado'] = m
    datos.update(mejor_prueba=mejor_p, mejor_vivo=mejor_v, probadas_total=probadas,
                 top=[{k: t.get(k) for k in ('id', 'nombre', 'regla', 'entreno', 'prueba', 'vivo')} for t in r['top']])
    eid = evento('ronda', titulo, datos, nivel)
    rn = nodo(f'ronda:{eid}', 'ronda', f"{pre}Ronda {r['gen']}", {'nivel': nivel, 'pasan': r['pasan'], 'mejor_prueba': mejor_p})
    if previo:
        arista(rn, f"ronda:{previo['id']}", 'sigue_a', 1, {'nivel_antes': json.loads(previo['datos']).get('nivel')})
    for t in r['top']:
        en = nodo(f"estrategia:{t['id']}", 'estrategia', t.get('nombre') or f"Estrategia {t['id']}", {'regla': t['regla']})
        arista(rn, en, 'simuló', 1, {'entreno': t['entreno'], 'prueba': t['prueba'], 'vivo': t['vivo']})
        porm = {}
        for o in t.get('ops') or []:
            p = porm.setdefault(o['token'], {'simbolo': o['simbolo'], 'n': 0, 's': 0.0})
            p['n'] += 1
            p['s'] += o['res']
        for tok, p in porm.items():
            arista(en, nodo(f'moneda:{tok}', 'moneda' if m == 'memes' else 'futuro', p['simbolo'] or tok[:6]), 'operó', p['n'], {'medio': round(p['s'] / p['n'], 2)})
    # hitos para la memoria principal
    record = db.ajuste('con_record_prueba' + suf)
    if mejor_p is not None and mejor_p > 0 and (record is None or mejor_p > record + 0.1 * r.get('escala', 5.0)):
        db.poner_ajuste('con_record_prueba' + suf, mejor_p)
        t = next(t for t in r['top'] if t['prueba']['medio'] == mejor_p)
        evento('record', f"DMN Trader (fábrica de {'estrategias' if m == 'memes' else m}): nuevo récord fuera de muestra, {mejor_p:+.1f}{ud} por operación en "
               f"{t['prueba']['n']} operaciones simuladas con la regla «{t['regla']}». Aún no es dinero real: falta probarla en vivo.",
               {'estrategia': t['id']}, nivel, apolo=True)
    if m == 'memes':
        _estado_apolo(nivel, r, mejor_p, mejor_v)
        _diario()
    sincronizar()
    return nivel


def promocion(e):
    preparar()
    v = e['vivo']
    en = nodo(f"estrategia:{e['id']}", 'estrategia', e.get('nombre') or e['id'], {'regla': e.get('regla')})
    arista(en, nodo('piloto', 'sistema', 'Piloto real'), 'promovida', 1, {'vivo': v})
    evento('promocion', f"DMN Trader: la estrategia «{e.get('nombre') or e['id']}» pasó a dinero real tras ganar en vivo "
           f"{v['medio']:+.1f} % por operación en {v['n']} operaciones ({v['gana_pct']} % ganadoras).", {'id': e['id'], 'vivo': v}, apolo=True)
    sincronizar()


def operacion_real(op):
    preparar()
    oid = nodo(f"real:{op.get('id') or op.get('cerrada')}", 'operación', f"{op.get('simbolo')} {op.get('resultado')}",
               {k: op.get(k) for k in ('simbolo', 'resultado', 'pnl_sol', 'pnl_pct', 'precio_entrada', 'estrategia', 'cerrada')})
    arista(oid, nodo(f"moneda:{op.get('token')}", 'moneda', op.get('simbolo') or '?'), 'en', 1)
    if op.get('est_id'):
        arista(nodo(f"estrategia:{op['est_id']}", 'estrategia', op.get('estrategia') or op['est_id']), oid, 'ejecutó', 1)
    evento('real', f"Operación real {op.get('simbolo')}: {op.get('resultado')} · {op.get('pnl_sol', 0):+.4f} SOL"
           + (f" (estrategia «{op['estrategia']}»)" if op.get('estrategia') else ''), {'pnl_sol': op.get('pnl_sol'), 'resultado': op.get('resultado')})

# ---------------- memoria principal (APOLO) ----------------


def _apolo_post(cuerpo):
    tok = open(TOKEN_APOLO, encoding='utf-8').read().strip()
    rq = urllib.request.Request(APOLO + '/v1/memoria', data=json.dumps(cuerpo).encode(), method='POST',
                                headers={'content-type': 'application/json', 'x-robot-token': tok})
    with urllib.request.urlopen(rq, timeout=8) as r:
        return json.loads(r.read())


def _estado_apolo(nivel, r, mejor_p, mejor_v):
    """Un solo recuerdo vivo (se reescribe cada ronda) con el estado de la conciencia del trader."""
    vel = db.ajuste('con_velas_total') or 0
    reales = db.uno("SELECT COUNT(*) n, COALESCE(SUM(json_extract(datos,'$.pnl_sol')),0) s FROM con_eventos WHERE tipo='real'")
    texto = (f"DMN Trader · conciencia nivel {nivel:.0f}/100 (ronda {r['gen']}): la fábrica de estrategias lleva {vel} muestras aprendidas en vivo, "
             f"{db.ajuste('con_probadas') or 0} estrategias simuladas; ahora {r['pasan']} pasan la validación"
             + (f", mejor fuera de muestra {mejor_p:+.1f} %/op" if mejor_p is not None else '')
             + (f", mejor en vivo {mejor_v:+.1f} %/op" if mejor_v is not None else '')
             + f". Piloto real: {reales['n']} operaciones, {reales['s']:+.4f} SOL.")
    try:
        m = _apolo_post({'texto': texto, 'tipo': 'proyecto', 'reemplaza': db.ajuste('con_apolo_estado'), 'nuevo': not db.ajuste('con_apolo_estado')})
        db.poner_ajuste('con_apolo_estado', m.get('id'))
    except Exception:
        pass


def _diario():
    """Un hito al día con lo aprendido en las últimas 24 h."""
    hoy = time.strftime('%Y-%m-%d')
    if db.ajuste('con_diario') == hoy:
        return
    desde = int(time.time()) - 86400
    rs = db.q("SELECT datos, nivel FROM con_eventos WHERE tipo='ronda' AND ts>? ORDER BY id", (desde,))
    if len(rs) < 6:
        return
    v = db.uno("SELECT COALESCE(SUM(json_extract(datos,'$.n')),0) s FROM con_eventos WHERE tipo='velas' AND ts>?", (desde,))['s']
    re_ = db.uno("SELECT COUNT(*) n, COALESCE(SUM(json_extract(datos,'$.pnl_sol')),0) s FROM con_eventos WHERE tipo='real' AND ts>?", (desde,))
    ult = json.loads(rs[-1]['datos'])
    db.poner_ajuste('con_diario', hoy)
    evento('diario', f"DMN Trader, diario {hoy}: {len(rs)} rondas de entrenamiento, {v} muestras nuevas, nivel de conciencia de "
           f"{rs[0]['nivel']:.0f} a {rs[-1]['nivel']:.0f}/100; {ult.get('pasan', 0)} estrategias pasan la validación"
           + (f", {re_['n']} operaciones reales ({re_['s']:+.4f} SOL)" if re_['n'] else ', sin operaciones reales') + '.', {}, apolo=True)


def sincronizar():
    """Manda a APOLO los hitos pendientes (si el daemon no responde, se reintenta en la siguiente ronda)."""
    with _lock:
        for ev in db.q("SELECT id, titulo FROM con_eventos WHERE apolo='pendiente' ORDER BY id LIMIT 20"):
            try:
                m = _apolo_post({'texto': ev['titulo'][:600], 'tipo': 'hecho', 'nuevo': True})
                db.ex('UPDATE con_eventos SET apolo=? WHERE id=?', (m.get('id') or 'ok', ev['id']))
            except Exception:
                break

# ---------------- lectura ----------------


def rondas(mercado='memes'):
    """Cuántas rondas lleva (para seguir la numeración tras reiniciar el servidor)."""
    preparar()
    return db.uno(f"SELECT COUNT(*) n FROM con_eventos WHERE tipo='ronda' AND {MERCADO}=?", (mercado,))['n']


def enviar_grafo():
    """El grafo (resumido) a la central (Pi) para dibujarlo en su panel; lo llama el bucle del piloto cada 5 min."""
    from . import agente
    if not agente.CENTRAL:
        return
    rq = urllib.request.Request(agente.CENTRAL + '/api/conciencia/grafo', data=json.dumps(grafo(), default=str).encode(), method='POST',
                                headers={'content-type': 'application/json', **({'x-token': agente.CENTRAL_TOKEN} if agente.CENTRAL_TOKEN else {})})
    urllib.request.urlopen(rq, timeout=20).read()


def linea(limite=200, mercado='memes'):
    preparar()
    rs = db.q(f"SELECT ts, nivel, datos FROM con_eventos WHERE tipo='ronda' AND {MERCADO}=? ORDER BY id DESC LIMIT ?", (mercado, limite))[::-1]
    out = []
    for r in rs:
        d = json.loads(r['datos'])
        out.append({'ts': r['ts'], 'nivel': r['nivel'], 'muestras': d.get('muestras'), 'pasan': d.get('pasan'),
                    'mejor_prueba': d.get('mejor_prueba'), 'mejor_vivo': d.get('mejor_vivo')})
    return out


def resumen():
    preparar()
    ev = db.q("SELECT ts, tipo, titulo, nivel, apolo FROM con_eventos WHERE tipo!='velas' ORDER BY id DESC LIMIT 12")
    cnt = {r['tipo']: r['n'] for r in db.q('SELECT tipo, COUNT(*) n FROM con_nodos GROUP BY tipo')}
    return {'nivel': _nivel_actual(), 'linea': linea(150), 'nivel_futuros': _nivel_actual('futuros'), 'linea_futuros': linea(150, 'futuros'), 'eventos': ev, 'nodos': cnt,
            'aristas': db.uno('SELECT COUNT(*) n FROM con_aristas')['n'], 'velas_total': db.ajuste('con_velas_total') or 0,
            'apolo_pendientes': db.uno("SELECT COUNT(*) n FROM con_eventos WHERE apolo='pendiente'")['n']}


def grafo(max_nodos=300):
    preparar()
    nodos = db.q('SELECT id, tipo, nombre, veces FROM con_nodos ORDER BY (tipo=\'ronda\') DESC, veces DESC LIMIT ?', (max_nodos,))
    ids = {n['id'] for n in nodos}
    ar = [a for a in db.q('SELECT de, a, tipo, peso FROM con_aristas') if a['de'] in ids and a['a'] in ids]
    return {'nodos': nodos, 'aristas': ar}
