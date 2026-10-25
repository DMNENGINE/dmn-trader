import time,unittest
from unittest.mock import patch
import test_guardia as base
from trader import real,piloto,piloto_fut as F,db

class Sincronia(unittest.TestCase):
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown

    def test_remoto_gmgn_respeta_tope_panel(self):
        real._cfg_mas_nueva({'por_op_sol':10,'cambiado':int(time.time())})
        self.assertEqual(piloto.cfg()['por_op_sol'],0.1)

    def test_remoto_gmgn_no_acepta_booleanos_como_montos(self):
        with self.assertRaises(ValueError):real._cfg_mas_nueva({'por_op_sol':True,'cambiado':int(time.time())})
        self.assertIsNone(db.ajuste(piloto.CFG))

    def test_fechas_invalidas_no_cambian_ningun_piloto(self):
        for sync,key in ((real._cfg_mas_nueva,piloto.CFG),(F.cfg_mas_nueva,F.EST+'_cfg')):
            for ts in (True,-1,'99999999999',float('inf'),float('nan'),int(time.time())+86400):
                with self.subTest(sync=sync.__name__,ts=ts):
                    with self.assertRaises(ValueError):sync({'cambiado':ts})
                    self.assertIsNone(db.ajuste(key))

    def test_version_antigua_o_igual_no_reactiva(self):
        now=int(time.time())
        for sync,key in ((real._cfg_mas_nueva,piloto.CFG),(F.cfg_mas_nueva,F.EST+'_cfg')):
            db.poner_ajuste(key,{'activo':False,'cambiado':now})
            for ts in (now-1,now):sync({'activo':True,'cambiado':ts})
            self.assertFalse(db.ajuste(key)['activo'])

    def test_version_recibida_se_conserva_sin_rebote(self):
        ts=int(time.time())-3
        for sync,key in ((real._cfg_mas_nueva,piloto.CFG),(F.cfg_mas_nueva,F.EST+'_cfg')):
            sync({'activo':False,'cambiado':ts})
            self.assertEqual(db.ajuste(key)['cambiado'],ts)

    def test_rollback_si_falla_segunda_escritura(self):
        original=db.poner_ajuste
        for sync,key in ((real._cfg_mas_nueva,piloto.CFG),(F.cfg_mas_nueva,F.EST+'_cfg')):
            n=[0]
            def escribir(k,v):
                n[0]+=1
                if n[0]==2:raise RuntimeError('fallo de almacenamiento')
                return original(k,v)
            with patch.object(db,'poner_ajuste',side_effect=escribir),self.assertRaises(RuntimeError):sync({'activo':False,'cambiado':int(time.time())})
            self.assertIsNone(db.ajuste(key))
