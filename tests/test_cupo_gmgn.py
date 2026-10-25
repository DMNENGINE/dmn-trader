import time,unittest
from unittest.mock import patch
from trader import onchain as O


class CupoGmgn(unittest.TestCase):
    def setUp(self):
        O._turnos.clear();O._pausa['hasta']=0
    tearDown=setUp
    def test_rafaga_espera_en_vez_de_fallar(self):
        t=time.time()
        for _ in range(O.POR_SEG+1):O.turno()
        self.assertGreaterEqual(time.time()-t,0.9)
    def test_respeta_la_pausa_pedida_por_gmgn(self):
        O._pausa['hasta']=time.time()+0.5;t=time.time();O.turno()
        self.assertGreaterEqual(time.time()-t,0.45)
    def test_espera_maxima_falla_sin_llamar(self):
        O._pausa['hasta']=time.time()+30
        with self.assertRaises(O.ErrorOnchain):O.turno(espera_max=0.2)
    def test_envio_real_sin_turno_no_se_envia(self):
        from trader import real,presupuesto_gmgn as PG
        O._pausa['hasta']=time.time()+30
        with patch.object(real,'_cli_directo') as cli,patch('trader.ejecutor.ejecutar',side_effect=lambda p,c,f:f()):
            with self.assertRaises(PG.EntradaNoEnviada):real._cli('swap','--chain','sol','--from','w','--amount','1')
        cli.assert_not_called()
