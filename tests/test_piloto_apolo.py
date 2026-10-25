import unittest
from unittest.mock import patch
from trader import piloto as P

C={'ia':True}
B={'simbolo':'TOK','token':'t','atr':4,'pts':7}


class PilotoApolo(unittest.TestCase):
    def setUp(self):
        self.ctx=patch.object(P,'_contexto',return_value={'rsi':50});self.ctx.start()
    def tearDown(self):self.ctx.stop()
    def test_apolo_aprueba(self):
        with patch.object(P,'_apolo',return_value={'decision':'entrar','confianza':0.8,'motivo':'bien'}) as a:
            self.assertEqual(P._ia_entrada(C,B),(True,'bien'))
        self.assertEqual(a.call_args.args[:2],('entrada','TOK'))
    def test_apolo_veta(self):
        with patch.object(P,'_apolo',return_value={'decision':'vetar','confianza':0.9,'motivo':'pump'}):
            self.assertEqual(P._ia_entrada(C,B),(False,'pump'))
    def test_sin_apolo_no_se_entra(self):
        with patch.object(P,'_apolo',side_effect=ConnectionRefusedError()):
            ok,motivo=P._ia_entrada(C,B)
        self.assertIsNone(ok);self.assertIn('no entro',motivo)
    def test_ia_apagada_no_pregunta(self):
        with patch.object(P,'_apolo') as a:self.assertEqual(P._ia_entrada({'ia':False},B),(True,'sin IA'))
        a.assert_not_called()
