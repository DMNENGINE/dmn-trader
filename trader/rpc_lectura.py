"""Consultas Solana limitadas a lectura; nunca firma ni transmite transacciones."""
import json,os,urllib.request
from .contabilidad_gmgn import natural,cantidad

METODOS=frozenset(('getBalance','getTokenAccountsByOwner','getSignatureStatuses','getSignaturesForAddress'))

def consultar(metodo,parametros):
    if metodo not in METODOS:raise ValueError('RPC de escritura no permitido')
    url=os.environ.get('DMN_SOLANA_RPC_URL','https://api.mainnet-beta.solana.com')
    request=urllib.request.Request(url,data=json.dumps({'jsonrpc':'2.0','id':1,'method':metodo,'params':parametros},allow_nan=False).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=10) as response:
        raw=response.read(2000001)
    if len(raw)>2000000:raise ValueError('respuesta RPC excesiva')
    data=json.loads(raw)
    if data.get('error') or 'result' not in data:raise ValueError('consulta RPC no disponible')
    return data['result']

def tokens(wallet,token):
    r=consultar('getTokenAccountsByOwner',[wallet,{'mint':token},{'encoding':'jsonParsed','commitment':'finalized'}])
    slot=natural(r['context']['slot']);seen=set();total=0;decimals=None
    if not isinstance(r.get('value'),list):raise ValueError('cuentas ausentes')
    for row in r['value']:
        key=row.get('pubkey');info=row['account']['data']['parsed']['info'];n=info['tokenAmount']
        d=natural(n['decimals'])
        if not isinstance(key,str) or not key or key in seen or info.get('owner')!=wallet or info.get('mint')!=token or d>18 or (decimals is not None and d!=decimals):raise ValueError('cuenta token fuera de ambito')
        seen.add(key);decimals=d;total+=cantidad(n['amount'])
    return {'atomico':str(total),'decimales':decimals,'slot':slot}

def firmas(signatures):
    if not signatures:return []
    result=consultar('getSignatureStatuses',[signatures,{'searchTransactionHistory':True}])
    values=result.get('value')
    if not isinstance(values,list) or len(values)!=len(signatures):raise ValueError('estados incompletos')
    for v in values:
        if v is not None and (not isinstance(v,dict) or 'err' not in v or v.get('confirmationStatus') not in ('processed','confirmed','finalized')):raise ValueError('estado invalido')
    return values
