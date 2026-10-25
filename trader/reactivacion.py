"""Espera persistente de agentes simulados: conservar riesgo antiguo y reanudar solo su sesion."""
import time
from . import db,motor,guardia

def preparar():
    db.ex('CREATE TABLE IF NOT EXISTS ag_esperas (sesion TEXT NOT NULL,agente_id INTEGER NOT NULL,estado TEXT NOT NULL,creada INTEGER NOT NULL,actualizada INTEGER NOT NULL,PRIMARY KEY(sesion,agente_id))')

def anteriores(e):
    preparar()
    return db.q("SELECT a.* FROM ag_agentes a WHERE a.activo=1 OR a.id IN (SELECT agente_id FROM ag_esperas WHERE sesion=?) ORDER BY a.id",(e.get('sesion_id',''),))

def esperar(e,a):
    from . import agente as A
    preparar();now=int(time.time())
    with db.transaccion():
        A._poner(a['id'],activo=0,estado='esperando_cierre',ultimo='Espera de cierre anterior; capital e historial conservados')
        db.ex("INSERT INTO ag_esperas VALUES(?,?,'esperando',?,?) ON CONFLICT(sesion,agente_id) DO NOTHING",(e['sesion_id'],a['id'],now,now))

def cancelar(sesion):
    preparar()
    db.ex("UPDATE ag_esperas SET estado='cancelada',actualizada=? WHERE sesion=? AND estado='esperando'",(int(time.time()),sesion))

def reanudar(sesion,aid):
    from . import agente as A
    preparar()
    with db.transaccion():
        e=A.estado() or {};g=guardia.estado()
        row=db.uno("SELECT * FROM ag_esperas WHERE sesion=? AND agente_id=? AND estado='esperando'",(sesion,aid))
        if not row or not e.get('activo') or e.get('sesion_id')!=sesion:return False
        if e.get('hasta') and time.time()>=e['hasta']:return False
        if g and (g.get('sesion')!=sesion or g.get('estado')!='activo' or time.time()>=g.get('vence',0)):return False
        a=db.uno('SELECT a.* FROM ag_agentes a JOIN cuentas c ON c.id=a.cuenta_id WHERE a.id=? AND c.tipo=\'agente\'',(aid,))
        if not a or a['activo'] or motor.posiciones(a['cuenta_id'],False):return False
        if db.uno("SELECT 1 FROM ordenes WHERE cuenta_id=? AND estado IN ('pendiente','espera')",(a['cuenta_id'],)):return False
        if not A._llm(a) and len([x for x in A.agentes() if not A._llm(x)])>=e['n']:return False
        # Todo se confirma en una transaccion: intento, asignacion de riesgo, activacion y cola.
        A._nuevo_intento(e,a,A._g(a),'cierre anterior confirmado; incorporacion a la sesion')
        if g and a['cuenta_id'] not in g['cuentas']:
            g['cuentas'].append(a['cuenta_id']);g['capitalInicial']+=e['capital'];db.poner_ajuste(guardia.CLAVE,g)
        A._poner(aid,activo=1)
        db.ex("UPDATE ag_esperas SET estado='reanudada',actualizada=? WHERE sesion=? AND agente_id=?",(int(time.time()),sesion,aid))
        A._cache_mapa.update(t=0,v=None)
    return True

def procesar():
    from . import agente as A
    preparar();e=A.estado() or {};done=0
    if not e.get('activo'):return 0
    for row in db.q("SELECT agente_id FROM ag_esperas WHERE sesion=? AND estado='esperando' ORDER BY agente_id",(e.get('sesion_id',''),)):
        try:
            a=db.uno('SELECT * FROM ag_agentes WHERE id=?',(row['agente_id'],))
            if a and not motor.posiciones(a['cuenta_id'],False) and A._preparar_cierre(a,'confirmacion de cierre previo'):
                done+=bool(reanudar(e['sesion_id'],a['id']))
        except Exception as ex:
            A.log(f"espera de agente {row['agente_id']}: {type(ex).__name__}")
    return done

def estado(e):
    preparar()
    waiting=db.uno("SELECT COUNT(*) n FROM ag_esperas WHERE sesion=? AND estado='esperando'",(e.get('sesion_id',''),))['n']
    return {'esperando_cierre':waiting,'reactivacion_parcial':bool(waiting and e.get('activo'))}
