"""Entrada a mercado: una intencion, un swap, confirmacion y proteccion verificadas por separado."""
import json,time,math
from decimal import Decimal,InvalidOperation
from . import db,real,ejecutor,contabilidad_gmgn as CG,supervision_gmgn as SG
from .presupuesto_gmgn import EntradaNoEnviada,presupuesto,PRIORIDAD,PROPINA


def entero(value,positive=True):
    if not isinstance(value,str) or not value.isascii() or not value.isdigit():raise ValueError('cantidad atomica ausente o invalida')
    n=int(value)
    if n>2**256-1 or (positive and n<=0):raise ValueError('cantidad atomica fuera de rango')
    return n


def leer_orden(x):
    """`order get` normalizado. Formato actual: status + report{input/output_*}; el antiguo: campos arriba y confirmation.state.
    Sin informe (pendiente o fallida) los tokens y cantidades quedan en None: desconocidos, no cero."""
    if not isinstance(x,dict):return None
    r=x['report'] if isinstance(x.get('report'),dict) else {}
    return {'order_id':x.get('order_id'),'hash':x.get('hash'),'estado':x.get('status') or (x.get('confirmation') or {}).get('state'),
            'input_token':r.get('input_token',x.get('input_token')),'output_token':r.get('output_token',x.get('output_token')),
            'entrada':r.get('input_amount',x.get('filled_input_amount')),'salida':r.get('output_amount',x.get('filled_output_amount')),
            'strategy_order_id':x.get('strategy_order_id')}


def en_ambito(x,oid,entra,sale):
    """La respuesta es de esta orden; los tokens, si vienen, son los esperados."""
    return bool(x) and x['order_id']==oid and x['input_token'] in (None,entra) and x['output_token'] in (None,sale)


def cotizar(wallet,input_token,output_token,amount,slippage):
    q=real._cli('order','quote','--chain','sol','--from',wallet,'--input-token',input_token,'--output-token',output_token,'--amount',str(amount),'--slippage',str(slippage))
    if not isinstance(q,dict) or q.get('input_token')!=input_token or q.get('output_token')!=output_token or entero(q.get('input_amount'))!=amount:
        raise EntradaNoEnviada('cotizacion no corresponde a esta entrada')
    try:
        minimum=entero(q.get('min_output_amount'));output=entero(q.get('output_amount'))
        slip=q.get('slippage')
        if minimum>output or isinstance(slip,bool) or not isinstance(slip,(int,float)) or not math.isfinite(slip) or not 0<=slip<=slippage:raise ValueError()
    except (ValueError,TypeError):raise EntradaNoEnviada('cotizacion incompleta o fuera de tolerancia') from None
    return {'minimo':minimum,'esperado':output,'ts':time.monotonic()}


def abrir(e,w,b,precio):
    from . import piloto as P
    config=P._autorizar_entrada(e,w)
    for value in (precio,b.get('tp'),b.get('sl')):
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
            raise EntradaNoEnviada('precio o protecciones invalidos')
    if b['sl']>=100:raise EntradaNoEnviada('stop de mercado fuera de rango')
    amount=presupuesto(config)['importe_lamports']
    if P._tokens(w,b['token'])!=0:raise EntradaNoEnviada('token preexistente en la wallet; no mezclar cantidades')
    quote=cotizar(w,real.SOL,b['token'],amount,10)
    reverse=cotizar(w,b['token'],real.SOL,quote['minimo'],15)
    # No aceptar una ruta cuyo suelo de ida/vuelta ya excede el freno total configurado.
    if Decimal(amount-reverse['minimo'])/1000000000>Decimal(str(config['perdida_max_sol'])):
        raise EntradaNoEnviada('ruta de entrada/salida fuera del presupuesto de perdida')
    def vigente():
        balance,current_wallet=P._saldo()
        if current_wallet!=w or not math.isfinite(balance) or Decimal(str(balance))*1000000000<presupuesto(config)['necesario_lamports'] or Decimal(str(e['saldo_inicio']))-Decimal(str(balance))>=Decimal(str(config['perdida_max_sol'])):
            raise EntradaNoEnviada('saldo o presupuesto modificados antes del envio')
        if P._tokens(w,b['token'])!=0:
            raise EntradaNoEnviada('token preexistente antes del envio; no mezclar cantidades')
        if P.cfg()!=config or time.monotonic()-quote['ts']>10:
            raise EntradaNoEnviada('configuracion cambiada o cotizacion vencida')
    vigente()
    e['intencion_entrada']={'modo':'mercado','wallet':w,'token':b['token'],'cantidad_lamports':str(amount),'ts':int(time.time()),'estado':'enviando'}
    db.poner_ajuste(P.EST,e)
    conditions=[dict(order_type='profit_stop',side='sell',price_scale=str(b['tp']),sell_ratio='100'),dict(order_type='loss_stop',side='sell',price_scale=str(b['sl']),sell_ratio='100')]
    try:
        response=real._cli('swap','--chain','sol','--from',w,'--input-token',real.SOL,'--output-token',b['token'],'--amount',str(amount),'--slippage','10','--min-output',str(quote['minimo']),'--priority-fee',PRIORIDAD,'--tip-fee',PROPINA,'--anti-mev','--condition-orders',json.dumps(conditions,separators=(',',':')),'--sell-ratio-type','buy_amount',preflight=vigente,timeout=60)
    except (ejecutor.Bloqueado,EntradaNoEnviada):
        e.pop('intencion_entrada',None);db.poner_ajuste(P.EST,e);raise
    # Una respuesta incompleta no autoriza repetir el swap.
    if not isinstance(response,dict) or not isinstance(response.get('order_id'),str) or not response['order_id']:
        raise RuntimeError('entrada sin identificador; conciliar antes de repetir')
    oid=response['order_id']
    e['orden']={**b,'id':oid,'id_entrada':oid,'modo':'mercado','wallet':w,'creada':int(time.time()),'compra':precio,'sol':amount/1e9,'cantidad_lamports':str(amount),'saldo_antes':e.get('saldo',e.get('saldo_inicio',0)),'estado':'pendiente_confirmacion_entrada','estrategia_propuesta':response.get('strategy_order_id')}
    e.pop('intencion_entrada',None);CG.operacion(e['orden']);db.poner_ajuste(P.EST,e)
    from . import observatorio_gmgn as O
    O.decision('entrada_enviada', b.get('est_id', ''), b['token'], 'broker_recibio_identificador', {'importe_lamports': str(amount)}, 'entrada-'+oid)
    return oid,precio,b['tp'],b['sl']


def bloquear(e,o,motivo):
    from . import piloto as P
    o['estado']='pendiente_conciliacion';o['incidencia']=motivo
    o['proteccion_verificada']=False
    e['error']=motivo
    SG.registrar(o['wallet'],CG.identificador(o),'proteccion',motivo)
    # Pausar nuevas entradas no suspende el seguimiento de la existente.
    if P.cfg().get('activo'):
        P.poner_cfg({'activo':False})
    db.poner_ajuste(P.EST,e)


def proteccion_valida(x,o,hash_compra,cantidad,tipo):
    from . import piloto as P
    # GMGN puede dejar open_sign_hash vacío en estrategias adjuntas al swap: entonces manda la cantidad exacta comprada
    if x.get('open_sign_hash') not in (hash_compra,'') or entero((x.get('order_statistic') or {}).get('buy_amount'))!=cantidad:
        return False
    if tipo=='history':return x.get('status')=='closed'
    if x.get('status')!='open' or x.get('strategy_status')!='running':return False
    conditions=x.get('condition_orders')
    if not isinstance(conditions,list):return False
    for kind,expected in (('profit_stop',o['tp']),('loss_stop',o['sl'])):
        hits=[c for c in conditions if c.get('order_type')==kind and c.get('side')=='sell' and c.get('status')=='check']
        if len(hits)!=1:return False
        try:
            if Decimal(str(hits[0].get('price_scale')))!=Decimal(str(expected)) or Decimal(str(hits[0].get('sell_ratio')))!=100:return False
        except InvalidOperation:return False
    return True


def buscar_por_cantidad(o,hash_compra,cantidad,grupo):
    """Antes de comprar se exige saldo cero del token, así que solo una estrategia válida puede ser la nuestra; con dos, ninguna."""
    for tipo in ('open','history'):
        pagina=real._cli('order','strategy','list','--chain','sol','--group-tag',grupo,'--type',tipo,'--limit','100','--from',o['wallet'],'--base-token',o['token'])
        if not isinstance(pagina,dict) or not isinstance(pagina.get('list'),list):raise ValueError('respuesta de ordenes incompleta')
        validas=[x for x in pagina['list'] if x.get('wallet_address') in (None,o['wallet']) and x.get('base_token') in (None,o['token'])
                 and isinstance(x.get('order_id'),str) and x['order_id'] and proteccion_valida(x,o,hash_compra,cantidad,tipo)]
        if len(validas)==1:return tipo,validas[0]
        if validas:return tipo,None
    return None,None


def seguir(e,c):
    from . import piloto as P
    o=e['orden']
    try:
        if o.get('entrada_confirmada') and o.get('grupo_proteccion'):
            tipo,strategy=P._buscar(o['id'],o['wallet'],o['token'],grupo=o['grupo_proteccion'])
            if strategy and (o.get('salida') or o.get('salida_solicitada')):
                # el piloto retira el TP/SL para vender: que siga la salida, no es una protección perdida
                o['proteccion_verificada']=False
                return P._seguir_estrategia(e,c,conciliada=(tipo,strategy))
            if not strategy or not proteccion_valida(strategy,o,o['hash_entrada'],entero(o['cantidad_comprada_atomica']),tipo):
                return bloquear(e,o,'proteccion perdida o no verificable durante el seguimiento')
            o['proteccion_verificada']=tipo=='open'
            return P._seguir_estrategia(e,c,conciliada=(tipo,strategy))
        x=leer_orden(real._cli('order','get','--chain','sol','--order-id',o['id_entrada']))
        if not en_ambito(x,o['id_entrada'],real.SOL,o['token']):
            return bloquear(e,o,'respuesta de entrada fuera de ambito')
        confirmation=x['estado']
        if confirmation in ('failed','expired'):
            # Una orden fallida puede gastar gas: no se inventa PnL cero.
            if any(v not in (None,'0','') for v in (x['salida'],x['entrada'])) or P._tokens(o['wallet'],o['token'])!=0:
                return bloquear(e,o,'entrada fallida con saldo o ejecucion no conciliados')
            op={**o,'cerrada':int(time.time()),'resultado':'fallo_compra','pnl_sol':None,'pnl_confirmado':False}
            CG.operacion(op)
            if x.get('hash'):CG.anotar(op,x['hash'],'fallo')
            e['ops']=(e.get('ops') or [])[-199:]+[op];e['orden']=None;db.poner_ajuste(P.EST,e);return
        if confirmation!='confirmed':
            o['estado']='pendiente_confirmacion_entrada';db.poner_ajuste(P.EST,e);return
        if x['input_token']!=real.SOL or x['output_token']!=o['token']:
            return bloquear(e,o,'entrada confirmada sin informe de ejecucion')
        bought=entero(x['salida']);spent=entero(x['entrada'])
        if spent>entero(o['cantidad_lamports']) or not isinstance(x['hash'],str) or not x['hash']:
            return bloquear(e,o,'ejecucion de entrada no verificable')
        o['cantidad_comprada_atomica']=str(bought);o['hash_entrada']=x['hash'];o['entrada_confirmada']=True
        CG.anotar(o,x['hash'],'entrada')
        sid=x['strategy_order_id'] or o.get('estrategia_propuesta')
        for group in ('STMix','LimitOrder'):
            if isinstance(sid,str) and sid:
                tipo,strategy=P._buscar(sid,o['wallet'],o['token'],grupo=group)
            else:  # el swap no devolvió el id: la única estrategia de este token con la cantidad exacta comprada
                tipo,strategy=buscar_por_cantidad(o,x['hash'],bought,group)
            if strategy and proteccion_valida(strategy,o,x['hash'],bought,tipo):
                o.update(id=strategy['order_id'],grupo_proteccion=group,proteccion_verificada=tipo=='open',estado='protegida' if tipo=='open' else 'cierre_en_conciliacion')
                db.poner_ajuste(P.EST,e)
                return P._seguir_estrategia(e,c,conciliada=(tipo,strategy))
        bloquear(e,o,'protecciones de entrada no verificadas; revisar antes de reanudar')
    except Exception as ex:
        o['estado']='pendiente_conciliacion';o['proteccion_verificada']=False;e['error']='conciliacion de mercado: '+type(ex).__name__;SG.registrar(o['wallet'],CG.identificador(o),'consulta','No se pudo verificar la posicion o sus protecciones.');db.poner_ajuste(P.EST,e)
