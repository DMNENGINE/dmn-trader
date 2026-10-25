"""Modo sombra, conciliacion e informe diario. Solo usa consultas de lectura."""
import time,json,math,threading,datetime,hashlib
from decimal import Decimal
from . import db,real,supervision_gmgn as S,contabilidad_gmgn as C,rpc_lectura as RPC

_hilo=None

def preparar():
    C.preparar()
    db.ex('CREATE TABLE IF NOT EXISTS gmgn_decisiones (clave TEXT PRIMARY KEY,ts INTEGER NOT NULL,tipo TEXT NOT NULL,regla TEXT,token TEXT,motivo TEXT NOT NULL,datos TEXT NOT NULL)')
    db.ex('CREATE INDEX IF NOT EXISTS ix_gmgn_decisiones_ts ON gmgn_decisiones(ts)')
    db.ex('CREATE TABLE IF NOT EXISTS gmgn_sombra (clave TEXT PRIMARY KEY,wallet TEXT NOT NULL,token TEXT NOT NULL,regla TEXT NOT NULL,creada INTEGER NOT NULL,vence INTEGER NOT NULL,proxima INTEGER NOT NULL,estado TEXT NOT NULL,datos TEXT NOT NULL)')
    db.ex('CREATE TABLE IF NOT EXISTS gmgn_informes (dia TEXT PRIMARY KEY,actualizado INTEGER NOT NULL,datos TEXT NOT NULL)')

def decision(tipo,regla='',token='',motivo='',datos=None,clave=None):
    preparar();now=int(time.time())
    # No almacenar mensajes arbitrarios del proveedor, claves ni respuestas completas.
    allowed={'importe_lamports','vuelta_minima_lamports','vuelta_esperada_lamports','esperado_atomico','minimo_atomico','horizonte_min','error_tipo','precio','tp','sl','resultado_cotizado_pct','brecha_segundos'}
    safe={k:v for k,v in (datos or {}).items() if k in allowed}
    if clave is None:clave=hashlib.sha256(json.dumps([tipo,regla,token,motivo,now//300,safe],sort_keys=True,allow_nan=False).encode()).hexdigest()
    db.ex('INSERT OR IGNORE INTO gmgn_decisiones VALUES(?,?,?,?,?,?,?)',(clave,now,tipo,str(regla)[:100],str(token)[:100],str(motivo)[:160],json.dumps(safe,allow_nan=False)))

def evaluar_senal(wallet,b,config):
    from .mercado_gmgn import cotizar
    from .presupuesto_gmgn import presupuesto
    preparar();now=int(time.time());rule=str(b['est_id']);token=b['token']
    key=hashlib.sha256(json.dumps([wallet,token,rule,now//300]).encode()).hexdigest()
    if db.uno('SELECT 1 FROM gmgn_sombra WHERE clave=?',(key,)):return
    amount=presupuesto(config)['importe_lamports'];hold=b.get('hold')
    if isinstance(hold,bool) or not isinstance(hold,int) or not 1<=hold<=288:raise ValueError('horizonte invalido')
    if isinstance(b.get('precio'),bool) or not isinstance(b.get('precio'),(int,float)) or not math.isfinite(b['precio']) or b['precio']<=0:raise ValueError('precio invalido')
    data={'importe_lamports':str(amount),'precio':b['precio'],'tp':b['tp'],'sl':b['sl'],'horizonte_min':hold*5}
    # Persistir antes de consultar: reiniciar no repite una muestra ni inventa su precio.
    db.ex("INSERT OR IGNORE INTO gmgn_sombra VALUES(?,?,?,?,?,?,?,'consultando',?)",(key,wallet,token,rule,now,now+hold*300,now+hold*300,json.dumps(data)))
    try:
        buy=cotizar(wallet,real.SOL,token,amount,10)
        sell=cotizar(wallet,token,real.SOL,buy['minimo'],15)
        if time.monotonic()-buy['ts']>30:raise ValueError('cotizaciones demasiado separadas')
        data.update(esperado_atomico=str(buy['esperado']),minimo_atomico=str(buy['minimo']),vuelta_minima_lamports=str(sell['minimo']),vuelta_esperada_lamports=str(sell['esperado']))
        state='observando';reason='cotizacion_de_ida_y_vuelta_sin_ejecutar'
        if Decimal(amount-sell['minimo'])/Decimal(10**9)>Decimal(str(config['perdida_max_sol'])):
            state='rechazada';reason='suelo_de_ruta_excede_presupuesto'
        db.ex('UPDATE gmgn_sombra SET estado=?,datos=? WHERE clave=?',(state,json.dumps(data,allow_nan=False),key))
        decision('sombra_aceptada' if state=='observando' else 'sombra_rechazada',rule,token,reason,data,key)
    except Exception as ex:
        db.ex("UPDATE gmgn_sombra SET estado='sin_cotizacion' WHERE clave=?",(key,))
        decision('sombra_rechazada',rule,token,'cotizacion_no_disponible',{'error_tipo':type(ex).__name__},key)

def completar(maximo=2):
    from .mercado_gmgn import cotizar
    preparar();now=int(time.time())
    # Una interrupcion durante consulta no se reinterpreta como observacion exitosa.
    db.ex("UPDATE gmgn_sombra SET estado='interrumpida' WHERE estado='consultando' AND creada<?",(now-300,))
    for row in db.q("SELECT * FROM gmgn_sombra WHERE estado='observando' AND proxima<=? ORDER BY vence LIMIT ?",(now,maximo)):
        data=json.loads(row['datos']);gap=now-row['vence']
        if gap>300:
            db.ex("UPDATE gmgn_sombra SET estado='horizonte_perdido' WHERE clave=?",(row['clave'],));decision('sombra_incompleta',row['regla'],row['token'],'horizonte_perdido',{'brecha_segundos':gap},row['clave']+'-fin');continue
        try:
            quote=cotizar(row['wallet'],row['token'],real.SOL,int(data['minimo_atomico']),15)
            result=(quote['esperado']/int(data['importe_lamports'])-1)*100
            data.update(salida_cotizada_lamports=str(quote['esperado']),salida_minima_lamports=str(quote['minimo']),resultado_cotizado_pct=result,cotizada_ts=now,brecha_segundos=gap)
            # Control simple a vencimiento, distinto del simulador TP/SL: solo fotos ya disponibles.
            if db.uno("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fab_muestras'"):
                photo=db.uno("SELECT precio,ts FROM fab_muestras WHERE token=? AND origen='vivo' AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1",(row['token'],now-150,now))
                if photo and isinstance(photo['precio'],(int,float)) and math.isfinite(photo['precio']) and photo['precio']>0:
                    from . import fabrica as F
                    data.update(control_hold_pct=(photo['precio']/data['precio']-1)*100-F.COSTES,control_coste_supuesto_pct=F.COSTES,control_foto_ts=photo['ts'])
            db.ex("UPDATE gmgn_sombra SET estado='completa',datos=? WHERE clave=?",(json.dumps(data,allow_nan=False),row['clave']))
            decision('sombra_completa',row['regla'],row['token'],'resultado_cotizado_no_es_ejecucion_ni_neto',data,row['clave']+'-fin')
        except Exception as ex:
            db.ex('UPDATE gmgn_sombra SET proxima=? WHERE clave=?',(now+60,row['clave']))
            decision('sombra_error',row['regla'],row['token'],'salida_no_cotizable',{'error_tipo':type(ex).__name__})

def escanear(wallet,config):
    from . import fabrica as F
    preparar();now=int(time.time());last=db.ajuste('gmgn_sombra_scan') or 0
    if now-last<300:return
    db.poner_ajuste('gmgn_sombra_scan',now)
    # Rotar las reglas para que el limite de consultas no privilegie siempre M1.
    rules=[{**m,'id':f'M{i+1}','vivo':{}} for i,m in enumerate(F.MANUALES)]
    offset=(now//300)%len(rules);rules=rules[offset:]+rules[:offset]
    candidates=F.senales_ahora(rules,max_velas=3)
    decision('escaneo',motivo='sin_senal' if not candidates else 'senales_solo_sombra')
    room=max(0,30-db.uno("SELECT COUNT(*) n FROM gmgn_sombra WHERE estado IN ('observando','consultando')")['n'])
    for b in candidates[:min(3,room)]:evaluar_senal(wallet,b,config)

def conciliar(o,broker_tokens=None):
    preparar();wallet,oid,token=o['wallet'],C.identificador(o),o['token'];now=int(time.time())
    result={'ts':now,'wallet':wallet,'operacion':oid,'estado':'pendiente','motivo':'esperando_consulta','solo_lectura':True}
    try:
        chain=RPC.tokens(wallet,token)
        if broker_tokens is None:
            from . import onchain
            balances=onchain._api('/v1/user/wallet_token_balance',chain='sol',wallet_address=wallet,token_address=token)['balances']
            broker_tokens=balances[0]['balance']
        d=chain['decimales'];balance=Decimal(str(broker_tokens))
        if not balance.is_finite() or balance<0:raise ValueError('saldo broker invalido')
        if d is None:
            match=balance==0 and chain['atomico']=='0'
        else:
            expected=Decimal(chain['atomico'])/(Decimal(10)**d)
            # GMGN puede devolver float: explicitar la tolerancia, nunca usarla para vender cantidades.
            tolerance=max(Decimal(1)/(Decimal(10)**d),abs(expected)*Decimal('1e-12'))
            match=abs(expected-balance)<=tolerance
        result.update(saldo_atomico_cadena=chain['atomico'],slot=chain['slot'],estado='coincide' if match else 'discrepancia',motivo='saldo_token_cotejado' if match else 'saldo_token_distinto')
        if not match:S.registrar(wallet,oid,'conciliacion_cadena','Saldo de token distinto entre GMGN y la cadena.')
        signatures=[x['firma'] for x in db.q('SELECT firma FROM gmgn_recibos WHERE wallet=? AND operacion=?',(wallet,oid))][:30]
        states=RPC.firmas(signatures)
        result['firmas']=[{'firma':s,'estado':'desconocida' if v is None else v['confirmationStatus'],'fallida':None if v is None else v['err'] is not None} for s,v in zip(signatures,states)]
        if match and any(v is None or v['confirmationStatus']!='finalized' for v in states):result.update(estado='pendiente',motivo='firmas_sin_finalizar')
        if o.get('entrada_confirmada') and any(v and v['err'] is not None and s==o.get('hash_entrada') for s,v in zip(signatures,states)):
            result.update(estado='discrepancia',motivo='entrada_broker_confirmada_fallida_en_cadena');S.registrar(wallet,oid,'conciliacion_cadena','Entrada declarada confirmada pero fallida en la cadena.')
    except Exception as ex:
        result.update(estado='no_disponible',motivo='consulta_independiente_no_disponible',error_tipo=type(ex).__name__)
        S.registrar(wallet,oid,'rpc_cadena','Consulta independiente no disponible; revisar antes de nuevas entradas.')
    db.poner_ajuste('gmgn_conciliacion',result)
    return result

def movimientos(wallet):
    """Detectar nuevas firmas ajenas al libro; no atribuirlas a beneficio ni asegurar cobertura completa."""
    preparar();key='gmgn_movimientos_'+wallet;previous=db.ajuste(key) or {};now=int(time.time())
    rows=RPC.consultar('getSignaturesForAddress',[wallet,{'limit':100,'commitment':'finalized'}])
    if not isinstance(rows,list):raise ValueError('movimientos ausentes')
    if any(not isinstance(x,dict) or not isinstance(x.get('signature'),str) for x in rows):raise ValueError('movimientos invalidos')
    boundary=previous.get('ultima');known={x['firma'] for x in db.q('SELECT firma FROM gmgn_recibos WHERE wallet=?',(wallet,))};new=[];found=False
    for row in rows:
        if row['signature']==boundary:found=True;break
        if boundary and row['signature'] not in known:new.append(row['signature'])
    if new:decision('movimiento_no_atribuido',motivo='firmas_nuevas_fuera_del_libro')
    result={'ts':now,'ultima':rows[0]['signature'] if rows else boundary,'nuevas_no_atribuidas':len(new),'cobertura':'linea_base' if not boundary else 'intervalo_cubierto' if found else 'incompleta','beneficio_atribuido':False}
    db.poner_ajuste(key,result);return result

def costes(margen_objetivo_pct=1.5):
    C.preparar();rows=db.q("SELECT datos FROM gmgn_recibos WHERE estado='verificado'")
    values=[json.loads(x['datos']) for x in rows]
    operations=db.q('SELECT datos FROM gmgn_libro WHERE cerrada IS NOT NULL');closed=[C.resumen(json.loads(x['datos'])) for x in operations]
    if isinstance(margen_objetivo_pct,bool) or not isinstance(margen_objetivo_pct,(int,float)) or not math.isfinite(margen_objetivo_pct) or margen_objetivo_pct<=0:raise ValueError('margen invalido')
    fees=sorted(int(x['comision_red_lamports']) for x in values if int(x['comision_red_lamports'])>0)
    # Limite inferior orientativo: dos comisiones de red al percentil 95 observado.
    floor=(2*fees[math.ceil(.95*len(fees))-1]/1e9/(margen_objetivo_pct/100)) if fees else None
    return {'recibos':len(values),'comision_red_sol':sum(int(x['comision_red_lamports']) for x in values)/1e9 if values else None,'variacion_cuentas_token_sol':sum(int(x['renta_cuentas_lamports']) for x in values)/1e9 if values else None,'operaciones_con_recibos_cerradas':sum(x['posicion_con_recibos_cerrada'] for x in closed),'costes_completos_confirmados':False,'importe_minimo_viable_sol':None,'minimo_parcial_solo_red_sol':floor,'margen_hipotetico_pct':margen_objetivo_pct,'motivo_minimo':'el_minimo_parcial_excluye_tasas_propinas_renta_y_deslizamiento_no_demostrados','coste_simulacion_pct':6.0}

def informe(dia=None):
    preparar();date=datetime.date.fromisoformat(dia) if dia else datetime.datetime.now(datetime.timezone.utc).date()
    start=int(datetime.datetime.combine(date,datetime.time(),datetime.timezone.utc).timestamp());end=start+86400
    counts={x['tipo']:x['n'] for x in db.q('SELECT tipo,COUNT(*) n FROM gmgn_decisiones WHERE ts>=? AND ts<? GROUP BY tipo',(start,end))}
    reasons=db.q('SELECT motivo,COUNT(*) n FROM gmgn_decisiones WHERE ts>=? AND ts<? GROUP BY motivo ORDER BY n DESC LIMIT 12',(start,end))
    pending=db.uno("SELECT COUNT(*) n FROM gmgn_sombra WHERE estado='observando'")['n']
    quotes=[json.loads(x['datos']) for x in db.q("SELECT datos FROM gmgn_sombra WHERE estado='completa' AND creada>=? AND creada<?",(start,end))]
    compared=[x for x in quotes if 'control_hold_pct' in x]
    return {'dia_utc':date.isoformat(),'decisiones':counts,'motivos':reasons,'sombra_pendientes':pending,'sombra_completas':len(quotes),'media_cotizada_pct':sum(x['resultado_cotizado_pct'] for x in quotes)/len(quotes) if quotes else None,'comparaciones_control_hold':len(compared),'media_control_hold_pct':sum(x['control_hold_pct'] for x in compared)/len(compared) if compared else None,'control_no_simula_tp_sl':True,'cotizaciones_no_son_ejecuciones':True,'costes_acumulados':costes(),'conciliacion':db.ajuste('gmgn_conciliacion'),'validacion':db.ajuste('fabrica_validacion_v3'),'ultimas':db.q('SELECT ts,tipo,regla,token,motivo FROM gmgn_decisiones WHERE ts>=? AND ts<? ORDER BY ts DESC LIMIT 20',(start,end))}

def _bucle():
    from . import piloto as P
    preparar()
    while True:
        try:
            e=P._est();wallet=real._wallet()
            if wallet:
                o=e.get('orden')
                # Fallar una fuente no impide consultar las demas ni completar muestras pendientes.
                errors=[]
                tasks=[('conciliacion',lambda:conciliar(o))] if o and not e.get('intencion_entrada') else []
                # Evitar que la investigacion consuma cuotas del broker mientras se gestiona riesgo real.
                if not o and not e.get('intencion_entrada'):
                    tasks.extend([('completar',completar),('escanear',lambda:escanear(wallet,P.cfg()))])
                if time.time()-(db.ajuste('gmgn_movimientos_ultimo') or 0)>=300:
                    db.poner_ajuste('gmgn_movimientos_ultimo',int(time.time()));tasks.append(('movimientos',lambda:movimientos(wallet)))
                for name,fn in tasks:
                    try:
                        result=fn()
                        if name=='conciliacion' and result['estado'] in ('no_disponible','discrepancia'):errors.append({'tarea':name,'tipo':result['estado']})
                    except Exception as ex:errors.append({'tarea':name,'tipo':type(ex).__name__})
                today=datetime.datetime.now(datetime.timezone.utc).date()
                for day in (today-datetime.timedelta(days=1),today):
                    report=informe(day.isoformat());db.ex('INSERT INTO gmgn_informes VALUES(?,?,?) ON CONFLICT(dia) DO UPDATE SET actualizado=excluded.actualizado,datos=excluded.datos',(day.isoformat(),int(time.time()),json.dumps(report,allow_nan=False)))
                db.poner_ajuste('gmgn_observatorio_servicio',{'ts':int(time.time()),'estado':'consulta_pendiente' if errors else 'activo','errores':errors,'solo_lectura':True})
            else:
                db.poner_ajuste('gmgn_observatorio_servicio',{'ts':int(time.time()),'estado':'esperando_wallet','solo_lectura':True})
        except Exception as ex:
            db.poner_ajuste('gmgn_observatorio_servicio',{'ts':int(time.time()),'estado':'consulta_pendiente','error_tipo':type(ex).__name__,'solo_lectura':True})
        time.sleep(60)

def arrancar():
    global _hilo
    if not (_hilo and _hilo.is_alive()):
        _hilo=threading.Thread(target=_bucle,daemon=True,name='gmgn-observatorio');_hilo.start()
