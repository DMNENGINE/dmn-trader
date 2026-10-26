import unittest
from unittest.mock import patch
import test_guardia as base
from trader import piloto as P,real,ejecutor as E,salida_gmgn as S

class Salida(unittest.TestCase):
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown
    def orden(self):return dict(wallet='wallet',token='token',simbolo='T',id='strategy',salida='test',cantidad_salida_atomica='100',venta_intento=1,venta_orden_id='sell-id')
    def respuesta(self,n='100'):return dict(order_id='sell-id',input_token='token',output_token=real.SOL,confirmation={'state':'confirmed'},filled_input_amount=n,filled_output_amount='1000',hash='hash-venta')

    def test_venta_confirmada_cierra_solo_cantidad_propia(self):
        o=self.orden()
        with patch.object(real,'_cli',return_value=self.respuesta()):S.conciliar(o)
        self.assertEqual(o['cantidad_salida_atomica'],'0');self.assertEqual(o['venta_estado'],'confirmada');self.assertTrue(o['vendido'])

    def test_formato_actual_de_gmgn_confirma_la_venta(self):
        o=self.orden();x={'status':'confirmed','hash':'hash-venta','order_id':'sell-id','report':{'input_token':'token','input_amount':'100','output_token':real.SOL,'output_amount':'1000'}}
        with patch.object(real,'_cli',return_value=x):S.conciliar(o)
        self.assertEqual(o['venta_estado'],'confirmada');self.assertEqual(o['ventas_confirmadas'][0]['salida_lamports'],'1000')

    def test_formato_actual_pendiente_sin_informe_sigue_pendiente(self):
        o=self.orden()
        with patch.object(real,'_cli',return_value={'status':'pending','order_id':'sell-id'}):S.conciliar(o)
        self.assertEqual(o['venta_estado'],'pendiente_conciliacion');self.assertFalse(o.get('vendido'))

    def test_venta_parcial_solo_reenvia_residuo_confirmado(self):
        o=self.orden();e={'orden':o}
        with patch.object(P,'_tokens',return_value=1),patch.object(real,'_cli',return_value=self.respuesta('40')),patch.object(P,'_vender') as sell:P._salir(e,o,'test')
        sell.assert_not_called();self.assertEqual(o['cantidad_salida_atomica'],'60')
        with patch.object(P,'_tokens',return_value=1),patch.object(P,'_vender',return_value={'order_id':'sell-2'}) as sell:P._salir(e,o,'test')
        sell.assert_called_once();self.assertEqual(sell.call_args.args[0]['cantidad_salida_atomica'],'60');self.assertEqual(o['venta_orden_id'],'sell-2')

    def test_venta_de_otro_token_no_se_confirma(self):
        o=self.orden();x=self.respuesta();x['input_token']='otro'
        with patch.object(real,'_cli',return_value=x),self.assertRaises(ValueError):S.conciliar(o)
        self.assertFalse(o.get('vendido'));self.assertEqual(o['cantidad_salida_atomica'],'100')

    def test_cantidad_mayor_no_se_certifica(self):
        o=self.orden()
        with patch.object(real,'_cli',return_value=self.respuesta('101')),self.assertRaises(ValueError):S.conciliar(o)
        self.assertFalse(o.get('vendido'))

    def test_fallo_de_venta_no_repite_por_ausencia_de_saldo(self):
        o=self.orden();x=self.respuesta();x['confirmation']['state']='failed'
        with patch.object(real,'_cli',return_value=x):S.conciliar(o)
        self.assertTrue(o['venta_intento']);self.assertFalse(o.get('vendido'));self.assertEqual(o['cantidad_salida_atomica'],'100')

    def test_timeout_sigue_pendiente_sin_swap_nuevo(self):
        o=self.orden();e={'orden':o}
        with patch.object(P,'_tokens',return_value=1),patch.object(real,'_cli',side_effect=TimeoutError()),patch.object(P,'_vender') as sell:P._salir(e,o,'test')
        sell.assert_not_called();self.assertTrue(o['venta_intento']);self.assertFalse(o.get('vendido'))

    def test_denegacion_conocida_no_deja_intento_falso(self):
        o=self.orden();o.pop('venta_intento');o.pop('venta_orden_id');e={'orden':o}
        with patch.object(P,'_tokens',return_value=1),patch.object(P,'_vender',side_effect=E.Bloqueado('sin propietario')):P._salir(e,o,'test')
        self.assertNotIn('venta_intento',o);self.assertEqual(o['venta_estado'],'no_enviada');self.assertEqual(o['venta_envios'],0)

    def test_limite_de_envios_parciales_no_es_infinito(self):
        o=self.orden();o.pop('venta_intento');o.pop('venta_orden_id');o['venta_envios']=3;e={'orden':o}
        with patch.object(P,'_tokens',return_value=1),patch.object(P,'_vender') as sell:P._salir(e,o,'test')
        sell.assert_not_called();self.assertEqual(o['venta_estado'],'limite_salidas_requiere_revision')
