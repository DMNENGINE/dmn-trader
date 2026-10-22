"""Analista de noticias en tiempo real: lee sin parar las fuentes que mueven el mercado (prensa financiera, cripto, Reserva
Federal, SEC, Google News de cada símbolo) y el calendario económico; gemma analiza cada titular nuevo (a qué afecta, en qué
dirección, cuánto importa y por qué) y el analista mantiene una puntuación por símbolo y del mercado que decae con el tiempo.

Lo usan: los rasgos de cada operación (filtro de IA), el gen `noticias` (no operar contra noticias fuertes ni junto a eventos
macro), el escuadrón LLM, el jefe de mesa y Mission Control. Solo analiza el nodo con modelo de lenguaje (el PC); la Pi recibe el
resultado en la sincronía. Sin dependencias: RSS/Atom con xml.etree."""
import hashlib, json, math, re, threading, time, traceback, urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from . import db, llm, sentimiento

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) DMN-Trader-Analista'
FUENTES = [  # (nombre, url, cada cuántos segundos, ámbito)
    ('CNBC', 'https://www.cnbc.com/id/100003114/device/rss/rss.html', 120, 'mercado'),
    ('CNBC Economía', 'https://www.cnbc.com/id/20910258/device/rss/rss.html', 300, 'macro'),
    ('MarketWatch', 'https://feeds.content.dowjones.io/public/rss/mw_topstories', 120, 'mercado'),
    ('MarketWatch última hora', 'https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines', 90, 'mercado'),
    ('Investing', 'https://www.investing.com/rss/news_25.rss', 180, 'mercado'),
    ('Seeking Alpha', 'https://seekingalpha.com/market_currents.xml', 180, 'mercado'),
    ('CoinDesk', 'https://www.coindesk.com/arc/outboundfeeds/rss/', 120, 'cripto'),
    ('Cointelegraph', 'https://cointelegraph.com/rss', 120, 'cripto'),
    ('Decrypt', 'https://decrypt.co/feed', 180, 'cripto'),
    ('The Block', 'https://www.theblock.co/rss.xml', 180, 'cripto'),
    ('Reserva Federal', 'https://www.federalreserve.gov/feeds/press_all.xml', 300, 'macro'),
    ('SEC', 'https://www.sec.gov/news/pressreleases.rss', 600, 'macro'),
]
NOMBRES = {'BTC-USD': 'bitcoin', 'ETH-USD': 'ethereum', 'SOL-USD': 'solana crypto', 'XRP-USD': 'XRP ripple', 'DOGE-USD': 'dogecoin', 'ADA-USD': 'cardano',
           'AVAX-USD': 'avalanche AVAX', 'LINK-USD': 'chainlink', 'LTC-USD': 'litecoin', 'NEAR-USD': 'NEAR protocol', 'SUI-USD': 'SUI crypto', 'UNI-USD': 'uniswap',
           'HBAR-USD': 'hedera HBAR', 'BCH-USD': 'bitcoin cash', 'DOT-USD': 'polkadot', 'SPY': 'S&P 500', 'QQQ': 'Nasdaq 100', 'TSLA': 'Tesla stock',
           'NVDA': 'Nvidia stock', 'AAPL': 'Apple stock', 'AMD': 'AMD stock', 'META': 'Meta Platforms stock', 'AMZN': 'Amazon stock'}
CADA_SIMBOLO, VIDA, MEDIA_VIDA, LOTE = 300, 12 * 3600, 3 * 3600, 30
CALENDARIO = 'https://nfs.faireconomy.media/ff_calendar_thisweek.json'
ESQUEMA = """CREATE TABLE IF NOT EXISTS an_noticias (id INTEGER PRIMARY KEY, hash TEXT UNIQUE, ts INTEGER, visto INTEGER, fuente TEXT, ambito TEXT,
  titulo TEXT, link TEXT, simbolos TEXT, impacto REAL, importancia REAL, categoria TEXT, resumen TEXT, analizada INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_an_ts ON an_noticias(analizada, ts);"""
_hilo, _parar, _ult, _CAL = None, threading.Event(), {}, [0, []]
ESTADO = {'ciclos': 0, 'errores': 0, 'ultimo': None}


def _get(url, timeout=12):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _ts(txt):
    if not txt:
        return None
    try:
        return int(parsedate_to_datetime(txt).timestamp())
    except Exception:
        pass
    try:
        return int(time.mktime(time.strptime(txt[:19], '%Y-%m-%dT%H:%M:%S')) - time.timezone)
    except Exception:
        return None


def leer_rss(url):
    """Titulares de un RSS o Atom: [(ts, título, enlace)]."""
    raiz = ET.fromstring(_get(url))
    out = []
    for it in raiz.iter():
        tag = it.tag.split('}')[-1]
        if tag not in ('item', 'entry'):
            continue
        campos = {c.tag.split('}')[-1]: c for c in it}
        tit = (campos.get('title').text or '').strip() if campos.get('title') is not None else ''
        link = campos.get('link')
        link = (link.text or link.get('href') or '') if link is not None else ''
        fecha = next((campos[k].text for k in ('pubDate', 'published', 'updated', 'date') if campos.get(k) is not None), None)
        if tit:
            out.append((_ts(fecha) or int(time.time()), re.sub(r'\s+', ' ', tit)[:300], link.strip()[:400]))
    return out


def _clave(titulo):
    t = re.sub(r'\s+[-–|]\s+[^-–|]{2,40}$', '', titulo)          # Google News añade " - Medio"
    t = re.sub(r'[^a-z0-9 ]', '', t.lower())
    return hashlib.sha1(' '.join(t.split()[:14]).encode()).hexdigest()[:20]


def preparar():
    db.con().executescript(ESQUEMA)


def recoger(simbolos):
    """Pasa por las fuentes que tocan y guarda los titulares nuevos (de las últimas 12 h)."""
    ahora, nuevas = time.time(), 0
    tareas = [(n, u, s, a) for n, u, s, a in FUENTES]
    for sim in simbolos:
        q = urllib.request.quote(f"{NOMBRES.get(sim, sim)} when:1d")
        tareas.append((f'Google News · {sim}', f'https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en', CADA_SIMBOLO, sim))
    for nombre, url, cada, ambito in tareas:
        if ahora - _ult.get(url, 0) < cada:
            continue
        _ult[url] = ahora
        try:
            items = leer_rss(url)
        except Exception:
            ESTADO['errores'] += 1
            continue
        for ts, tit, link in items[:25]:
            if ts < ahora - VIDA or ts > ahora + 600:
                continue
            fuente = nombre if not nombre.startswith('Google') else (tit.rsplit(' - ', 1)[-1][:40] if ' - ' in tit else nombre)
            try:
                antes = db.con().total_changes
                db.ex('INSERT OR IGNORE INTO an_noticias(hash,ts,visto,fuente,ambito,titulo,link) VALUES(?,?,?,?,?,?,?)',
                      (_clave(tit), ts, int(ahora), fuente, ambito, tit, link))
                nuevas += db.con().total_changes - antes
            except Exception:
                pass
    return nuevas


def analizar(simbolos, lote=LOTE):
    """gemma analiza los titulares pendientes (los más recientes primero) en UNA consulta."""
    pend = db.q('SELECT id, ts, fuente, ambito, titulo FROM an_noticias WHERE analizada=0 AND ts>? ORDER BY ts DESC LIMIT ?', (int(time.time()) - VIDA, lote))
    if not pend:
        return 0
    lista = '\n'.join(f"{i}. [{p['fuente']}{' · ' + p['ambito'] if p['ambito'] not in ('mercado', 'macro', 'cripto') else ''}] {p['titulo']}" for i, p in enumerate(pend))
    texto = ('Eres el analista de noticias de una mesa de trading de corto plazo (horas). Para CADA titular decide:\n'
             f"- simbolos: a cuáles de esta lista afecta directamente {', '.join(simbolos)}; o 'CRIPTO' (todo el mercado cripto), 'ACCIONES' (bolsa de EE. UU.), "
             "'MERCADO' (riesgo global: Fed, inflación, geopolítica); [] si no afecta a nada.\n"
             '- impacto: de -1 (muy negativo para el precio) a +1 (muy positivo); 0 si es neutro.\n'
             '- importancia: de 0 (ruido, opinión, publicidad) a 1 (noticia que mueve el mercado ya: decisión de la Fed, hackeo, resultados, aprobación de un ETF…).\n'
             '- categoria: una de macro, fed, resultados, regulacion, hackeo, etf, adopcion, geopolitica, empresa, rumor, analisis, otro.\n'
             '- resumen: en español, máximo 14 palabras, qué pasa y por qué importa.\n'
             'Responde SOLO JSON: {"n": [{"i": 0, "simbolos": [], "impacto": 0, "importancia": 0, "categoria": "", "resumen": ""}]}\n\nTITULARES:\n' + lista)
    res = llm.preguntar(texto, timeout=180)
    validos = set(simbolos) | {'CRIPTO', 'ACCIONES', 'MERCADO'}
    hechos = 0
    for r in res.get('n') or []:
        try:
            p = pend[int(r['i'])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        sims = [s for s in (r.get('simbolos') or []) if isinstance(s, str) and s.upper() in validos]
        if p['ambito'] in simbolos and p['ambito'] not in sims and float(r.get('importancia') or 0) > .2:
            sims.append(p['ambito'])                           # vino de la búsqueda de ese símbolo
        imp = max(-1.0, min(1.0, float(r.get('impacto') or 0))) if isinstance(r.get('impacto'), (int, float)) else 0.0
        ipt = max(0.0, min(1.0, float(r.get('importancia') or 0))) if isinstance(r.get('importancia'), (int, float)) else 0.0
        db.ex('UPDATE an_noticias SET simbolos=?, impacto=?, importancia=?, categoria=?, resumen=?, analizada=1 WHERE id=?',
              (json.dumps([s.upper() for s in sims]), imp, ipt, str(r.get('categoria') or 'otro')[:20], str(r.get('resumen') or '')[:200], p['id']))
        hechos += 1
    for p in pend:  # las que el modelo no devolvió no se reintentan para siempre
        db.ex('UPDATE an_noticias SET analizada=CASE WHEN analizada=0 THEN -1 ELSE analizada END WHERE id=? AND analizada=0 AND visto<?', (p['id'], int(time.time()) - 900))
    return hechos


def calendario():
    """Eventos económicos de la semana (ForexFactory): los de impacto alto y medio de EE. UU. y globales.
    Se pide como mucho cada 30 min (la fuente limita); si falla, se queda con lo último que trajo."""
    if time.time() - _CAL[0] < 1800:
        return _CAL[1]
    try:
        crudo = json.loads(_get(CALENDARIO))
    except Exception:
        return _CAL[1]
    out = []
    for e in crudo:
        if e.get('country') not in ('USD', 'All') or e.get('impact') not in ('High', 'Medium'):
            continue
        ts = _ts(e.get('date'))
        if ts:
            out.append({'ts': ts, 'titulo': e['title'], 'impacto': e['impact'], 'pais': e['country'], 'prevision': e.get('forecast'), 'anterior': e.get('previous')})
    _CAL[:] = [time.time(), sorted(out, key=lambda x: x['ts'])]
    return _CAL[1]


def agregar(simbolos):
    """Puntuación por símbolo y del mercado: suma de impacto × importancia con media vida de 3 h, más la parte del sector."""
    ahora = time.time()
    filas = db.q('SELECT * FROM an_noticias WHERE analizada=1 AND importancia>0.15 AND ts>? ORDER BY ts DESC', (int(ahora) - VIDA,))
    peso = lambda f: f['impacto'] * f['importancia'] * 0.5 ** ((ahora - f['ts']) / MEDIA_VIDA)
    grupo = {'CRIPTO': 0.0, 'ACCIONES': 0.0, 'MERCADO': 0.0}
    por, mejor, repe = {s: 0.0 for s in simbolos}, {}, {}
    for f in sorted(filas, key=lambda f: -abs(peso(f))):
        sims = json.loads(f['simbolos'] or '[]')
        tema = (tuple(sorted(sims)), f['categoria'])          # la misma historia contada por varios medios cuenta menos cada vez
        k = repe[tema] = repe.get(tema, -1) + 1
        w = peso(f) * 0.5 ** k
        for s in sims:
            if s in grupo:
                grupo[s] += w
            elif s in por:
                por[s] += w
                if s not in mejor or abs(peso(f)) > abs(peso(mejor[s])):
                    mejor[s] = f
    out = {}
    for s in simbolos:
        sector = grupo['CRIPTO'] if s.endswith('-USD') else grupo['ACCIONES']
        v = math.tanh((por[s] + 0.5 * sector + 0.3 * grupo['MERCADO']) / 1.5)
        out[s] = {'s': round(v, 2), 'n': sum(1 for f in filas if s in (f['simbolos'] or '')), 'titular': (mejor[s]['resumen'] or mejor[s]['titulo'])[:140] if s in mejor else '', 'ts': int(ahora)}
    macro = {k: round(math.tanh(v), 2) for k, v in grupo.items()}
    top = sorted(filas, key=lambda f: -abs(peso(f)))[:30]
    noticias = [{'ts': f['ts'], 'fuente': f['fuente'], 'titulo': f['titulo'], 'resumen': f['resumen'], 'impacto': f['impacto'], 'importancia': f['importancia'],
                 'categoria': f['categoria'], 'simbolos': json.loads(f['simbolos'] or '[]'), 'link': f['link']} for f in top]
    return out, macro, noticias


def ciclo(simbolos):
    """Una pasada completa: recoger, analizar, calendario y publicar en el estado de sentimiento."""
    preparar()
    recoger(simbolos)
    n = 0
    for _ in range(3):  # hasta 3 lotes por pasada si hay atasco (p. ej. al arrancar)
        try:
            k = analizar(simbolos)
        except llm.SinCuota:
            break
        n += k
        if k < LOTE:
            break
    st = sentimiento.actual()
    try:
        if time.time() - st.get('eventos_ts', 0) > 1800:
            st['eventos'], st['eventos_ts'] = calendario(), int(time.time())
    except Exception:
        pass
    sims, macro, noticias = agregar(simbolos)
    st.update(simbolos=sims, macro=macro, noticias=noticias, ts=int(time.time()), modelo=llm.MODELO, analista=True)
    db.poner_ajuste(sentimiento.CLAVE, st)
    ESTADO.update(ciclos=ESTADO['ciclos'] + 1, ultimo=int(time.time()), analizadas=n)
    return n


def arrancar(universo, cada=60):
    """Hilo propio del analista (solo donde hay modelo de lenguaje). `universo` es una función que da los símbolos actuales."""
    global _hilo
    if _hilo and _hilo.is_alive():
        return
    _parar.clear()

    def bucle():
        while not _parar.is_set():
            t = time.time()
            try:
                ciclo(universo())
            except Exception:
                ESTADO['errores'] += 1
                ESTADO['error'] = traceback.format_exc().splitlines()[-1]
            _parar.wait(max(5, cada - (time.time() - t)))
    _hilo = threading.Thread(target=bucle, daemon=True, name='analista')
    _hilo.start()


def activo():
    return bool(_hilo and _hilo.is_alive())


def evento_cerca(st=None, antes=15, despues=30):
    """Evento macro de impacto alto entre `antes` min por delante y `despues` min por detrás, o None."""
    st = st or sentimiento.actual()
    ahora = time.time()
    for e in st.get('eventos') or []:
        if e['impacto'] == 'High' and -despues * 60 <= e['ts'] - ahora <= antes * 60:
            return e
    return None


def proximo_evento(st=None):
    st = st or sentimiento.actual()
    fut = [e for e in st.get('eventos') or [] if e['ts'] > time.time() - 1800]
    return fut[0] if fut else None


def resumen(st=None):
    st = st or sentimiento.actual()
    if not st.get('analista'):
        return 'Analista de noticias: sin datos (solo corre en el equipo con modelo de lenguaje).'
    no = st.get('noticias') or []
    fuertes = [x for x in no if x['importancia'] >= .7][:3]
    ev = proximo_evento(st)
    m = st.get('macro') or {}
    partes = [f"Analista de noticias (hace {(time.time() - st.get('ts', 0)) / 60:.0f} min): mercado {m.get('MERCADO', 0):+.2f} · cripto {m.get('CRIPTO', 0):+.2f} · acciones {m.get('ACCIONES', 0):+.2f}"]
    if fuertes:
        partes.append('lo más fuerte: ' + ' | '.join(f"{x['resumen'] or x['titulo']} ({x['impacto']:+.1f})" for x in fuertes))
    if ev:
        partes.append(f"próximo evento: {ev['titulo']} ({ev['impacto']}) {time.strftime('%d/%m %H:%M', time.localtime(ev['ts']))}")
    return '. '.join(partes) + '.'
