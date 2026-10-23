"""LABORATORIO DE MODELOS para el piloto: cada 10 min pregunta a VARIOS modelos lo mismo («¿entrar o vetar?») sobre las mejores
monedas del último escaneo, con los mismos datos, y una hora después mira qué hizo el precio simulando la operación que habría
hecho el piloto (compra al precio de la pregunta, TP = 2×SL, SL según el ATR, costes 6 % ida y vuelta). Así se elige el
modelo con datos y no a ojo. No opera: solo anota. Tabla ia_lab."""
import os, threading, time
from . import db, llm, onchain

CADA, HORIZONTE, COSTES = 600, 3600, 6.0
MODELOS = [m for m in os.environ.get('DMN_TRADER_LAB', 'gemma4:31b-cloud,gpt-oss:120b-cloud,gpt-oss:20b,qwen3:14b').split(',') if m]
ESQUEMA = """
CREATE TABLE IF NOT EXISTS ia_lab (id INTEGER PRIMARY KEY, ts INTEGER, token TEXT, simbolo TEXT, pts INTEGER, atr REAL, modelo TEXT,
  decision TEXT, confianza REAL, motivo TEXT, precio REAL, ms INTEGER, resultado REAL, evaluado INTEGER);
CREATE INDEX IF NOT EXISTS ix_ia_lab ON ia_lab(evaluado, ts);
"""
_hilo = {'t': None}


def preparar():
    db.con().executescript(ESQUEMA)


def _instalados():
    import json, urllib.request
    try:
        with urllib.request.urlopen(llm.OLLAMA + '/api/tags', timeout=3) as r:
            hay = {m['name'] for m in json.loads(r.read())['models']}
    except Exception:
        return []
    return [m for m in MODELOS if m in hay or m + ':latest' in hay]


def preguntar_todos(cand):
    from . import piloto
    ctx = piloto._contexto(cand['token'], cand['simbolo'])
    sl = max(8, min(2.5 * cand['atr'], 18))
    texto = (f"{piloto.REGLAS_IA}\nEl escáner propone COMPRAR en retroceso ({cand['pts']}/9 puntos, ATR15 {cand['atr']} %, "
             f"TP +{2 * sl:.0f} %, SL −{sl:.0f} %).\nDatos: {ctx}\n"
             'Contesta {"decision":"entrar"|"vetar","confianza":0-1,"motivo":"una frase en español"}')
    for m in _instalados():
        t0 = time.time()
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
        db.ex('INSERT INTO ia_lab(ts,token,simbolo,pts,atr,modelo,decision,confianza,motivo,precio,ms,evaluado) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)',
              (int(t0), cand['token'], cand['simbolo'], cand['pts'], cand['atr'], m, dec, conf, str(r.get('motivo') or '')[:200],
               ctx['precio'], int((time.time() - t0) * 1000)))


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


def informe():
    """Por modelo: cuántas veces aprueba, qué habría ganado siguiéndolo y si acierta más que entrar en todo (la referencia)."""
    filas = [dict(r) for r in db.q('SELECT * FROM ia_lab WHERE evaluado=1 AND resultado IS NOT NULL')]
    ref = sum(f['resultado'] for f in {(f['token'], f['ts'] // 60): f for f in filas}.values())
    n_ref = len({(f['token'], f['ts'] // 60) for f in filas})
    out = []
    for m in sorted({f['modelo'] for f in filas}):
        fm = [f for f in filas if f['modelo'] == m and f['decision'] != 'error']
        ent = [f for f in fm if f['decision'] == 'entrar']
        acierto = sum(1 for f in fm if (f['decision'] == 'entrar') == (f['resultado'] > 0))
        out.append({'modelo': m, 'preguntas': len(fm), 'aprueba_pct': round(100 * len(ent) / len(fm), 1) if fm else None,
                    'resultado_siguiendolo_pct': round(sum(f['resultado'] for f in ent), 2), 'medio_por_entrada_pct': round(sum(f['resultado'] for f in ent) / len(ent), 2) if ent else None,
                    'acierto_pct': round(100 * acierto / len(fm), 1) if fm else None,
                    'ms_medio': round(sum(f['ms'] for f in fm) / len(fm)) if fm else None,
                    'errores': sum(1 for f in filas if f['modelo'] == m and f['decision'] == 'error')})
    pend = (db.uno('SELECT COUNT(*) n FROM ia_lab WHERE evaluado=0') or {'n': 0})['n']
    return {'modelos': out, 'referencia_entrar_en_todo_pct': round(ref, 2), 'casos': n_ref, 'pendientes': pend, 'instalados': _instalados()}


def _bucle():
    from . import piloto
    visto = 0
    while True:
        try:
            evaluar()
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
