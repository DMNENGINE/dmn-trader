"""Sentimiento del mercado (idea de quantum-trader): índice Fear & Greed de cripto + titulares de cada símbolo puntuados por un
modelo de lenguaje (gemma vía ollama, el cerebro de APOLO). Son rasgos más para el filtro de IA: si no ayudan, el modelo los ignora.
Solo el nodo con ollama puntúa noticias (el PC); la Pi recibe las puntuaciones en la sincronización. Sin LLM: solo Fear & Greed."""
import json, os, re, time, urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from . import db

OLLAMA = os.environ.get('DMN_TRADER_OLLAMA', 'http://127.0.0.1:11434').rstrip('/')
MODELO = os.environ.get('DMN_TRADER_LLM', 'gemma4:31b-cloud')
CLAVE, VIGENCIA, HORAS = 'sentimiento', 3 * 3600, 12
UA = 'Mozilla/5.0 (DMN Trader)'


def _get(url, timeout=12, datos=None):
    req = urllib.request.Request(url, data=datos, headers={'User-Agent': UA, **({'content-type': 'application/json'} if datos else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


def fear_greed():
    d = json.loads(_get('https://api.alternative.me/fng/?limit=1'))['data'][0]
    return {'valor': int(d['value']), 'clase': d['value_classification'], 'ts': int(d['timestamp'])}


def titulares(simbolo, horas=HORAS, maximo=6):
    """Titulares recientes del símbolo (RSS de Yahoo Finance, gratis y sin clave)."""
    raiz = ET.fromstring(_get(f'https://feeds.finance.yahoo.com/rss/2.0/headline?s={simbolo}&region=US&lang=en-US'))
    out, limite = [], time.time() - horas * 3600
    for it in raiz.iter('item'):
        try:
            ts = parsedate_to_datetime(it.findtext('pubDate')).timestamp()
        except Exception:
            continue
        if ts >= limite and it.findtext('title'):
            out.append((int(ts), it.findtext('title').strip()))
    return sorted(out, reverse=True)[:maximo]


def llm_disponible():
    try:
        return any(m['name'].startswith(MODELO.split(':')[0]) for m in json.loads(_get(OLLAMA + '/api/tags', 3))['models'])
    except Exception:
        return False


def _preguntar(texto):
    cuerpo = {'model': MODELO, 'stream': False, 'format': 'json', 'options': {'temperature': 0},
              'messages': [{'role': 'user', 'content': texto}]}
    r = json.loads(_get(OLLAMA + '/api/chat', 120, json.dumps(cuerpo).encode()))['message']['content']
    m = re.search(r'\{.*\}', r, re.S)
    return json.loads(m.group(0)) if m else {}


def puntuar(por_simbolo):
    """{símbolo: [titulares]} → {símbolo: puntuación -1..1} con UNA sola consulta al modelo."""
    if not por_simbolo:
        return {}
    bloques = '\n\n'.join(f"{s}:\n" + '\n'.join(f'- {t}' for t in ts) for s, ts in por_simbolo.items())
    texto = ('Eres un analista de trading de corto plazo. Para cada símbolo, puntúa de -1 (noticias claramente negativas para su precio '
             'en las próximas horas) a +1 (claramente positivas); 0 si son neutras, irrelevantes o no tratan de ese activo. '
             'Responde SOLO un objeto JSON {"SÍMBOLO": número}.\n\n' + bloques)
    res = _preguntar(texto)
    return {s: max(-1.0, min(1.0, float(res[s]))) for s in por_simbolo if isinstance(res.get(s), (int, float))}


def actual():
    return db.ajuste(CLAVE, {}) or {}


def actualizar(simbolos):
    """Refresca Fear & Greed y, si hay modelo de lenguaje en este equipo, las noticias. Devuelve el estado guardado."""
    st = actual()
    try:
        st['fng'] = fear_greed()
    except Exception:
        pass
    if llm_disponible():
        heads = {}
        for s in simbolos:
            try:
                h = titulares(s)
            except Exception:
                continue
            if h:
                heads[s] = [t for _, t in h]
        if heads:
            notas = puntuar(heads)
            ahora = int(time.time())
            sims = {s: v for s, v in (st.get('simbolos') or {}).items() if ahora - v.get('ts', 0) < VIGENCIA}
            for s, v in notas.items():
                sims[s] = {'s': round(v, 2), 'n': len(heads[s]), 'titular': heads[s][0][:140], 'ts': ahora}
            st.update(simbolos=sims, ts=ahora, modelo=MODELO)
    db.poner_ajuste(CLAVE, st)
    return st


def recibir(otro):
    """En la central: adopta las puntuaciones que manda un nodo con LLM si son más recientes."""
    if not otro or not otro.get('ts'):
        return
    st = actual()
    if otro['ts'] > st.get('ts', 0):
        st.update(simbolos=otro.get('simbolos') or {}, ts=otro['ts'], modelo=otro.get('modelo'))
        if not st.get('fng') and otro.get('fng'):
            st['fng'] = otro['fng']
        db.poner_ajuste(CLAVE, st)


def rasgos(simbolo, st=None):
    """Lo que se guarda con cada operación (y usa la IA): Fear & Greed y nota de noticias vigente."""
    st = st or actual()
    out = {}
    if st.get('fng'):
        out['fng'] = st['fng']['valor']
    v = (st.get('simbolos') or {}).get(simbolo)
    if v and time.time() - v.get('ts', 0) < VIGENCIA:
        out['sent'] = v['s']
    return out


def resumen():
    st = actual()
    partes = []
    if st.get('fng'):
        partes.append(f"Fear & Greed {st['fng']['valor']} ({st['fng']['clase']})")
    sims = {s: v for s, v in (st.get('simbolos') or {}).items() if time.time() - v.get('ts', 0) < VIGENCIA}
    if sims:
        orden = sorted(sims.items(), key=lambda x: -abs(x[1]['s']))[:6]
        partes.append('noticias según ' + str(st.get('modelo', 'LLM')).split(':')[0] + ' hace ' + f"{(time.time() - st.get('ts', 0)) / 60:.0f} min: " +
                      ', '.join(f"{s.replace('-USD', '')} {v['s']:+.1f}" for s, v in orden))
    return 'Sentimiento: ' + (' · '.join(partes) if partes else 'aún sin datos') + '.'
