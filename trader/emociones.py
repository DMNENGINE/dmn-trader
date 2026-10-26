"""Emociones de los agentes del enjambre: un estado interno que sube y baja con lo que les pasa, cambia cómo operan y se ve.

Seis emociones (0..1) con inercia: cada actualización mezcla el ánimo anterior con el que piden los hechos (rachas, caída desde el
máximo, tiempo sin operar, quiebras, metas, ascensos). Están pensadas para que operen MEJOR, no como un humano con miedo y codicia:
  miedo       → baja el tamaño tras perder        · confianza → lo sube un poco con rachas buenas (con tope)
  euforia     → frena la sobreconfianza tras ganar mucho de golpe
  hambre      → busca otro sistema antes si lleva mucho sin encontrar operaciones (no rebaja sus filtros)
  frustración → se para 30 min a pensar en vez de vengarse del mercado
  orgullo     → tras una meta o un ascenso; lo celebra y lo cuenta
El ánimo medio del enjambre se le pasa a APOLO (su conciencia) con cambios pequeños."""
import json, time, threading, urllib.request
from . import db

EMOCIONES = ('hambre', 'confianza', 'miedo', 'euforia', 'frustracion', 'orgullo')
EMOJI = {'hambre': '😤', 'confianza': '😎', 'miedo': '😰', 'euforia': '🤯', 'frustracion': '😔', 'orgullo': '🏆', 'calma': '🙂'}
NOMBRE = {'hambre': 'con hambre', 'confianza': 'confiado', 'miedo': 'con miedo', 'euforia': 'eufórico', 'frustracion': 'frustrado',
          'orgullo': 'orgulloso', 'calma': 'tranquilo'}
INERCIA = 0.6        # cuánto pesa el ánimo anterior en cada actualización
UMBRAL = 0.35        # por debajo de esto ninguna emoción manda: está tranquilo
CADA = 60            # segundos entre actualizaciones de un agente
PAUSA_FRUSTRADO = 30 * 60
ESQUEMA = """CREATE TABLE IF NOT EXISTS ag_sentimientos (id INTEGER PRIMARY KEY, ts INTEGER, agente_id INTEGER, emocion TEXT, intensidad REAL, texto TEXT);
CREATE INDEX IF NOT EXISTS ix_sent_ag ON ag_sentimientos(agente_id, id);"""
_ULT = {}


def preparar():
    c = db.con()
    c.executescript(ESQUEMA)
    cols = {r[1] for r in c.execute('PRAGMA table_info(ag_agentes)')}
    for col, tipo in (('animo', 'TEXT'), ('medallas', 'TEXT'), ('ascendido', 'INTEGER DEFAULT 0'), ('inmune_gen', 'INTEGER DEFAULT 0')):
        if col not in cols:
            c.execute(f'ALTER TABLE ag_agentes ADD COLUMN {col} {tipo}')


def _lim(x):
    return max(0.0, min(1.0, x))


def leer(a):
    try:
        v = json.loads(a.get('animo') or '{}') if isinstance(a, dict) else json.loads(a['animo'] or '{}')
    except (ValueError, TypeError, KeyError, IndexError):
        v = {}
    return v if isinstance(v, dict) else {}


def objetivo(h):
    """Lo que los hechos piden sentir ahora. h: rs (R de las últimas operaciones, en orden), equity, capital, max_equity,
    sin_operar_s, descartes, meta_reciente, quiebra_reciente, ascendido."""
    rs = h.get('rs') or []
    racha_g = racha_p = 0
    for r in reversed(rs):
        if r > 0 and not racha_p:
            racha_g += 1
        elif r <= 0 and not racha_g:
            racha_p += 1
        else:
            break
    suma5 = sum(rs[-5:])
    cap, eq = h.get('capital') or 1.0, h.get('equity') or 0.0
    caida = 1 - eq / max(h.get('max_equity') or 0, cap) if eq else 0.0
    hambre = 0.8 * _lim((h.get('sin_operar_s') or 0) / 7200) + 0.2 * _lim((h.get('descartes') or 0) / 150)
    confianza = _lim(0.22 * racha_g + 0.12 * max(0.0, suma5)) if len(rs) >= 3 else 0.0
    miedo = _lim(0.22 * racha_p + 0.1 * max(0.0, -suma5) + 0.8 * _lim(caida))
    euforia = _lim((eq / cap - 1.5) / 3) + (0.35 if rs and rs[-1] >= 2 else 0.0)
    frustracion = 0.95 if h.get('quiebra_reciente') else _lim(0.2 * racha_p - 0.25 + 0.4 * (hambre > 0.85))
    orgullo = 1.0 if h.get('meta_reciente') or h.get('ascenso_reciente') else (0.3 if h.get('ascendido') else 0.0)
    return {'hambre': hambre, 'confianza': confianza, 'miedo': miedo, 'euforia': _lim(euforia), 'frustracion': frustracion, 'orgullo': orgullo}


def mezclar(viejo, obj):
    v = viejo.get('valores') or {}
    return {k: round(INERCIA * float(v.get(k, 0.0)) + (1 - INERCIA) * obj[k], 3) for k in EMOCIONES}


def dominante(valores):
    k = max(EMOCIONES, key=lambda x: valores.get(x, 0))
    return (k, valores[k]) if valores.get(k, 0) >= UMBRAL else ('calma', 1 - max(valores.values() or [0]))


def frase(emo, h):
    rs = h.get('rs') or []
    ult = f"{rs[-1]:+.1f}R" if rs else ''
    return {
        'hambre': f"Llevo {int((h.get('sin_operar_s') or 0) / 60)} min sin una operación que pague los costes. Voy a buscar otra forma de entrar.",
        'confianza': f"Mi sistema está funcionando ({sum(rs[-5:]):+.1f}R en las últimas). Subo un poco el tamaño, sin pasarme.",
        'miedo': f"Lo estoy pasando mal: {h.get('equity', 0):.3f} $ y cayendo desde mi máximo. Bajo el tamaño hasta que se aclare.",
        'euforia': f"¡Voy {h.get('equity', 0) / (h.get('capital') or 1):.1f}× el capital{(' y la última ' + ult) if ult else ''}! Freno: así es como se pierde todo.",
        'frustracion': 'He quebrado. Me paro a pensar qué falló antes de volver.' if h.get('quiebra_reciente') else 'Pierdo una y otra vez. Me paro 30 min a pensar en vez de vengarme del mercado.',
        'orgullo': '¡Lo conseguí! Llegué a la meta.' if h.get('meta_reciente') else '¡He ascendido! Mi sistema es candidato a dinero real.' if h.get('ascenso_reciente') else 'Sigo con la cabeza alta: soy de los que ascendieron.',
        'calma': 'Tranquilo, esperando mi oportunidad.',
    }[emo]


def modular(animo):
    """Cómo cambia su forma de operar: riesgo (multiplica su % de riesgo), paciencia (multiplica los descartes que aguanta antes
    de buscar otro sistema) y pausa_hasta (frustración)."""
    v = (animo or {}).get('valores') or {}
    g = lambda k: float(v.get(k, 0.0))
    riesgo = max(0.4, min(1.25, 1 + 0.25 * g('confianza') - 0.5 * g('miedo') - 0.4 * g('euforia')))
    return {'riesgo': round(riesgo, 3), 'paciencia': round(1 - 0.5 * g('hambre'), 3), 'pausa_hasta': (animo or {}).get('pausa_hasta', 0)}


def hechos(a, e, it, eq, descartes):
    ahora = time.time()
    desde = a['genoma_desde'] or a['creado'] or 0
    rs = [x['r'] for x in db.q("""SELECT r FROM ag_trades WHERE agente_id=? AND r IS NOT NULL AND COALESCE(causa,'')<>'sistema' AND abierto>=?
                                   ORDER BY trade_id DESC LIMIT 12""", (a['id'], desde))][::-1]
    ult = db.uno('SELECT MAX(abierto) t FROM ag_trades WHERE agente_id=?', (a['id'],))
    ref = max((ult or {}).get('t') or 0, desde, (it or {}).get('inicio') or 0)
    fin = lambda res, s: db.uno('SELECT 1 x FROM ag_intentos WHERE agente_id=? AND resultado=? AND fin>=?', (a['id'], res, int(ahora - s))) is not None
    return {'rs': rs, 'equity': eq, 'capital': e.get('capital') or 1.0, 'max_equity': (it or {}).get('max_equity') or 0, 'sin_operar_s': ahora - ref,
            'descartes': descartes, 'meta_reciente': fin('meta', 6 * 3600), 'quiebra_reciente': fin('quiebra', 2 * 3600),
            'ascendido': bool(a.get('ascendido')), 'ascenso_reciente': bool(a.get('ascendido')) and (leer(a).get('ascenso_ts', 0) > ahora - 6 * 3600)}


def actualizar(a, e, it, eq, descartes, forzar=False):
    """Recalcula el ánimo del agente (como mucho una vez por minuto), apunta en su diario cuando cambia lo que siente
    y devuelve el ánimo guardado."""
    ahora = time.time()
    viejo = leer(a)
    if not forzar and ahora - _ULT.get(a['id'], 0) < CADA and viejo:
        return viejo
    _ULT[a['id']] = ahora
    h = hechos(a, e, it, eq, descartes)
    valores = mezclar(viejo, objetivo(h))
    emo, inten = dominante(valores)
    nuevo = {**viejo, 'valores': valores, 'emocion': emo, 'emoji': EMOJI[emo], 'intensidad': round(inten, 2), 'frase': frase(emo, h), 'ts': int(ahora)}
    if emo == 'frustracion' and valores['frustracion'] >= 0.75 and ahora - viejo.get('ultima_pausa', 0) > 3 * 3600:
        nuevo['pausa_hasta'], nuevo['ultima_pausa'] = int(ahora + PAUSA_FRUSTRADO), int(ahora)
    if emo != viejo.get('emocion'):
        db.ex('INSERT INTO ag_sentimientos(ts,agente_id,emocion,intensidad,texto) VALUES(?,?,?,?,?)', (int(ahora), a['id'], emo, nuevo['intensidad'], nuevo['frase']))
    db.ex('UPDATE ag_agentes SET animo=? WHERE id=?', (json.dumps(nuevo, ensure_ascii=False), a['id']))
    return nuevo


def vista(a):
    v = leer(a)
    return {k: v.get(k) for k in ('emocion', 'emoji', 'intensidad', 'frase', 'valores')} if v else None


def diario(aid, n=20):
    return db.q('SELECT ts, emocion, intensidad, texto FROM ag_sentimientos WHERE agente_id=? ORDER BY id DESC LIMIT ?', (aid, n))


def colectivo(agentes):
    """El ánimo del enjambre: media de cada emoción, la que manda y cuántos sienten cada cosa."""
    vs = [leer(a) for a in agentes]
    vs = [v for v in vs if v.get('valores')]
    if not vs:
        return None
    medias = {k: round(sum(v['valores'].get(k, 0) for v in vs) / len(vs), 3) for k in EMOCIONES}
    cuenta = {}
    for v in vs:
        cuenta[v.get('emocion', 'calma')] = cuenta.get(v.get('emocion', 'calma'), 0) + 1
    emo = max(cuenta, key=cuenta.get)
    return {'emocion': emo, 'emoji': EMOJI[emo], 'nombre': NOMBRE[emo], 'medias': medias, 'cuenta': cuenta, 'agentes': len(vs),
            'texto': f"{EMOJI[emo]} El enjambre está sobre todo {NOMBRE[emo]} ({cuenta[emo]} de {len(vs)}): " +
                     ', '.join(f"{EMOJI[k]} {n}" for k, n in sorted(cuenta.items(), key=lambda x: -x[1]))}


def cambios_apolo(col):
    """Cómo le mueve a APOLO el ánimo de su equipo: pequeños empujones (como mucho ±0,05) en su ánimo y su preocupación."""
    m = col['medias']
    bueno = m['confianza'] + m['orgullo'] + 0.5 * m['euforia']
    malo = m['miedo'] + m['frustracion']
    lim = lambda x: round(max(-0.05, min(0.05, x)), 3)
    return {'animo': lim(0.06 * (bueno - malo)), 'preocupacion': lim(0.05 * (malo - bueno)), 'curiosidad': lim(0.02 * m['hambre'])}


def sentir_apolo(col, apolo_url, token_file):
    """Le pasa a la conciencia de APOLO cómo se siente su equipo de traders (POST /v1/conciencia/sentir). Sin APOLO no pasa nada."""
    if not col:
        return False
    def enviar():
        try:
            tok = open(token_file, encoding='utf-8').read().strip()
            cuerpo = {'cambios': cambios_apolo(col), 'motivo': f"mi equipo de traders: {col['texto']}"[:160]}
            rq = urllib.request.Request(apolo_url + '/v1/conciencia/sentir', method='POST', data=json.dumps(cuerpo).encode(),
                                        headers={'content-type': 'application/json', 'x-robot-token': tok})
            urllib.request.urlopen(rq, timeout=10).read()
        except Exception:
            pass
    threading.Thread(target=enviar, daemon=True).start()
    return True
