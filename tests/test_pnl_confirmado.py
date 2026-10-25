import unittest,copy
from unittest.mock import patch
import test_guardia as base
import test_contabilidad_gmgn as T
from trader import db,piloto as P,contabilidad_gmgn as C,supervision_gmgn as S

ENT,SAL=T.SIG,'4'*88

def recibo(lado,firma):
    x=T.receipt(lado);x['transaction']['signatures']=[firma];return x


class PnlConfirmado(unittest.TestCase):
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown
    def op(self,**kw):return {**dict(wallet='wallet',id_entrada='op',id='strategy',token='token',simbolo='TOK',cantidad_comprada_atomica='100',cerrada=1,resultado='tp',pnl_sol=None,pnl_confirmado=False),**kw}
    def verificar(self,o):
        C.anotar(o,ENT,'entrada');C.anotar(o,SAL,'salida',esperado='100')
        with patch.object(C,'consultar',side_effect=lambda f:recibo('entrada' if f==ENT else 'salida',f)):C.procesar()
    def test_sin_recibos_sigue_pendiente(self):
        o=self.op();C.operacion(o)
        self.assertIsNone(C.confirmar(o))
    def test_entrada_y_salida_verificadas_dan_el_flujo_neto(self):
        o=self.op();self.verificar(o)
        self.assertAlmostEqual(C.confirmar(o),(-12005000+10995000)/1e9)
    def test_solo_entrada_no_confirma(self):
        o=self.op();C.anotar(o,ENT,'entrada')
        with patch.object(C,'consultar',return_value=recibo('entrada',ENT)):C.procesar()
        self.assertIsNone(C.confirmar(o))
    def test_no_entro_es_cero_sin_transaccion(self):
        self.assertEqual(C.confirmar(self.op(resultado='no_entro')),0.0)
    def test_bucle_confirma_y_avisa_a_la_conciencia(self):
        o=self.op();self.verificar(o)
        db.poner_ajuste(P.EST,{'ops':[o,{'simbolo':'VIEJA','pnl_sol':0.1}]})
        with patch.object(P.conciencia,'operacion_real') as con:P._confirmar_ops()
        ops=db.ajuste(P.EST)['ops']
        self.assertTrue(ops[0]['pnl_confirmado']);self.assertAlmostEqual(ops[0]['pnl_sol'],-0.00101)
        self.assertNotIn('pnl_confirmado',ops[1]);con.assert_called_once()
        with patch.object(P.conciencia,'operacion_real') as con:P._confirmar_ops()
        con.assert_not_called()


class ConciliadoAMano(unittest.TestCase):
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown
    def test_desbloquea_intencion_e_incidencias_una_vez(self):
        S.registrar('wallet','op','libro','x')
        e={'intencion_entrada':{'token':'token'},'orden':None}
        P._conciliado_a_mano(e,123)
        e=db.ajuste(P.EST)
        self.assertNotIn('intencion_entrada',e);self.assertEqual(e['conciliado_hecho'],123)
        S.admitir('wallet')
    def test_boton_llega_por_la_config(self):
        c=P.poner_cfg({'conciliado':999})
        self.assertEqual(P.cfg()['conciliado'],999)
        with self.assertRaises(ValueError):P.poner_cfg({'conciliado':'ya'})
