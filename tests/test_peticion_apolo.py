import time,unittest
from unittest.mock import patch
import test_guardia as base
from trader import db,piloto as P

TOK='C5xph2yL6gMdxmuLGxJBkGpmuDFfyi5fqkBNhvL6pump'
class Vivo:
    def is_alive(self):return True


class PeticionApolo(unittest.TestCase):
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown
    def activo(self,**kw):
        db.poner_ajuste(P.CFG,{**P.DEFECTO,'activo':True,'cambiado':1,**kw})
    def pedir(self,**kw):
        with patch.dict(P._hilo,{'t':Vivo()}):return P.pedir_apolo({'accion':'comprar','token':TOK,'motivo':'m',**kw})
    def test_compra_valida_queda_pendiente_con_limites_por_defecto(self):
        self.activo();pet=self.pedir()
        self.assertEqual((pet['estado'],pet['tp'],pet['sl']),('pendiente',30.0,15.0))
        self.assertEqual(P.peticion_apolo()['id'],pet['id'])
    def test_direccion_inventada_o_nombre_se_rechaza(self):
        self.activo()
        for t in ('BTC','7p7S6tS7uK6uMscH8Y1c8K8XyS8Y8Y8Y8Y8Y8Y8Y8Y8Y8Y8Y8Y8Y8Y8Y8Y8Y80',''):
            with self.assertRaises(ValueError):self.pedir(token=t)
    def test_piloto_parado_o_con_operacion_abierta_rechaza(self):
        with self.assertRaises(ValueError):self.pedir()
        self.activo();db.poner_ajuste(P.EST,{'orden':{'id':'x'}})
        with self.assertRaises(ValueError):self.pedir()
    def test_tp_sl_fuera_de_rango(self):
        self.activo()
        for kw in ({'sl':80},{'sl':2},{'tp':500},{'tp':True}):
            with self.assertRaises(ValueError):self.pedir(**kw)
    def test_sin_piloto_en_este_equipo(self):
        self.activo()
        with patch.dict(P._hilo,{'t':None}),self.assertRaises(ValueError):P.pedir_apolo({'accion':'comprar','token':TOK})
    def test_caduca_a_los_10_min(self):
        self.activo();pet=self.pedir();db.poner_ajuste(P.PET,{**pet,'ts':int(time.time())-700})
        self.assertIsNone(P._peticion('comprar'));self.assertEqual(P.peticion_apolo()['estado'],'caducada')
    def test_comprar_pasa_por_mercado_gmgn_con_los_datos_de_apolo(self):
        self.activo();pet=self.pedir(tp=40,sl=10,simbolo='BTC')
        e={}
        with patch.object(P,'_filtro_apolo',return_value=[]),patch.object(P,'_precio',return_value=0.01),patch('trader.mercado_gmgn.abrir') as abrir:P._comprar_apolo(e,'w',pet)
        _,w,b,precio=abrir.call_args.args
        self.assertEqual((w,b['token'],b['tp'],b['sl'],b['est_id'],precio),('w',TOK,40.0,10.0,'apolo-conservador',0.01))
    def test_token_que_no_pasa_el_filtro_no_se_compra(self):
        self.activo();pet=self.pedir()
        malo={'address':TOK,'liquidity':200000,'volume':900000,'rug_ratio':0.92,'top_10_holder_rate':0.79,'renounced_mint':1,'renounced_freeze_account':1,'open_timestamp':0}
        with patch.object(P.onchain,'_api',return_value={'rank':[malo]}),patch('trader.mercado_gmgn.abrir') as abrir:
            with self.assertRaises(P.EntradaNoEnviada) as ex:P._comprar_apolo({},'w',pet)
        abrir.assert_not_called();self.assertIn('perfil conservador',str(ex.exception));self.assertIn('riesgo de rug',str(ex.exception));self.assertIn('top 10',str(ex.exception))
    def test_token_fuera_del_ranking_no_se_compra(self):
        self.activo();pet=self.pedir()
        with patch.object(P.onchain,'_api',return_value={'rank':[]}),patch('trader.mercado_gmgn.abrir') as abrir:
            with self.assertRaises(P.EntradaNoEnviada):P._comprar_apolo({},'w',pet)
        abrir.assert_not_called()
    def test_token_sano_pasa_el_filtro(self):
        sano={'address':TOK,'liquidity':200000,'volume':900000,'rug_ratio':0.1,'top_10_holder_rate':0.2,'renounced_mint':1,'renounced_freeze_account':1,'open_timestamp':1700000000}
        with patch.object(P.onchain,'_api',return_value={'rank':[sano]}):self.assertEqual(P._filtro_apolo(TOK),[])
    def test_salir_sin_posicion_se_rechaza_sin_vender(self):
        db.poner_ajuste(P.PET,{'id':'a','accion':'salir','motivo':'x','ts':int(time.time()),'estado':'pendiente'})
        db.poner_ajuste(P.EST,{'orden':None})
        with patch.object(P,'_salir') as salir,patch.object(P.onchain,'actualizar_smart'):P.paso()
        salir.assert_not_called();self.assertEqual(P.peticion_apolo()['estado'],'rechazada')
    def test_salir_con_posicion_usa_la_salida_del_piloto(self):
        db.poner_ajuste(P.PET,{'id':'a','accion':'salir','motivo':'pinta mal','ts':int(time.time()),'estado':'pendiente'})
        db.poner_ajuste(P.EST,{'orden':{'id':'o','simbolo':'X','entro':1,'token':TOK,'wallet':'w'}})
        with patch.object(P,'_salir') as salir,patch.object(P,'_seguir'),patch.object(P.onchain,'actualizar_smart'):P.paso()
        self.assertIn('APOLO: pinta mal',salir.call_args.args[2]);self.assertEqual(P.peticion_apolo()['estado'],'enviada')

    # perfiles: cambian el filtro del token y los rangos de TP/SL, nunca los límites de dinero ni la ruta/TP/SL de abrir()
    def info(self,**kw):
        d={'liquidity':30000,'open_timestamp':1700000000,'price':{'volume_24h':500000},'dev':{'top_10_holder_rate':0.3}}
        d.update(kw);return d
    def comprar(self,perfil,info,**kw):
        self.activo();pet=self.pedir(perfil=perfil,**kw)
        with patch.object(P.onchain,'_api',return_value=info),patch.object(P,'_precio',return_value=0.01),patch('trader.mercado_gmgn.abrir') as abrir:
            try:P._comprar_apolo({},'w',pet)
            except P.EntradaNoEnviada as ex:return None,str(ex)
        return abrir.call_args.args[2],None
    def test_perfil_desconocido_se_rechaza(self):
        self.activo()
        with self.assertRaises(ValueError):self.pedir(perfil='kamikaze')
    def test_rangos_de_tp_sl_por_perfil(self):
        self.activo()
        with self.assertRaises(ValueError):self.pedir(sl=45)
        with self.assertRaises(ValueError):self.pedir(perfil='moderado',sl=45)
        self.assertEqual(self.pedir(perfil='agresivo',sl=45,tp=300)['sl'],45.0)
        with self.assertRaises(ValueError):self.pedir(perfil='agresivo',sl=70)
    def test_agresivo_compra_fuera_del_ranking_con_liquidez_minima(self):
        b,err=self.comprar('agresivo',self.info(liquidity=6000,dev={'top_10_holder_rate':0.9},open_timestamp=int(time.time())))
        self.assertIsNone(err);self.assertEqual((b['est_id'],b['perfil']),('apolo-agresivo','agresivo'))
    def test_agresivo_sin_liquidez_no_compra(self):
        for info in (self.info(liquidity=4000),self.info(liquidity=None)):
            b,err=self.comprar('agresivo',info);self.assertIsNone(b);self.assertIn('liquidez',err)
    def test_moderado_exige_volumen_top10_y_edad(self):
        b,err=self.comprar('moderado',self.info());self.assertIsNone(err);self.assertEqual(b['est_id'],'apolo-moderado')
        for kw,txt in (({'price':{'volume_24h':5000}},'volumen'),({'dev':{'top_10_holder_rate':0.8}},'top 10'),({'open_timestamp':int(time.time())},'nuevo')):
            b,err=self.comprar('moderado',self.info(**kw));self.assertIsNone(b);self.assertIn(txt,err)
    def test_candidatas_por_perfil(self):
        P._CAND.clear();ahora=int(time.time())
        filas=[{'address':'A'*44,'symbol':'VIEJO','liquidity':30000,'volume':200000,'top_10_holder_rate':0.3,'open_timestamp':1700000000},
               {'address':'B'*44,'symbol':'NUEVO','liquidity':30000,'volume':200000,'top_10_holder_rate':0.9,'open_timestamp':ahora},
               {'address':'C'*44,'symbol':'TRAMPA','liquidity':90000,'is_honeypot':1,'open_timestamp':1700000000}]
        with patch.object(P.onchain,'_api',return_value={'rank':filas}):
            self.assertEqual([x['simbolo'] for x in P.candidatas_perfil('agresivo')],['VIEJO','NUEVO'])
            self.assertEqual([x['simbolo'] for x in P.candidatas_perfil('moderado')],['VIEJO'])
        with self.assertRaises(ValueError):P.candidatas_perfil('otro')
    def test_rotacion_elige_el_perfil_con_menos_operaciones(self):
        self.assertEqual(P._siguiente_perfil({'ops':[]}),'conservador')
        self.assertEqual(P._siguiente_perfil({'ops':[{'est_id':'apolo-conservador'},{'est_id':'apolo-agresivo'}]}),'moderado')
    def test_perfiles_compra_la_primera_aprobada_y_salta_los_vetos(self):
        cand=[{'simbolo':'MALO','token':'A'*44,'liquidez':1,'volumen_1h':1,'cambio_1h':0,'top10':0.1,'edad_h':5},{'simbolo':'BUENO','token':'B'*44,'liquidez':1,'volumen_1h':1,'cambio_1h':0,'top10':0.1,'edad_h':5}]
        e={'ops':[{'est_id':'apolo-conservador'}]}
        respuestas=iter([{'decision':'veto','confianza':0.9,'motivo':'no'},{'decision':'entrar','confianza':0.8,'motivo':'pinta bien'}])
        with patch.object(P,'candidatas_perfil',return_value=cand) as cp,patch.object(P,'_apolo',side_effect=lambda *a:next(respuestas)),patch.object(P,'_contexto',return_value={}),patch.object(P,'_comprar_apolo') as comprar:
            P._entrada_perfiles(e,{**P.DEFECTO,'ia':True},'w',set())
        cp.assert_called_once_with('moderado');pet=comprar.call_args.args[2]
        self.assertEqual((pet['simbolo'],pet['perfil'],pet['tp'],pet['sl']),('BUENO','moderado',40.0,20.0));self.assertIn('A'*44,e['enfriar'])
    def test_perfiles_sin_apolo_no_compra(self):
        cand=[{'simbolo':'X','token':'A'*44,'liquidez':1,'volumen_1h':1,'cambio_1h':0,'top10':0.1,'edad_h':5}]
        with patch.object(P,'candidatas_perfil',return_value=cand),patch.object(P,'_apolo',side_effect=OSError('cerrado')),patch.object(P,'_contexto',return_value={}),patch.object(P,'_comprar_apolo') as comprar:
            P._entrada_perfiles({},{**P.DEFECTO,'ia':True},'w',set())
        comprar.assert_not_called()
    def test_perfil_sin_candidatas_pasa_al_siguiente_en_el_mismo_escaneo(self):
        cand={'conservador':[],'moderado':[],'agresivo':[{'simbolo':'X','token':'A'*44,'liquidez':1,'volumen_1h':1,'cambio_1h':0,'top10':0.1,'edad_h':5}]}
        with patch.object(P,'candidatas_perfil',side_effect=lambda p:cand[p]),patch.object(P,'_apolo',return_value={'decision':'entrar','confianza':0.8,'motivo':'ok'}),patch.object(P,'_contexto',return_value={}),patch.object(P,'_comprar_apolo') as comprar:
            P._entrada_perfiles({},{**P.DEFECTO,'ia':True},'w',set())
        self.assertEqual(comprar.call_args.args[2]['perfil'],'agresivo')
