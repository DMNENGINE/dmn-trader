"""Escuadrón LLM y jefe de mesa (ideas de AI-Trader). Solo corre en el nodo que tiene el modelo de lenguaje (el PC).

- Escuadrón: N traders en los que gemma decide CADA operación (ve precios, indicadores, noticias, sus posiciones, sus últimas
  operaciones y sus lecciones). Usan la misma cuenta simulada, costes y gestión que los agentes evolutivos y compiten en el
  mismo marcador ('llm 15m' en el informe), así se ve con datos si la IA de lenguaje gana a la evolución.
- Jefe de mesa: cada 2 h lee el informe, escribe un análisis y propone cambios de genes; cada propuesta se prueba en el
  simulador con velas reales y solo se aplica si mejora."""
import copy, json, statistics, threading, time, traceback
from . import agente as A, datos, db, indicadores as ind, llm, motor, sentimiento

PERSONALIDADES = [
    ('Tendencias', 'Solo operas a favor de la tendencia clara de 15 minutos (EMA20 > EMA50 para comprar, al revés para vender). Paciente: mejor no operar que operar mal.'),
    ('Contrarian', 'Buscas excesos: RSI extremo y precio en los bordes del rango de 24 h para apostar por la vuelta a la media.'),
    ('Noticias', 'Te guías sobre todo por la nota de noticias y el Fear & Greed; el análisis técnico solo te da el momento de entrar.'),
    ('Rupturas', 'Entras cuando el precio rompe con fuerza el rango de las últimas horas, a favor de la ruptura.'),
    ('Mixto', 'Combinas tendencia, momento, noticias y costes; solo entras cuando varias cosas apuntan al mismo lado.'),
    ('Selectivo', 'Muy selectivo: solo la mejor oportunidad del mercado y solo si el objetivo paga de sobra comisión y spread.'),
]
N_LLM, LLM_CADA, JEFE_CADA = 6, 300, 7200
GEN_LLM = dict(A.GENOMA_BASE, estrategia='llm', marco='15m', max_barras=16, be_en_r=1.0, trail_r=1.0, trail_frac=0.8, riesgo_pct=10.0,
               guardia=3, enfriar=1, filtro_ia=0.0, entrada='mercado', tramos=1, roi_min=None)
GENES_JEFE = {'stop_atr': (0.6, 4.0), 'rr': (0.8, 5.0), 'min_ventaja': (1.0, 6.0), 'max_barras': (5, 120), 'be_en_r': (0.0, 2.0),
              'riesgo_pct': (3.0, 35.0), 'vol_max_pct': (0.05, 3.0), 'retroceso': (0.05, 1.5), 'tramos': (1, 3), 'roi_min': (0.0, 2.0),
              'trail_r': (0.0, 2.0), 'trail_frac': (0.3, 1.5), 'filtro_ia': (0.0, 0.65), 'enfriar': (0, 5), 'guardia': (0, 5),
              'filtro_tendencia': (False, True), 'marco': tuple(A.MARCOS), 'entrada': ('mercado', 'limite'), 'estrategia': tuple(A.ESTRATEGIAS)}
_ronda, _jefe = threading.Lock(), threading.Lock()
_disp = [0, False]


def activo():
    if time.time() - _disp[0] > 300:
        _disp[:] = [time.time(), llm.disponible()]
    return _disp[1]


def es_llm(a):
    return '"estrategia": "llm"' in (a['genoma'] or '')


def asegurar(e):
    """Crea los traders LLM que falten (solo aquí, en el nodo con modelo)."""
    vivos = [a for a in A.agentes() if es_llm(a)]
    usados = {A._g(a).get('personalidad') for a in vivos}
    for i in [k for k in range(N_LLM) if k not in usados][:max(0, N_LLM - len(vivos))]:
        a = A.crear_agente(e, dict(copy.deepcopy(GEN_LLM), personalidad=i))
        A._poner(a['id'], nombre=f"IA-{PERSONALIDADES[i % len(PERSONALIDADES)][0]}-{a['id']:03d}")
        A.log(f"🧠 nace el trader LLM IA-{PERSONALIDADES[i % len(PERSONALIDADES)][0]}")
    return [a for a in A.agentes() if es_llm(a)]


def mercado(e):
    """Tabla compacta del mercado real (velas de 15 m) para el prompt, y los números para ejecutar."""
    st, filas, num = sentimiento.actual(), [], {}
    for s in A._universo(e):
        try:
            q = datos.cotizacion(s)
            if not q['abierto']:
                continue
            v, v5 = datos.velas(s, '15m')[:-1], datos.velas(s, '5m')[:-1]
        except Exception:
            continue
        if len(v) < 60 or len(v5) < 14:
            continue
        c, h, l = [b['c'] for b in v], [b['h'] for b in v], [b['l'] for b in v]
        e20, e50, rsi, atr = ind.ema(c, 20)[-1], ind.ema(c, 50)[-1], ind.rsi(c, 14)[-1], ind.atr(h, l, c, 14)[-1]
        if None in (e20, e50, rsi, atr):
            continue
        p = q['precio']
        sp = ((q['ask'] - q['bid']) / p * 100) if q.get('ask') and q.get('bid') else 0.0
        coste = A.coste_pct(A.CUENTA_CFG, sp, q['tipo'])
        hi, lo = max(h[-96:]), min(l[-96:])
        sent = sentimiento.rasgos(s, st).get('sent')
        num[s] = {'precio': p, 'atr': atr, 'coste': coste, 'spread': sp, 'tend': 1 if e20 > e50 else -1}
        filas.append(f"{s}: precio {p:.6g} · 1h {(v5[-1]['c'] / v5[-13]['c'] - 1) * 100:+.2f}% · tendencia {'alcista' if e20 > e50 else 'bajista'} · "
                     f"RSI {rsi:.0f} · ATR {atr / p * 100:.2f}% · en el rango de 24h al {((p - lo) / (hi - lo) * 100) if hi > lo else 50:.0f}% · "
                     f"coste ida+vuelta {coste:.2f}%" + (f" · noticias {sent:+.1f}" if sent is not None else ''))
    return filas, num


def _prompt(e, a, g, filas):
    cid = a['cuenta_id']
    r = motor.resumen(cid)
    pos = motor.posiciones(cid)
    ult = db.q("""SELECT ag.simbolo, ag.direccion, ag.r, ag.causa FROM ag_trades ag WHERE ag.agente_id=? AND ag.r IS NOT NULL
                  AND COALESCE(ag.causa,'')<>'sistema' ORDER BY ag.trade_id DESC LIMIT 6""", (a['id'],))
    lec = db.q("SELECT texto FROM ag_lecciones WHERE agente_id=? AND tipo NOT IN ('ruido') ORDER BY id DESC LIMIT 4", (a['id'],))
    st = sentimiento.actual()
    nombre, estilo = PERSONALIDADES[g.get('personalidad', 0) % len(PERSONALIDADES)]
    return '\n'.join([
        f"Eres {a['nombre']}, trader de una simulación con precios REALES de mercado. Tu estilo ({nombre}): {estilo}",
        f"Capital: {r['equity']:.4f} $ (empezaste con {e['capital']} $; meta {e['meta']} $). Si bajas del 10 % quiebras.",
        'Reglas: stop = stop_atr × ATR de 15 min (stop_atr entre 1 y 4); objetivo = rr × distancia del stop (rr entre 1 y 4). '
        'El objetivo debe pagar con margen la comisión y el spread (columna coste). Como máximo UNA entrada nueva por turno; no hacer nada es válido. '
        f"Las posiciones se cierran solas por stop, objetivo, trailing desde +1R o a las {g['max_barras'] * 15 // 60} h.",
        f"Fear & Greed de cripto: {st['fng']['valor']} ({st['fng']['clase']})." if st.get('fng') else '',
        'Tus posiciones: ' + ('; '.join(f"{p['simbolo']} {'largo' if p['cantidad'] > 0 else 'corto'} desde {p['precio_medio']:.6g}, ahora {p['precio']:.6g}" for p in pos) or 'ninguna') + '.',
        'Tus últimas operaciones: ' + ('; '.join(f"{t['simbolo']} {t['direccion']} {t['r']:+.2f}R" + (f" ({t['causa']})" if t['causa'] else '') for t in ult) or 'ninguna aún') + '.',
        'Tus lecciones recientes: ' + (' | '.join(x['texto'][:160] for x in lec) or 'ninguna') + '.',
        'Mercado ahora (velas de 15 minutos):', *filas,
        'Responde SOLO con JSON: {"entrar": null o {"simbolo": "BTC-USD", "lado": "compra" o "venta", "stop_atr": 2, "rr": 2, "confianza": 0.0-1.0, '
        '"motivo": "una frase"}, "cerrar": ["SÍMBOLO"...], "pensamiento": "una frase sobre el mercado"}',
    ])


def decidir(e, a, filas, num):
    g, cid = A._g(a), a['cuenta_id']
    it = A._intento_actual(a['id'])
    if not it or A._PAUSA.get(a['id'], (0,))[0] > time.time():
        return
    resp = llm.preguntar(_prompt(e, a, g, filas), temperatura=0.3)
    pos = {p['simbolo']: p for p in motor.posiciones(cid, False)}
    for s in resp.get('cerrar') or []:
        if isinstance(s, str) and s.upper() in pos:
            try:
                motor.cerrar(cid, s.upper(), 100, 'decisión del LLM')
                pos.pop(s.upper())
            except motor.ErrorOrden:
                pass
    pensar = str(resp.get('pensamiento') or '')[:160]
    en = resp.get('entrar')
    if not isinstance(en, dict):
        A._poner(a['id'], ultimo=f"🧠 {pensar or 'espero'}")
        return
    sim = str(en.get('simbolo', '')).upper()
    lado = str(en.get('lado', '')).lower()
    d = 1 if lado in ('compra', 'buy', 'largo', 'long') else -1 if lado in ('venta', 'sell', 'corto', 'short') else 0
    motivo = str(en.get('motivo') or '')[:200]
    x = num.get(sim)
    if not d or not x or sim in pos or len(pos) >= A.CUENTA_CFG['max_posiciones'] or sim in A._vetados(a, cid, g):
        A._poner(a['id'], ultimo=f"🧠 quería {lado} {sim}, no es posible ahora")
        return
    try:
        sa, rr = min(max(float(en.get('stop_atr', 2)), 1.0), 4.0), min(max(float(en.get('rr', 2)), 1.0), 4.0)
    except (TypeError, ValueError):
        sa, rr = 2.0, 2.0
    p, dist = x['precio'], x['atr'] * sa
    if rr * dist / p * 100 < 2 * x['coste'] or dist / p * 100 < 2 * x['coste']:
        A._poner(a['id'], ultimo=f"🧠 {sim}: descartado, los costes ({x['coste']:.2f}%) se comen el objetivo")
        return
    r = motor.resumen(cid)
    libres = max(1, A.CUENTA_CFG['max_posiciones'] - len(pos))
    qty = min(r['equity'] * g['riesgo_pct'] / 100 / dist, r['poder_compra'] * 0.97 / libres / p)
    if qty * p < 0.0001:
        return
    gt = dict(g, stop_atr=sa, rr=rr)
    rasgos = {'atr_pct': round(x['atr'] / p * 100, 4), 'tendencia': x['tend'], 'hora': time.gmtime().tm_hour, 'spread_pct': round(x['spread'], 4),
              'coste_pct': round(x['coste'], 4), 'precio': p, 'dist': dist, 'entrada': 'mercado', 'tramos': 1, 'motivo': motivo,
              'confianza': en.get('confianza'), **sentimiento.rasgos(sim)}
    try:
        o = motor.enviar(cid, sim, 'buy' if d > 0 else 'sell', qty, 'mercado', sl=p - d * dist, tp=p + d * rr * dist, setup='llm',
                         nota=f"{a['nombre']}: {motivo}"[:250], origen='agente')
    except motor.ErrorOrden as err:
        A._poner(a['id'], ultimo=f'🧠 orden rechazada: {err}'[:200])
        return
    if o['estado'] == 'ejecutada':
        A._al_llenar(a, {'cid': cid, 'it': it['id'], 'sim': sim, 'd': d, 'g': gt, 'rasgos': rasgos, 'q': qty, 'dist': dist, 'riesgo': qty * dist}, o)
        A._poner(a['id'], ultimo=f"🧠 {'🟢' if d > 0 else '🔴'} {sim}: {motivo}"[:200])


def ronda(e):
    if not _ronda.acquire(blocking=False):
        return
    try:
        agentes = asegurar(e)
        filas, num = mercado(e)
        if not filas:
            return
        for a in agentes:
            try:
                decidir(e, db.uno('SELECT * FROM ag_agentes WHERE id=?', (a['id'],)), filas, num)
            except llm.SinCuota as err:
                A.log(f'🧠 escuadrón en espera: {err}')
                break
            except Exception:
                A.log(f"🧠 error en {a['nombre']}: " + traceback.format_exc().splitlines()[-1])
    finally:
        _ronda.release()

# ---------------- jefe de mesa ----------------


def _limpiar(cambios):
    out = {}
    for k, v in (cambios or {}).items():
        lim = GENES_JEFE.get(k)
        if lim is None:
            continue
        if k == 'roi_min' and v is None:
            out[k] = None
        elif isinstance(lim[0], str) or isinstance(lim[0], bool):
            if v in lim:
                out[k] = v
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            v = min(max(v, lim[0]), lim[1])
            out[k] = int(round(v)) if isinstance(lim[0], int) else round(float(v), 3)
    if 'estrategia' in out:
        out['p'] = dict(A.ESTRATEGIAS[out['estrategia']])
    return out


def _resumen_genes(medidos):
    def uno(f, a):
        g = A._g(a)
        return (f"{f:+.2f}R {g['estrategia']} {g['marco']} stop {g['stop_atr']} rr {g['rr']} entrada {g['entrada']} tramos {g['tramos']} "
                f"roi {g['roi_min']} trail {g['trail_r']} ia {g['filtro_ia']} riesgo {g['riesgo_pct']}%")
    return ['Mejores: ' + ' | '.join(uno(f, a) for f, a in medidos[:5]), 'Peores: ' + ' | '.join(uno(f, a) for f, a in medidos[-5:])]


def jefe(e):
    """Análisis del jefe de mesa + propuestas validadas en el simulador. Devuelve el informe guardado."""
    if not _jefe.acquire(blocking=False):
        return None
    try:
        medidos = sorted(((f, a) for f, a in ((A.aptitud(a)[0], a) for a in A.agentes() if not es_llm(a)) if f is not None), key=lambda x: -x[0])
        texto = '\n'.join([
            'Eres el jefe de mesa de un enjambre de agentes de trading simulado con precios reales (cripto y acciones, cuentas de 1 $). '
            'Cada agente tiene genes (estrategia, marco de velas, stop en ATR, rr, filtros…) y la evolución reemplaza a los peores. '
            'Lee el informe y propone hasta 3 cambios concretos de genes que creas que mejorarán el resultado. Cada propuesta se probará '
            'en un simulador con velas reales antes de aplicarse, así que sé concreto.',
            'INFORME:', A.informe(), *_resumen_genes(medidos),
            'Genes que puedes cambiar y rangos: ' + ', '.join(f"{k} {v}" for k, v in GENES_JEFE.items()) + '.',
            'Responde SOLO JSON: {"analisis": "máximo 5 frases en español", "propuestas": [{"titulo": "...", "a_quien": "peores" | "todos" | '
            '"estrategia:NOMBRE", "cambios": {"gen": valor}}]}',
        ])
        resp = llm.preguntar(texto, timeout=180, temperatura=0.2)
        analisis = str(resp.get('analisis') or '')[:900]
        res = []
        props = [p for p in (resp.get('propuestas') or []) if isinstance(p, dict)][:3]
        if props:
            marcos = set(A.MARCOS)
            ctx = A.contexto(e, marcos)
            sims = [s for s in A._universo(e) if s in ctx['q']][:8]
            coste = statistics.fmean(ctx['coste'][s][0] for s in sims) if sims else 0.3
            ahorro = A.CUENTA_CFG['deslizamiento_pct'] + (statistics.fmean(ctx['coste'][s][1] for s in sims) / 2 if sims else 0)
            series = {m: [v for v in (ctx['v'].get((s, m)) for s in sims) if v] for m in marcos}
            for pr in props:
                cambios = _limpiar(pr.get('cambios'))
                quien = str(pr.get('a_quien') or 'peores')
                if quien.startswith('estrategia:'):
                    obj = [x for x in medidos if A._g(x[1])['estrategia'] == quien.split(':', 1)[1].strip()]
                elif quien == 'todos':
                    obj = medidos
                else:
                    obj = medidos[-max(3, len(medidos) // 5):]
                titulo = str(pr.get('titulo') or 'propuesta')[:120]
                if not cambios or not obj:
                    res.append({'titulo': titulo, 'resultado': 'descartada (sin cambios válidos o sin agentes a los que aplicar)'})
                    continue
                deltas = []
                for f, a in obj[:10]:
                    g = A._g(a)
                    g2 = dict(copy.deepcopy(g), **cambios)
                    r1 = A.simular(g, series.get(g['marco'], []), coste, ahorro)
                    r2 = A.simular(g2, series.get(g2['marco'], []), coste, ahorro)
                    if len(r1) >= 3 and len(r2) >= 3:
                        deltas.append(statistics.fmean(r2) - statistics.fmean(r1))
                    elif len(r2) >= 3 and statistics.fmean(r2) > 0:
                        deltas.append(statistics.fmean(r2))
                mejora = statistics.fmean(deltas) if deltas else None
                if mejora is not None and mejora > 0.05 and sum(1 for x in deltas if x > 0) > len(deltas) / 2:
                    for f, a in obj:
                        g = dict(A._g(a), **cambios)
                        A._poner(a['id'], genoma=json.dumps(g), genoma_desde=int(time.time()))
                    res.append({'titulo': titulo, 'cambios': cambios, 'mejora': round(mejora, 3), 'agentes': len(obj),
                                'resultado': f'APLICADA a {len(obj)} agentes (simulación {mejora:+.2f}R por trade)'})
                else:
                    res.append({'titulo': titulo, 'cambios': cambios, 'mejora': round(mejora, 3) if mejora is not None else None,
                                'resultado': 'rechazada por la simulación' + (f' ({mejora:+.2f}R)' if mejora is not None else ' (pocos trades)')})
        it = {'ts': int(time.time()), 'analisis': analisis, 'propuestas': res, 'modelo': llm.MODELO}
        db.poner_ajuste('jefe_mesa', it)
        A._leccion(None, None, None, 'jefe', '🧑‍💼 Jefe de mesa: ' + analisis + (' ' + ' | '.join(f"{x['titulo']}: {x['resultado']}" for x in res) if res else ''), {})
        return it
    finally:
        _jefe.release()


def resumen_jefe():
    j = db.ajuste('jefe_mesa', None)
    if not j:
        return 'Jefe de mesa: aún sin análisis.'
    return (f"Jefe de mesa (hace {(time.time() - j['ts']) / 60:.0f} min): {j['analisis']} " +
            ' | '.join(f"{x['titulo']}: {x['resultado']}" for x in j.get('propuestas', [])))
