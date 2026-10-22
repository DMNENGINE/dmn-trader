"""Acceso al modelo de lenguaje (gemma vía ollama, el cerebro de APOLO) con tope de consultas por hora para no agotar su cuota."""
import json, os, re, threading, time, urllib.request

OLLAMA = os.environ.get('DMN_TRADER_OLLAMA', 'http://127.0.0.1:11434').rstrip('/')
MODELO = os.environ.get('DMN_TRADER_LLM', 'gemma4:31b-cloud')
MAX_HORA = int(os.environ.get('DMN_TRADER_LLM_MAX_HORA', '150'))
_uso, _lock = [], threading.Lock()


class SinCuota(Exception):
    pass


def disponible():
    try:
        with urllib.request.urlopen(OLLAMA + '/api/tags', timeout=3) as r:
            return any(m['name'].startswith(MODELO.split(':')[0]) for m in json.loads(r.read())['models'])
    except Exception:
        return False


def uso_hora():
    with _lock:
        del _uso[:sum(1 for t in _uso if t < time.time() - 3600)]
        return len(_uso)


def preguntar(texto, timeout=120, temperatura=0.0):
    """Devuelve el objeto JSON que conteste el modelo ({} si no contesta JSON)."""
    if uso_hora() >= MAX_HORA:
        raise SinCuota(f'tope de {MAX_HORA} consultas/hora al modelo')
    with _lock:
        _uso.append(time.time())
    cuerpo = {'model': MODELO, 'stream': False, 'format': 'json', 'options': {'temperature': temperatura},
              'messages': [{'role': 'user', 'content': texto}]}
    req = urllib.request.Request(OLLAMA + '/api/chat', data=json.dumps(cuerpo).encode(), headers={'content-type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = json.loads(r.read().decode('utf-8', 'replace'))['message']['content']
    m = re.search(r'\{.*\}', txt, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}
