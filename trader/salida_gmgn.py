"""Conciliar un swap de salida conocido; ausencia o timeout no autorizan repetirlo."""
from . import real,contabilidad_gmgn as CG,supervision_gmgn as SG
from .mercado_gmgn import entero


def conciliar(o):
    oid=o.get('venta_orden_id')
    if not isinstance(oid,str) or not oid:
        o['venta_estado']='pendiente_conciliacion';return
    x=real._cli('order','get','--chain','sol','--order-id',oid)
    if not isinstance(x,dict) or x.get('order_id')!=oid or x.get('input_token')!=o['token'] or x.get('output_token')!=real.SOL:
        raise ValueError('respuesta de venta fuera de ambito')
    state=(x.get('confirmation') or {}).get('state')
    if state!='confirmed':
        o['venta_estado']='fallida_requiere_revision' if state in ('failed','expired') else 'pendiente_conciliacion'
        if state in ('failed','expired'):SG.registrar(o['wallet'],CG.identificador(o),'salida','Venta fallida; revisar la cantidad pendiente antes de reintentar.')
        return
    sold=entero(x.get('filled_input_amount'));proceeds=entero(x.get('filled_output_amount'));expected=entero(o['cantidad_salida_atomica'])
    if sold>expected or not isinstance(x.get('hash'),str) or not x['hash']:
        raise ValueError('venta confirmada no corresponde a cantidad propia')
    CG.anotar(o,x['hash'],'salida',esperado=str(sold))
    o.setdefault('ventas_confirmadas',[]).append({'id':oid,'hash':x['hash'],'cantidad_atomica':str(sold),'salida_lamports':str(proceeds)})
    remaining=expected-sold;o['cantidad_salida_atomica']=str(remaining)
    if remaining:
        o.pop('venta_intento',None);o.pop('venta_orden_id',None);o['venta_estado']='parcial_confirmada'
    else:
        import time
        o['vendido']=int(time.time());o['venta_estado']='confirmada'
