import time,unittest
from unittest.mock import patch
import test_guardia as base
from trader import piloto as P


class GraciaApolo(unittest.TestCase):
    """APOLO no revisa (ni vende) una posición recién comprada: los costes ya están pagados y el SL protege."""
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown
    def correr(self,hace):
        o=dict(token='T',simbolo='SPCX',compra=1.0,tp=40.0,sl=20.0,atr=8.0,entro=time.time()-hace)
        c={**P.cfg(),'ia':True,'gestion':True,'smart_salida_usd':10**9,'ia_conf_min':0.6}
        with patch.object(P,'_precio',return_value=1.0),patch('trader.onchain.registrar'),patch('trader.onchain.smart_neto',return_value=None),\
             patch('trader.onchain.velas',return_value=[]),patch.object(P,'_contexto',return_value={}),\
             patch.object(P,'_apolo',return_value={'accion':'salir','confianza':0.9,'motivo':'lateral'}) as apolo,\
             patch.object(P,'_salir') as salir:
            P._gestionar({},c,o)
        return apolo,salir
    def test_recien_comprada_no_se_consulta(self):
        apolo,salir=self.correr(80)
        apolo.assert_not_called();salir.assert_not_called()
    def test_pasada_la_gracia_se_consulta_y_el_prompt_descarta_costes(self):
        apolo,salir=self.correr(P.IA_GRACIA+5)
        self.assertIn('YA están pagados',apolo.call_args.args[2]);salir.assert_called_once()

if __name__=='__main__':unittest.main()
