"""LABORATORIO DE MODELOS para el piloto: cada 10 min pregunta a VARIOS modelos lo mismo («¿entrar o vetar?») sobre las mejores
monedas del último escaneo, con los mismos datos, y una hora después mira qué hizo el precio simulando la operación que habría
hecho el piloto (compra al precio de la pregunta, TP = 2×SL, SL según el ATR, costes 6 % ida y vuelta). Así se elige el
modelo con datos y no a ojo. No opera: solo anota. Tabla ia_lab.

Segunda estrategia (estrategia='smart'): cada 10 min mira qué compran a la vez varias wallets smart money / KOL en la última hora,
pasa el filtro anti-rug y anota la señal como entrada a mercado; los modelos opinan igual y se puntúa igual. Así se ve si seguir
al smart money gana antes de arriesgar dinero."""
import collections, os, threading, time
from . import db, llm, onchain

CADA, HORIZONTE, COSTES = 600, 3600, 6.0
MODELOS = [m for m in os.environ.get('DMN_TRADER_LAB', 'gemma4:31b-cloud,gpt-oss:120b-cloud,gpt-oss:20b,qwen3:14b').split(',') if m]
ESQUEMA = """
CREATE TABLE IF NOT EXISTS ia_lab (id INTEGER PRIMARY KEY, ts INTEGER, token TEXT, simbolo TEXT, pts INTEGER, atr REAL, modelo TEXT,
  decision TEXT, confianza REAL, motivo TEXT, precio REAL, ms INTEGER, resultado REAL, evaluado INTEGER);
CREATE INDEX IF NOT EXISTS ix_ia_lab ON ia_lab(evaluado, ts);
"""
SM = {'compradores_min': 2, 'neto_min_usd': 300, 'liq_min_nuevo': 10_000, 'liq_min': 50_000, 'top10_max': 0.4, 'bundlers_max': 0.3, 'edad_min_h': 1, 'max_ronda': 3, 'repetir_h': 2}
_senales = collections.deque(maxlen=20)
_feed = {}  # tx → (ts, token, ±usd, wallet, símbolo); el feed de GMGN solo trae los últimos 2-6 min, se acumula cada minuto
NO_MEME = {'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB'}
_hilo = {'t': None}


def preparar():
    c = db.con()
    c.executescript(ESQUEMA)
    if 'estrategia' not in {r[1] for r in c.execute('PRAGMA table_info(ia_lab)')}:
        c.execute("ALTER TABLE ia_lab ADD COLUMN estrategia TEXT DEFAULT 'retroceso'")
        c.commit()


def _instalados():
    import json, urllib.request
    try:
        with urllib.request.urlopen(llm.OLLAMA + '/api/tags', timeout=3) as r:
            hay = {m['name'] for m in json.loads(r.read())['models']}
    except Exception:
        return []
    return [m for m in MODELOS if m in hay or m + ':latest' in hay]


def preguntar_todos(cand, estrategia='retroceso'):
    from . import piloto
    ctx = piloto._contexto(cand['token'], cand['simbolo'])
    sl = max(8, min(2.5 * cand['atr'], 18))
    if estrategia.startswith('smart'):
        propuesta = (f"COMPRAR YA a mercado porque {cand['pts']} wallets smart money/KOL distintas lo compraron en la última hora "
                     f"(neto +{cand['neto']:.0f} $)")
    else:
        propuesta = f"COMPRAR en retroceso ({cand['pts']}/9 puntos)"
    texto = (f"{piloto.REGLAS_IA}\nEl escáner propone {propuesta}, ATR15 {cand['atr']} %, "
             f"TP +{2 * sl:.0f} %, SL −{sl:.0f} %.\nDatos: {ctx}\n"
             'Contesta {"decision":"entrar"|"vetar","confianza":0-1,"motivo":"una frase en español"}')
    for m in _instalados() or ['-']:  # sin modelos se anota igual la señal, para la referencia
        t0 = time.time()
        if m == '-':
            r = {'decision': 'error', 'motivo': 'sin modelos'}
        else:
          try:
            r = llm.preguntar(texto, timeout=180, modelo=m)
          except Exception as ex:
            r = {'decision': 'error', 'motivo': str(ex)[:120]}
        dec = str(r.get('decision', '')).lower()
        dec = 'entrar' if dec.startswith('ent') else 'vetar' if dec.startswith('vet') else 'error'
        try:
            conf = float(r.get('confianza') or 0)
        except (TypeError, ValueError):
            conf = 0.0
        db.ex('INSERT INTO ia_lab(ts,token,simbolo,pts,atr,modelo,decision,confianza,motivo,precio,ms,evaluado,estrategia) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?)',
              (int(t0), cand['token'], cand['simbolo'], cand['pts'], cand['atr'], m, dec, conf, str(r.get('motivo') or '')[:200],
               ctx['precio'], int((time.time() - t0) * 1000), estrategia))


def senales_smart():
    """Tokens que compran a la vez ≥2 wallets smart money/KOL en la última hora (neto positivo) y pasan el filtro anti-rug."""
    ag = collections.defaultdict(lambda: {'neto': 0.0, 'compr': set(), 'sym': ''})
    for ts, tok, usd, w, sim in list(_feed.values()):
        if time.time() - ts > 3600 or tok in NO_MEME:
            continue
        a = ag[tok]
        a['neto'] += usd
        if usd > 0:
            a['compr'].add(w)
        a['sym'] = sim or a['sym']
    recientes = {r['token'] for r in db.q("SELECT DISTINCT token FROM ia_lab WHERE estrategia LIKE 'smart%' AND ts>?", (int(time.time()) - SM['repetir_h'] * 3600,))}
    out = []
    for tok, a in sorted(ag.items(), key=lambda kv: -(len(kv[1]['compr']) * 1e6 + kv[1]['neto'])):
        if len(out) >= SM['max_ronda'] or len(a['compr']) < SM['compradores_min']:
            break
        if a['neto'] < SM['neto_min_usd'] or tok in recientes:
            continue
        try:
            t = onchain._api('/v1/token/info', chain='sol', address=tok)
            st = t.get('stat') or {}
            liq = float(t.get('liquidity') or 0)
            if (liq < SM['liq_min_nuevo'] or float(st.get('top_10_holder_rate') or 1) > SM['top10_max']
                    or float(st.get('top_bundler_trader_percentage') or 0) > SM['bundlers_max']):
                continue
            # estricta = moneda ya asentada; si no, es un lanzamiento recién salido (lo que más francotira el smart money)
            asentada = liq >= SM['liq_min'] and time.time() - (t.get('open_timestamp') or time.time()) >= SM['edad_min_h'] * 3600
            sim = t.get('symbol') or a['sym']
            s_ = onchain.registrar('SOL', tok, sim, t.get('name'))
            v = onchain.velas(s_, '15m')[-16:]
            k = 1.0
            if len(v) < 8:
                v, k = onchain.velas(s_, '5m')[-16:], 1.7  # ATR de 5 min llevado a 15 min (≈√3)
            if len(v) < 4:
                continue
            atr = round(sum((x['h'] - x['l']) / x['c'] for x in v) / len(v) * 100 * k, 2)
            out.append({'token': tok, 'simbolo': sim, 'pts': len(a['compr']), 'neto': round(a['neto']), 'atr': atr, 'ts': int(time.time()),
                        'estrategia': 'smart' if asentada else 'smart_nuevo'})
        except Exception:
            continue
    return out


def _simular(f):
    """Resultado neto (%) de la operación que habría hecho el piloto desde la pregunta: TP/SL sobre velas de 5 min."""
    s = onchain.registrar('SOL', f['token'], f['simbolo'])
    v = [x for x in onchain.velas(s, '5m', inicio=f['ts'], fin=f['ts'] + HORIZONTE) if f['ts'] - 300 < x['t'] <= f['ts'] + HORIZONTE]
    if not v:
        return None
    sl = max(8, min(2.5 * f['atr'], 18))
    tp_p, sl_p = f['precio'] * (1 + 2 * sl / 100), f['precio'] * (1 - sl / 100)
    for x in v:
        if x['l'] <= sl_p:          # si una vela toca los dos, se asume lo peor (stop primero)
            return -sl - COSTES
        if x['h'] >= tp_p:
            return 2 * sl - COSTES
    return (v[-1]['c'] / f['precio'] - 1) * 100 - COSTES


def evaluar():
    hechos = {}
    for f in db.q('SELECT * FROM ia_lab WHERE evaluado=0 AND ts<? ORDER BY ts LIMIT 60', (int(time.time()) - HORIZONTE - 300,)):
        f = dict(f)
        k = (f['token'], f['ts'] // 60)
        if k not in hechos:
            try:
                hechos[k] = _simular(f)
            except Exception:
                hechos[k] = None
        db.ex('UPDATE ia_lab SET resultado=?, evaluado=1 WHERE id=?', (hechos[k], f['id']))


def acumular_feed():
    for ruta in ('/v1/user/smartmoney', '/v1/user/kol'):
        d = onchain._api(ruta, chain='sol', limit=200)
        for x in ((d or {}).get('list') if isinstance(d, dict) else d) or []:
            try:
                _feed[x['transaction_hash']] = (int(x['timestamp']), x['base_address'], float(x['amount_usd']) * (1 if x['side'] == 'buy' else -1),
                                                x.get('maker'), (x.get('base_token') or {}).get('symbol') or '')
            except (KeyError, TypeError, ValueError):
                pass
    for k in [k for k, v in _feed.items() if time.time() - v[0] > 3600]:
        del _feed[k]


def _bucle_feed():
    while True:
        try:
            acumular_feed()
        except Exception:
            pass
        time.sleep(60)


def informe(estrategia='retroceso'):
    """Por modelo: cuántas veces aprueba, qué habría ganado siguiéndolo y si acierta más que entrar en todo (la referencia)."""
    filas = [dict(r) for r in db.q("SELECT * FROM ia_lab WHERE evaluado=1 AND resultado IS NOT NULL AND COALESCE(estrategia,'retroceso')=?", (estrategia,))]
    ref = sum(f['resultado'] for f in {(f['token'], f['ts'] // 60): f for f in filas}.values())
    casos = {(f['token'], f['ts'] // 60): f for f in filas}.values()
    n_ref = len(casos)
    out = []
    for m in sorted({f['modelo'] for f in filas} - {'-'}):
        fm = [f for f in filas if f['modelo'] == m and f['decision'] != 'error']
        ent = [f for f in fm if f['decision'] == 'entrar']
        acierto = sum(1 for f in fm if (f['decision'] == 'entrar') == (f['resultado'] > 0))
        out.append({'modelo': m, 'preguntas': len(fm), 'aprueba_pct': round(100 * len(ent) / len(fm), 1) if fm else None,
                    'resultado_siguiendolo_pct': round(sum(f['resultado'] for f in ent), 2), 'medio_por_entrada_pct': round(sum(f['resultado'] for f in ent) / len(ent), 2) if ent else None,
                    'acierto_pct': round(100 * acierto / len(fm), 1) if fm else None,
                    'ms_medio': round(sum(f['ms'] for f in fm) / len(fm)) if fm else None,
                    'errores': sum(1 for f in filas if f['modelo'] == m and f['decision'] == 'error')})
    pend = (db.uno("SELECT COUNT(*) n FROM ia_lab WHERE evaluado=0 AND COALESCE(estrategia,'retroceso')=?", (estrategia,)) or {'n': 0})['n']
    r = {'modelos': out, 'referencia_entrar_en_todo_pct': round(ref, 2), 'casos': n_ref, 'pendientes': pend, 'instalados': _instalados(),
         'ganadoras_pct': round(100 * sum(1 for f in casos if f['resultado'] > 0) / n_ref) if n_ref else None}
    if estrategia == 'retroceso':
        r['smart'] = {**informe('smart'), 'senales': list(_senales)[::-1]}
        r['smart_nuevo'] = informe('smart_nuevo')
    return r


def _bucle():
    from . import piloto
    visto = 0
    while True:
        try:
            evaluar()
        except Exception:
            pass
        try:
            for cand in senales_smart():
                _senales.append(cand)
                preguntar_todos(cand, cand['estrategia'])
        except Exception:
            pass
        try:
            sc = (piloto._est().get('ultimo_scan') or {})
            if sc.get('ts', 0) > visto and time.time() - sc['ts'] < 900:
                visto = sc['ts']
                for cand in (sc.get('top') or [])[:2]:
                    preguntar_todos(cand)
        except Exception:
            pass
        time.sleep(CADA)


def arrancar():
    preparar()
    if not (_hilo['t'] and _hilo['t'].is_alive()):
        _hilo['t'] = threading.Thread(target=_bucle, daemon=True)
        _hilo['t'].start()
        threading.Thread(target=_bucle_feed, daemon=True).start()
