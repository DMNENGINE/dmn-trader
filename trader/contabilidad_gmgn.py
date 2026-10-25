"""Libro por operacion y recibos Solana. Flujos identificados no equivalen a todos los costes externos."""
import json,time,os,re,hashlib,urllib.request
from . import db,supervision_gmgn as S


def preparar():
    db.ex('CREATE TABLE IF NOT EXISTS gmgn_libro (wallet TEXT NOT NULL,operacion TEXT NOT NULL,token TEXT NOT NULL,datos TEXT NOT NULL,cerrada INTEGER,PRIMARY KEY(wallet,operacion))')
    db.ex('CREATE TABLE IF NOT EXISTS gmgn_recibos (firma TEXT PRIMARY KEY,wallet TEXT NOT NULL,operacion TEXT NOT NULL,rol TEXT NOT NULL,estado TEXT NOT NULL,datos TEXT,huella TEXT,intentos INTEGER NOT NULL DEFAULT 0,proxima INTEGER NOT NULL DEFAULT 0,error TEXT)')


def identificador(o):return o.get('id_entrada') or o.get('id')


def operacion(o):
    preparar();wallet,oid,token=o.get('wallet'),identificador(o),o.get('token')
    if not all(isinstance(x,str) and x for x in (wallet,oid,token)):return False
    with db.transaccion():
        previous=db.uno('SELECT token FROM gmgn_libro WHERE wallet=? AND operacion=?',(wallet,oid))
        if previous and previous['token']!=token:
            S.registrar(wallet,oid,'libro','Token distinto para la misma operacion.');return False
        db.ex('INSERT INTO gmgn_libro VALUES(?,?,?,?,?) ON CONFLICT(wallet,operacion) DO UPDATE SET datos=excluded.datos,cerrada=COALESCE(excluded.cerrada,gmgn_libro.cerrada)',(wallet,oid,token,json.dumps(o,allow_nan=False),o.get('cerrada')))
    return True


def anotar(o,firma,rol,esperado=None):
    preparar()
    if not operacion(o):return
    wallet,oid=o.get('wallet'),identificador(o)
    if not wallet or not oid or not firma:return
    if rol not in ('entrada','salida','fallo'):raise ValueError('rol de recibo invalido')
    if not isinstance(firma,str) or re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{64,88}',firma) is None:
        S.registrar(wallet,oid,'firma','Firma no verificable; contabilidad pendiente.',False);return
    if esperado is None and rol=='entrada':esperado=o.get('cantidad_comprada_atomica')
    with db.transaccion():
        old=db.uno('SELECT wallet,operacion,rol,datos FROM gmgn_recibos WHERE firma=?',(firma,))
        if old and (old['wallet'],old['operacion'],old['rol'])!=(wallet,oid,rol):
            S.registrar(wallet,oid,'libro','Recibo asociado a otra operacion.');return
        before=(json.loads(old['datos']) if old and old['datos'] else {}).get('esperado_atomico')
        if before is not None and esperado is not None and before!=esperado:
            S.registrar(wallet,oid,'libro','La cantidad declarada de un recibo ya registrado cambio.');return
        db.ex("INSERT OR IGNORE INTO gmgn_recibos(firma,wallet,operacion,rol,estado,datos) VALUES(?,?,?,?,'pendiente',?)",(firma,wallet,oid,rol,json.dumps({'esperado_atomico':esperado})))


def natural(n):
    if isinstance(n,bool) or not isinstance(n,int) or not 0<=n<=2**64-1:raise ValueError('entero RPC invalido')
    return n


def cantidad(s):
    if not isinstance(s,str) or re.fullmatch('[0-9]+',s) is None:raise ValueError('cantidad RPC invalida')
    n=int(s)
    if n>2**256-1:raise ValueError('cantidad fuera de rango')
    return n


def interpretar(result,firma,wallet,token,rol):
    if rol not in ('entrada','salida','fallo'):raise ValueError('rol invalido')
    if not isinstance(result,dict) or not isinstance(result.get('meta'),dict):raise ValueError('recibo ausente')
    tx=result.get('transaction') or {};meta=result['meta'];message=tx.get('message') or {}
    signatures=tx.get('signatures')
    if not isinstance(signatures,list) or not signatures or firma!=signatures[0]:raise ValueError('firma RPC principal distinta')
    keys=[x.get('pubkey') if isinstance(x,dict) else x for x in message.get('accountKeys',[])]
    before,after=meta.get('preBalances'),meta.get('postBalances')
    if not isinstance(before,list) or not isinstance(after,list):raise ValueError('saldos ausentes')
    if len(keys)<len(before):
        loaded=meta.get('loadedAddresses') or {};keys+=loaded.get('writable',[])+loaded.get('readonly',[])
    if len(keys)!=len(before) or len(before)!=len(after) or keys.count(wallet)!=1 or len(set(keys))!=len(keys):raise ValueError('cuentas RPC no conciliadas')
    before=[natural(x) for x in before];after=[natural(x) for x in after];idx=keys.index(wallet)
    if 'err' not in meta:raise ValueError('estado RPC ausente')
    fee=natural(meta.get('fee'));slot=natural(result.get('slot'));amounts=[];owned=set()
    for field in ('preTokenBalances','postTokenBalances'):
        entries=meta.get(field)
        if not isinstance(entries,list):raise ValueError('balances de token ausentes')
        total=0;seen=set()
        for entry in entries:
            if entry.get('mint')!=token:continue
            if not isinstance(entry.get('owner'),str):raise ValueError('propietario de token ausente')
            if entry['owner']!=wallet:continue
            index=natural(entry.get('accountIndex'))
            if index>=len(keys) or index in seen:raise ValueError('indice de token invalido o duplicado')
            seen.add(index)
            owned.add(index);total+=cantidad((entry.get('uiTokenAmount') or {}).get('amount'))
        amounts.append(total)
    delta=amounts[1]-amounts[0];failed=meta.get('err') is not None
    if failed:
        if delta!=0:raise ValueError('transaccion fallida con cambio de token')
    elif (rol=='entrada' and delta<=0) or (rol=='salida' and delta>=0) or rol=='fallo':raise ValueError('recibo fuera del movimiento esperado')
    return {'firma':firma,'wallet':wallet,'token':token,'rol':rol,'slot':slot,'fallida':failed,'delta_lamports':str(after[idx]-before[idx]),'delta_token_atomico':str(delta),'comision_red_lamports':str(fee if idx==0 else 0),'renta_cuentas_lamports':str(sum(after[i]-before[i] for i in owned)),'costes_externos_verificados':False}


def guardar(firma,wallet,oid,rol,parsed):
    if parsed.get('firma')!=firma or parsed.get('wallet')!=wallet or parsed.get('rol')!=rol:raise ValueError('ambito de recibo distinto')
    preparar();parsed=dict(parsed)
    with db.transaccion():
        operation=db.uno('SELECT token FROM gmgn_libro WHERE wallet=? AND operacion=?',(wallet,oid))
        if not operation or operation['token']!=parsed.get('token'):raise ValueError('recibo no corresponde a la operacion registrada')
        row=db.uno('SELECT wallet,operacion,rol,huella,datos FROM gmgn_recibos WHERE firma=?',(firma,))
        previous=(json.loads(row['datos']) if row and row['datos'] else {}).get('esperado_atomico')
        expected=parsed.get('esperado_atomico',previous)
        if expected is not None:
            if (previous is not None and expected!=previous) or abs(int(parsed['delta_token_atomico']))!=cantidad(expected):
                S.registrar(wallet,oid,'libro','Cantidad del recibo no coincide con su registro.');return False
            parsed['esperado_atomico']=expected
        canonical=json.dumps(parsed,sort_keys=True,separators=(',',':'),allow_nan=False)
        proof=json.dumps({k:v for k,v in parsed.items() if k!='esperado_atomico'},sort_keys=True,separators=(',',':'),allow_nan=False)
        fingerprint=hashlib.sha256(proof.encode()).hexdigest()
        if row and ((row['wallet'],row['operacion'],row['rol'])!=(wallet,oid,rol) or (row['huella'] and row['huella']!=fingerprint)):
            S.registrar(wallet,oid,'libro','Recibo verificado modificado o reutilizado.');return False
        db.ex("INSERT INTO gmgn_recibos(firma,wallet,operacion,rol,estado,datos,huella) VALUES(?,?,?,?,'verificado',?,?) ON CONFLICT(firma) DO UPDATE SET estado='verificado',datos=excluded.datos,huella=excluded.huella,error=NULL",(firma,wallet,oid,rol,canonical,fingerprint))
    return True


def consultar(firma):
    url=os.environ.get('DMN_SOLANA_RPC_URL','https://api.mainnet-beta.solana.com')
    req=urllib.request.Request(url,data=json.dumps({'jsonrpc':'2.0','id':1,'method':'getTransaction','params':[firma,{'encoding':'jsonParsed','commitment':'finalized','maxSupportedTransactionVersion':0}]}).encode(),headers={'Content-Type':'application/json'},method='POST')
    with urllib.request.urlopen(req,timeout=10) as response:
        raw=response.read(2000001)
        if len(raw)>2000000:raise ValueError('respuesta RPC demasiado grande')
        data=json.loads(raw)
    if data.get('error') or not data.get('result'):raise ValueError('recibo no finalizado o no disponible')
    return data['result']


def procesar(maximo=2):
    preparar();rows=db.q("SELECT r.*,l.token FROM gmgn_recibos r JOIN gmgn_libro l ON l.wallet=r.wallet AND l.operacion=r.operacion WHERE r.estado='pendiente' AND proxima<=? ORDER BY proxima LIMIT ?",(int(time.time()),maximo))
    for row in rows:
        try:
            parsed=interpretar(consultar(row['firma']),row['firma'],row['wallet'],row['token'],row['rol'])
            expected=(json.loads(row['datos']) if row['datos'] else {}).get('esperado_atomico')
            if expected is not None and abs(int(parsed['delta_token_atomico']))!=cantidad(expected):
                S.registrar(row['wallet'],row['operacion'],'libro','Cantidad del recibo distinta de la ejecucion declarada.')
                db.ex("UPDATE gmgn_recibos SET estado='conflicto',error='cantidad_distinta' WHERE firma=?",(row['firma'],));continue
            parsed['esperado_atomico']=expected
            guardar(row['firma'],row['wallet'],row['operacion'],row['rol'],parsed)
        except Exception as ex:
            attempts=row['intentos']+1;delay=min(3600,60*2**min(attempts,6))
            db.ex('UPDATE gmgn_recibos SET intentos=?,proxima=?,error=? WHERE firma=?',(attempts,int(time.time())+delay,type(ex).__name__,row['firma']))


def resumen(o):
    preparar();wallet,oid=o.get('wallet'),identificador(o)
    rows=db.q('SELECT rol,estado,datos FROM gmgn_recibos WHERE wallet=? AND operacion=?',(wallet,oid))
    verified=[(x['rol'],json.loads(x['datos'])) for x in rows if x['estado']=='verificado']
    deltas=sum(int(x['delta_token_atomico']) for _,x in verified)
    closed=any(rol=='entrada' and int(x['delta_token_atomico'])>0 for rol,x in verified) and any(rol=='salida' and int(x['delta_token_atomico'])<0 for rol,x in verified) and deltas==0
    roles={x['rol'] for x in rows};missing=[]
    if o.get('cerrada') and o.get('resultado') not in ('fallo_compra','no_entro'):
        missing=[role for role in ('entrada','salida') if role not in roles]
    return {'recibos_verificados':len(verified),'recibos_pendientes':sum(x['estado']!='verificado' for x in rows),'firmas_faltantes':missing,'flujo_sol_identificado':sum(int(x['delta_lamports']) for _,x in verified)/1e9 if verified else None,'comision_red_sol':sum(int(x['comision_red_lamports']) for _,x in verified)/1e9 if verified else None,'variacion_cuentas_token_sol':sum(int(x['renta_cuentas_lamports']) for _,x in verified)/1e9 if verified else None,'token_neto_atomico':str(deltas),'posicion_con_recibos_cerrada':bool(closed),'costes_completos_confirmados':False,'pnl_sol':None}


def confirmar(o):
    """PnL neto en SOL solo con recibos finalizados que cierran la cantidad propia; None mientras falte alguno."""
    if not o.get('cerrada'):return None
    if o.get('resultado')=='no_entro':return 0.0  # buy_low anulado sin ejecutarse: no hubo transaccion
    r=resumen(o)
    if r['recibos_pendientes'] or r['firmas_faltantes']:return None
    if o.get('resultado')=='fallo_compra':
        # Solo gas perdido; sin recibo no se sabe si hubo transaccion.
        return r['flujo_sol_identificado']
    if not r['posicion_con_recibos_cerrada']:return None
    # Flujo de la wallet: incluye comisiones de red, DEX y la renta de la cuenta del token si no se recupero.
    return r['flujo_sol_identificado']
