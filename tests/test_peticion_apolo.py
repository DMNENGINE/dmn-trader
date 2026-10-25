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
        self.assertEqual((w,b['token'],b['tp'],b['sl'],b['est_id'],precio),('w',TOK,40.0,10.0,'apolo',0.01))
    def test_token_que_no_pasa_el_filtro_no_se_compra(self):
        self.activo();pet=self.pedir()
        malo={'address':TOK,'liquidity':200000,'volume':900000,'rug_ratio':0.92,'top_10_holder_rate':0.79,'renounced_mint':1,'renounced_freeze_account':1,'open_timestamp':0}
        with patch.object(P.onchain,'_api',return_value={'rank':[malo]}),patch('trader.mercado_gmgn.abrir') as abrir:
            with self.assertRaises(P.EntradaNoEnviada) as ex:P._comprar_apolo({},'w',pet)
        abrir.assert_not_called();self.assertIn('riesgo de rug',str(ex.exception));self.assertIn('top 10',str(ex.exception))
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
