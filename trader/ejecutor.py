"""Propietario fijo por cuenta. Sin caducidad, eleccion automatica ni toma de control remota."""
from pathlib import Path
import hashlib,hmac,json,os,re,secrets,threading,urllib.request,urllib.parse

_mutex=threading.RLock()
_locks={}


class Bloqueado(ValueError):
    """Denegado antes de llamar al broker: se sabe que no hubo envio."""


def directorio():
    return Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share')))/'DMNTrader/control-ejecucion'


def identidad():
    if os.name=='nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SOFTWARE\Microsoft\Cryptography',0,winreg.KEY_READ|winreg.KEY_WOW64_64KEY) as k:
            machine=winreg.QueryValueEx(k,'MachineGuid')[0]
    else:
        machine=Path('/etc/machine-id').read_text().strip()
    if not isinstance(machine,str) or len(machine.strip())<16:
        raise ValueError('identidad del equipo no verificable')
    return hashlib.sha256(('dmn-ejecutor/1|'+machine.strip()+'|'+str(Path.home().resolve())).encode()).hexdigest()


def _ambito(proveedor,cuenta):
    if proveedor not in ('gmgn-sol','ninjatrader') or not isinstance(cuenta,str) or not cuenta or len(cuenta)>160 or any(ord(c)<32 for c in cuenta):
        raise ValueError('ambito de ejecucion invalido')
    return proveedor+'|'+cuenta


def _hex(x):
    return isinstance(x,str) and re.fullmatch('[0-9a-f]{64}',x) is not None


def _firma(token,cuerpo):
    return hmac.new(token.encode(),json.dumps(cuerpo,sort_keys=True,separators=(',',':')).encode(),hashlib.sha256).hexdigest()


def verificar(b):
    """Endpoint de solo verificacion. La asignacion no puede cambiarse por HTTP."""
    if not isinstance(b,dict):raise ValueError('solicitud invalida')
    scope=_ambito(b.get('proveedor'),b.get('cuenta'))
    node,nonce=b.get('nodo'),b.get('nonce')
    if not _hex(node) or not _hex(nonce):raise ValueError('identidad o nonce invalido')
    token=os.environ.get('DMN_TRADER_TOKEN','')
    if not token:raise ValueError('autoridad sin autenticacion')
    try:policy=json.loads((directorio()/'autoridad.json').read_text(encoding='utf-8'))
    except Exception:raise ValueError('autoridad no configurada; ejecucion bloqueada') from None
    aid=policy.get('id')
    if not _hex(aid) or not isinstance(policy.get('propietarios'),dict):raise ValueError('politica de autoridad invalida')
    owner=policy['propietarios'].get(scope)
    if owner is not None and not _hex(owner):raise ValueError('propietario invalido')
    result=dict(contrato='dmn-ejecutor/1',autoridad=aid,ambito=scope,nodo=node,nonce=nonce,permitido=owner==node)
    return {**result,'firma':_firma(token,result)}


class _SinRedireccion(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError('redireccion de autoridad no permitida')


def cliente():
    explicit=tuple(os.environ.get(k,'') for k in ('DMN_EJECUTOR_URL','DMN_EJECUTOR_AUTORIDAD','DMN_EJECUTOR_TOKEN'))
    if any(explicit):return explicit
    try:
        cfg=json.loads((directorio()/'cliente.json').read_text(encoding='utf-8'))
        url,aid=cfg['url'],cfg['autoridad']
        central=os.environ.get('DMN_TRADER_CENTRAL','').rstrip('/')
        if not isinstance(url,str) or central!=url.rstrip('/'):
            raise ValueError()
        # Reutilizar el secreto ya configurado para esta misma central; no copiarlo a cliente.json.
        return url,aid,os.environ.get('DMN_TRADER_CENTRAL_TOKEN','')
    except Exception:
        raise ValueError('conexion de autoridad no configurada o distinta de la central') from None


def autorizar(proveedor,cuenta):
    scope=_ambito(proveedor,cuenta)
    url,aid,token=cliente()
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/') or not _hex(aid) or not token:
        raise ValueError('ejecutor real sin autoridad configurada; envio bloqueado')
    node=identidad();nonce=secrets.token_hex(32)
    body=dict(proveedor=proveedor,cuenta=cuenta,nodo=node,nonce=nonce)
    req=urllib.request.Request(url.rstrip('/')+'/api/ejecutor/verificar',data=json.dumps(body).encode(),headers={'x-token':token,'Content-Type':'application/json'},method='POST')
    try:
        # No usar proxies heredados ni seguir redirecciones con el token de la autoridad.
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_SinRedireccion())
        with opener.open(req,timeout=5) as response:
            if response.status!=200:raise ValueError('respuesta de autoridad invalida')
            raw=response.read(16385)
            if len(raw)>16384:raise ValueError('respuesta de autoridad demasiado grande')
            result=json.loads(raw)
        if not isinstance(result,dict):raise ValueError('respuesta invalida')
        signature=result.pop('firma',None)
        expected=dict(contrato='dmn-ejecutor/1',autoridad=aid,ambito=scope,nodo=node,nonce=nonce,permitido=True)
        if result!=expected or not isinstance(signature,str) or not hmac.compare_digest(signature,_firma(token,result)):
            raise ValueError('propietario no autorizado o respuesta no verificable')
    except Exception:
        raise ValueError('no se confirma ejecutor unico; envio real bloqueado') from None


def _retener(proveedor,cuenta):
    scope=_ambito(proveedor,cuenta)
    with _mutex:
        # Se retiene hasta que termine el proceso: no liberar por timeout o desconexion.
        if scope in _locks:return
        root=directorio();root.mkdir(parents=True,exist_ok=True)
        f=(root/(hashlib.sha256(scope.encode()).hexdigest()+'.lock')).open('a+b')
        try:
            if os.name=='nt':
                import msvcrt
                f.seek(0,2)
                if f.tell()==0:f.write(b'0');f.flush()
                f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except Exception:
            f.close();raise ValueError('otro proceso conserva esta cuenta; envio bloqueado') from None
        _locks[scope]=f


def ejecutar(proveedor,cuenta,accion):
    # El bloqueo de proceso se toma antes de consultar la autoridad y enviar.
    try:
        _retener(proveedor,cuenta)
        autorizar(proveedor,cuenta)
    except ValueError as ex:
        raise Bloqueado(str(ex)) from None
    with _mutex:
        return accion()
