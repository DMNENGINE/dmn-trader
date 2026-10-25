import unittest
from unittest.mock import patch
from trader import ejecutor as E,piloto as P

class Intencion(unittest.TestCase):
    def test_denegacion_previa_no_deja_intencion_gmgn_ambigua(self):
        e={}
        with patch.object(P,'_autorizar_entrada'),patch.object(P,'_crear',side_effect=E.Bloqueado('no propietario')),patch.object(P.db,'poner_ajuste'):
            with self.assertRaises(E.Bloqueado):P._crear_seguro(e,'wallet',{'token':'token'},1)
        self.assertNotIn('intencion_entrada',e)

    def test_error_del_broker_conserva_intencion_ambigua(self):
        e={}
        with patch.object(P,'_autorizar_entrada'),patch.object(P,'_crear',side_effect=ValueError('respuesta desconocida')),patch.object(P.db,'poner_ajuste'):
            with self.assertRaises(ValueError):P._crear_seguro(e,'wallet',{'token':'token'},1)
        self.assertIn('intencion_entrada',e)
