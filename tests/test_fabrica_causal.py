import unittest,time,json
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import test_guardia as base
from trader import fabrica as F,db

def datos(n=2,h=2):
    return SimpleNamespace(n=n,F=np.ones((n,1)),col={'atr':0},ts=np.arange(n)*1000+100,tok=np.arange(n),vivo=np.ones(n,dtype=bool),corte=1100,H=np.ones((n,h))*3,L=np.zeros((n,h)),C=np.ones((n,h))*2,longitudes=np.full(n,h))

def regla(hold=2):return {'conds':[['atr','>',0]],'sl_k':1,'rr':2,'hold':hold}

class FabricaCausal(unittest.TestCase):
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown

    def test_atr_ausente_no_se_inventa(self):
        d=datos(1);d.F[0,0]=np.nan
        self.assertEqual(F.simular({'conds':[],'sl_k':1,'rr':2,'hold':2},d),[])

    def test_ruta_corta_no_se_convierte_en_cierre_a_tiempo(self):
        d=datos(1);d.longitudes[0]=1
        result=F.simular(regla(),d,diagnostico=True)
        self.assertEqual(result['ops'],[]);self.assertEqual(len(result['incompletas']),1)

    def test_perdida_observada_antes_de_ruta_corta_se_conserva(self):
        d=datos(1);d.longitudes[0]=1;d.L[0,0]=-5
        result=F.simular(regla(),d,diagnostico=True)
        self.assertEqual(len(result['ops']),1);self.assertLess(result['ops'][0][1],0);self.assertFalse(result['incompletas'])

    def test_dato_no_finito_no_inventa_ganancia(self):
        d=datos(1);d.C[0,0]=np.nan
        result=F.simular(regla(),d,diagnostico=True)
        self.assertEqual(result['ops'],[]);self.assertEqual(len(result['incompletas']),1)

    def test_incompleta_reserva_exposicion_del_token(self):
        d=datos(2);d.ts=np.array([100,200]);d.tok=np.array([0,0]);d.longitudes[0]=1
        result=F.simular(regla(),d,diagnostico=True)
        self.assertFalse(result['ops']);self.assertEqual(len(result['incompletas']),1)

    def test_historico_no_cuenta_como_vivo(self):
        d=datos(2,1);d.vivo=np.array([False,True])
        self.assertEqual(F.evaluar(regla(1),d,desde=1)['vivo']['n'],1)

    def test_posicion_previa_al_inicio_no_bloquea_primer_trade_vivo(self):
        d=datos(2);d.ts=np.array([100,200]);d.tok=np.array([0,0])
        self.assertEqual(F.evaluar(regla(),d,desde=150)['vivo']['n'],1)

    def test_nuevas_y_antiguas_se_conservan_pero_solo_nuevas_acreditan(self):
        F.preparar();ts=int(time.time())-10000
        row={'atr':1,'liq_k':50,'top10':.1,'rug':0,'version_temporal':F.VERSION_VALIDACION,'rasgos_hasta':ts}
        for i,features in enumerate(({'atr':1},row)):
            db.ex('INSERT INTO fab_muestras(ts,token,simbolo,origen,precio,f,ruta,lista) VALUES(?,?,?,?,?,?,?,1)',(ts+i,'t'+str(i),'T','vivo',1,json.dumps(features),json.dumps([[3,0,2]]*24)))
        d=F.Datos();self.assertEqual(d.n,1);self.assertEqual(d.cuarentena_temporal,1);self.assertEqual(db.uno('SELECT COUNT(*) n FROM fab_muestras')['n'],2)

    def test_riesgo_desconocido_no_pasa_filtro(self):
        safe={'liq_k':50,'top10':.1,'rug':0}
        self.assertTrue(F._seguridad_foto(safe))
        for key in safe:
            f=dict(safe);f.pop(key);self.assertFalse(F._seguridad_foto(f))
        for rug in (True,float('nan'),-.1,.31):self.assertFalse(F._seguridad_foto({**safe,'rug':rug}))

    def test_regla_live_con_rug_ausente_no_consulta_velas(self):
        row={'token':'t','simbolo':'T','precio':1,'f':json.dumps({'liq_k':50,'top10':.1})}
        with patch.object(db,'uno',return_value={'t':int(time.time())}),patch.object(db,'q',return_value=[row]),patch.object(F.onchain,'velas') as candles:
            self.assertFalse(F.senales_ahora([{'conds':[]}]))
        candles.assert_not_called()

    def test_foto_entre_velas_no_usa_cierre_posterior(self):
        F.preparar();ts=(int(time.time())-F.HORIZONTE-1200)//300*300+120
        features={'foto_version':F.VERSION_VALIDACION,'liq_k':50,'top10':.1,'rug':0}
        db.ex('INSERT INTO fab_muestras(ts,token,simbolo,origen,precio,f) VALUES(?,?,?,?,?,?)',(ts,'token','T','vivo',1,json.dumps(features)))
        start=ts//300*300-50*300
        candles=[dict(t=start+i*300,o=1,c=1,h=1.01,l=.99,v=1) for i in range(100)]
        candles[50]['h']=1000
        with patch.object(F.onchain,'registrar',return_value='t'),patch.object(F.onchain,'velas',return_value=candles),patch.object(F.time,'sleep'),patch.object(F.conciencia,'velas'):F.completar()
        f=json.loads(db.uno('SELECT f FROM fab_muestras')['f'])
        self.assertLessEqual(f['rasgos_hasta'],ts);self.assertAlmostEqual(f['atr'],2);self.assertEqual(f['version_temporal'],F.VERSION_VALIDACION)

    def test_redondeo_visual_no_promueve_media_inferior(self):
        d=datos(20,1);d.ts=np.arange(20)*1000+100;d.corte=10000;d.vivo=np.arange(20)>=10;d.H[:]=7.6;d.C[:]=7.4999
        e={**regla(1),'id':'M1','nombre':'test','desde':1}
        with patch.object(F,'Datos',return_value=d),patch.object(F,'_candidatas',return_value=[e]),patch.object(F.onchain,'_cacheado',side_effect=lambda k,t,fn:fn()),patch.object(F.VF,'politica',return_value={'version':F.VF.VERSION,'entreno_hasta':1,'seleccion_hasta':2,'reservado_desde':3}):self.assertFalse(F.promovidas())

    def test_modelo_corto_conserva_sentido_al_validar_ruta(self):
        d=datos(1,1);d.H[:]=2;d.L[:]=-3;d.C[:]=-2
        result=F.simular({**regla(1),'lado':-1},d)
        self.assertAlmostEqual(result[0][1],-4)

    def test_toque_stop_y_objetivo_misma_vela_sigue_conservador(self):
        d=datos(1,1);d.H[:]=20;d.L[:]=-20
        self.assertLess(F.simular(regla(1),d)[0][1],0)

    def test_metadato_temporal_booleano_no_certifica_muestra(self):
        F.preparar();f={'atr':1,'liq_k':50,'top10':.1,'rug':0,'version_temporal':F.VERSION_VALIDACION,'rasgos_hasta':True}
        db.ex('INSERT INTO fab_muestras(ts,token,simbolo,origen,precio,f,ruta,lista) VALUES(?,?,?,?,?,?,?,1)',(100,'t','T','vivo',1,json.dumps(f),json.dumps([[3,0,2]]*24)))
        d=F.Datos();self.assertFalse(d.simulables[0])

    def test_generada_necesita_sello_de_nueva_validacion(self):
        d=datos(60,2);d.ts=np.arange(60)*10000+100;d.corte=100000;d.vivo=np.arange(60)>=10;d.H[:,0]=1;d.C[:,0]=0;d.H[:,1]=50;d.C[:,1]=45;d.version_modelo=F.VF.VERSION
        e={**regla(2),'rr':10,'id':'G-test','nombre':'test','desde':1};e['version']=F.version(e)
        with patch.object(F,'Datos',return_value=d),patch.object(F,'_candidatas',return_value=[e]),patch.object(F.onchain,'_cacheado',side_effect=lambda k,t,fn:fn()),patch.object(F.VF,'politica',return_value={'version':F.VF.VERSION,'entreno_hasta':1,'seleccion_hasta':2,'reservado_desde':3}):
            self.assertFalse(F.promovidas());e['validacion']=F.VF.VERSION;self.assertEqual(len(F.promovidas()),1)

    def test_winrate_visual_redondeado_no_supera_filtro_real(self):
        d=datos(46,1);d.ts=np.arange(46)*1000+100;d.corte=23000;d.vivo=np.arange(46)>=23;d.H[:]=20;d.C[:]=20
        d.H[31:]=6;d.C[31:]=5.9
        e={**regla(1),'rr':5,'id':'M1','nombre':'test','desde':1}
        with patch.object(F,'Datos',return_value=d),patch.object(F,'_candidatas',return_value=[e]),patch.object(F.onchain,'_cacheado',side_effect=lambda k,t,fn:fn()),patch.object(F.VF,'politica',return_value={'version':F.VF.VERSION,'entreno_hasta':1,'seleccion_hasta':2,'reservado_desde':3}):self.assertFalse(F.promovidas())
