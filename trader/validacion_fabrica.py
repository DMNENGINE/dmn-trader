"""Cortes registrados una vez; la seleccion no consume la cohorte prospectiva."""
import time
import json
from . import db

VERSION='gmgn-reservada-20261024-3'
CLAVE='fabrica_validacion_v3'


def politica():return db.ajuste(CLAVE) or {}


def es_valida(policy):
    if not isinstance(policy,dict) or policy.get('version')!=VERSION:return False
    values=[policy.get(k) for k in ('entreno_hasta','seleccion_hasta','reservado_desde')]
    return all(isinstance(x,int) and not isinstance(x,bool) and x>=0 for x in values) and values[0]<values[1]<=values[2]


def preparar(manuales,version_regla,version_datos=None):
    with db.transaccion():
        policy=politica()
        if policy and not es_valida(policy):raise ValueError('politica de validacion invalida; conservar y revisar')
        if not policy:
            rows=db.q("SELECT ts,origen,f FROM fab_muestras WHERE lista=1 ORDER BY ts")
            rows=[row for row in rows if row['origen']=='hist' or (version_datos and json.loads(row['f']).get('version_temporal')==version_datos)]
            if len(rows)>=300:
                split=rows[int((len(rows)-1)*.7)]['ts'];end=rows[-1]['ts']+1
                policy={'version':VERSION,'entreno_hasta':split,'seleccion_hasta':end,'reservado_desde':int(time.time()),'reglas_probadas':0,'reglas_registradas':len(manuales)}
                if es_valida(policy):db.poner_ajuste(CLAVE,policy)
        registry=db.ajuste('fabrica_registro_reglas_v3') or {}
        for i,rule in enumerate(manuales,1):
            key='M'+str(i);fingerprint=version_regla(rule);previous=registry.get(key)
            if not previous or previous['version']!=fingerprint:
                history=(previous.get('historial',[])+[{'version':previous['version'],'desde':previous['desde']}]) if previous else []
                registry[key]={'version':fingerprint,'desde':int(time.time()),'historial':history}
        db.poner_ajuste('fabrica_registro_reglas_v3',registry)


def registrar_busqueda(cuantas):
    with db.transaccion():
        policy=politica()
        if policy:
            policy['reglas_probadas']=policy.get('reglas_probadas',0)+int(cuantas)
            db.poner_ajuste(CLAVE,policy)


def inicio_manual(key,rule,version_regla):
    row=(db.ajuste('fabrica_registro_reglas_v3') or {}).get(key)
    if not row or row['version']!=version_regla(rule):return None
    return max(row['desde'],politica().get('reservado_desde',row['desde']))


def diversidad(ops,D):
    days={};tokens={}
    for ts,result,index in ops:
        day=ts//86400;days.setdefault(day,[]).append(result)
        tk=int(D.tok[index]);tokens[tk]=tokens.get(tk,0)+1
    n=len(ops)
    return {'dias':len(days),'tokens':len(tokens),'concentracion_maxima':max(tokens.values())/n if n else 1.0,'dias_positivos':sum(sum(v)>0 for v in days.values()),'peor_dia_pct':min((sum(v)/len(v) for v in days.values()),default=None)}
