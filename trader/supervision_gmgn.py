"""Incidencias locales persistentes. No activa pilotos ni envia mensajes externos."""
import time
from . import db


def preparar():
    db.ex('CREATE TABLE IF NOT EXISTS gmgn_incidencias (wallet TEXT NOT NULL,operacion TEXT NOT NULL,codigo TEXT NOT NULL,motivo TEXT NOT NULL,critica INTEGER NOT NULL,creada INTEGER NOT NULL,ultima INTEGER NOT NULL,resuelta INTEGER,PRIMARY KEY(wallet,operacion,codigo))')


def registrar(wallet,operacion,codigo,motivo,critica=True):
    preparar();now=int(time.time())
    db.ex('INSERT INTO gmgn_incidencias VALUES(?,?,?,?,?,?,?,NULL) ON CONFLICT(wallet,operacion,codigo) DO UPDATE SET ultima=excluded.ultima,motivo=excluded.motivo,resuelta=NULL,critica=excluded.critica',
          (wallet or '',operacion or '',codigo,str(motivo)[:240],int(critica),now,now))


def resolver_posicion(wallet,operacion):
    preparar()
    # Un cierre no resuelve una discrepancia de contabilidad.
    db.ex("UPDATE gmgn_incidencias SET resuelta=? WHERE wallet=? AND operacion=? AND codigo IN ('proteccion','consulta','salida')",(int(time.time()),wallet,operacion))


def resolver_todas():
    """Revisión manual desde el panel: cierra todas las incidencias abiertas. Devuelve cuántas."""
    preparar()
    n=(db.uno('SELECT COUNT(*) n FROM gmgn_incidencias WHERE resuelta IS NULL') or {'n':0})['n']
    db.ex('UPDATE gmgn_incidencias SET resuelta=? WHERE resuelta IS NULL',(int(time.time()),))
    return n


def admitir(wallet):
    preparar()
    if db.uno('SELECT 1 FROM gmgn_incidencias WHERE wallet=? AND critica=1 AND resuelta IS NULL LIMIT 1',(wallet,)):
        raise ValueError('incidencia critica pendiente de conciliar; entradas bloqueadas')


def estado(e,c,fabrica=None):
    preparar();inc=db.q('SELECT codigo,motivo,creada,ultima,critica FROM gmgn_incidencias WHERE resuelta IS NULL ORDER BY critica DESC,ultima DESC LIMIT 20')
    o=e.get('orden') or {};critical=any(x['critica'] for x in inc)
    if critical or o.get('incidencia'):
        code,reason='requiere_revision',o.get('incidencia') or inc[0]['motivo']
    elif e.get('intencion_entrada'):
        code,reason='conciliacion','Entrada sin confirmacion; no se repetira el envio.'
    elif o.get('salida') or o.get('salida_solicitada'):
        code,reason='saliendo','Esperando confirmar el cierre de la cantidad propia.'
    elif o:
        code,reason=('protegida','Posicion y protecciones verificadas.') if o.get('proteccion_verificada') else ('conciliacion','Esperando verificar ejecucion y protecciones.')
    elif not c.get('activo'):
        code,reason='apagado','Piloto real apagado; la fabrica puede seguir evaluando.'
    elif not (fabrica or {}).get('promovidas'):
        code,reason='validando','Ninguna estrategia cumple todavia la validacion requerida.'
    else:
        code,reason='esperando_senal',e.get('motivo') or 'Estrategias aptas; esperando una senal y verificaciones de entrada.'
    return {'estado':code,'motivo':reason,'requiere_revision':critical or bool(o.get('incidencia')),'incidencias':inc}
