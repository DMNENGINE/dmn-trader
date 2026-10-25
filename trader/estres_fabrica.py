"""Escenarios hipoteticos reproducibles, no cotizaciones de comisiones ni predicciones."""
import hashlib
import numpy as np
from types import SimpleNamespace

ESCENARIOS={
    'adverso':{'coste_extra_pct':3.0,'rechazos_pct':10,'coste_fallo_pct':.5,'fraccion':1.0,'stop_extremo':True},
    'parcial':{'coste_extra_pct':2.0,'rechazos_pct':0,'coste_fallo_pct':.5,'fraccion':.5,'stop_extremo':False},
    'demora':{'coste_extra_pct':0.0,'rechazos_pct':0,'coste_fallo_pct':0.0,'fraccion':1.0,'stop_extremo':False,'demora':True},
}


def resultados(e,D,ops,perfil,coste_base=6.0):
    p=ESCENARIOS[perfil];out=[];costs=getattr(D,'costes',None)
    for ts,net,index in ops:
        if p.get('demora'):
            from . import fabrica as F
            h=int(e['hold']);entry=D.C[index,0];factor=1+entry/100
            if h<2 or not np.isfinite(factor) or factor<=0:continue
            features=D.F[index:index+1].copy();features[0,D.col['atr']]/=factor
            proxy=SimpleNamespace(n=1,F=features,col=D.col,ts=np.array([ts+300]),tok=np.array([0]),C=((1+D.C[index:index+1,1:h]/100)/factor-1)*100,H=((1+D.H[index:index+1,1:h]/100)/factor-1)*100,L=((1+D.L[index:index+1,1:h]/100)/factor-1)*100,longitudes=np.array([max(0,int(getattr(D,'longitudes',np.full(D.n,D.C.shape[1]))[index])-1)]),sl_lim=getattr(D,'sl_lim',(4,30)),costes=np.array([coste_base if costs is None else costs[index]]))
            simulated=F.simular({**e,'hold':h-1,'conds':[]},proxy)
            if simulated:out.append((ts,simulated[0][1],index))
            continue
        source=D.tokens[int(D.tok[index])] if hasattr(D,'tokens') else str(D.tok[index])
        uniform=int(hashlib.sha256((perfil+'|'+str(source)+'|'+str(ts)).encode()).hexdigest()[:8],16)%100
        if uniform<p['rechazos_pct']:
            out.append((ts,-p['coste_fallo_pct'],index));continue
        base=coste_base if costs is None else float(costs[index])
        value=(net+base)*p['fraccion']-base-p['coste_extra_pct']
        if p['stop_extremo']:
            lo,hi=getattr(D,'sl_lim',(4,30));stop=float(np.clip(e['sl_k']*D.F[index,D.col['atr']],lo,hi))
            low=D.L[index,:int(e['hold'])] if e.get('lado',1)==1 else -D.H[index,:int(e['hold'])]
            hit=np.flatnonzero(low<=-stop)
            high=D.H[index,:int(e['hold'])] if e.get('lado',1)==1 else -D.L[index,:int(e['hold'])]
            target=np.flatnonzero(high>=stop*e['rr'])
            if len(hit) and (not len(target) or hit[0]<=target[0]):value=min(value,float(low[hit[0]])-base-p['coste_extra_pct'])
        out.append((ts,float(value),index))
    return out
