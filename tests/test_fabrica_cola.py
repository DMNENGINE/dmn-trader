import json,time,unittest
from unittest.mock import patch
import test_guardia as base
from trader import db,fabrica as F


class ColaCompletar(unittest.TestCase):
    """La cola de muestras sin completar no puede crecer sin fin: primero las más antiguas y por tramos acotados."""
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown
    def muestra(self,ts,token):
        db.ex('INSERT INTO fab_muestras(ts,token,simbolo,origen,precio,f) VALUES(?,?,?,?,?,?)',(ts,token,token,'vivo',1,json.dumps({'foto_version':F.VERSION_VALIDACION})))
    def test_primero_el_token_con_la_muestra_mas_antigua_aunque_vaya_ultimo_alfabeticamente(self):
        F.preparar();ahora=int(time.time())
        self.muestra(ahora-5*3600,'aaa');self.muestra(ahora-30*3600,'zzz')
        pedidos=[]
        def velas(s,res,inicio,fin):pedidos.append(s);return []
        with patch.object(F.onchain,'registrar',side_effect=lambda c,t,s:t),patch.object(F.onchain,'velas',side_effect=velas),patch.object(F.conciencia,'velas'):F.completar(max_tokens=1)
        self.assertEqual(pedidos,['zzz'])
    def test_cada_token_pide_como_mucho_un_tramo_de_velas(self):
        F.preparar();ahora=int(time.time())
        for h in (60,50,40,30,20,10):self.muestra(ahora-h*3600,'t')
        ventanas=[]
        def velas(s,res,inicio,fin):ventanas.append(fin-inicio);return []
        with patch.object(F.onchain,'registrar',side_effect=lambda c,t,s:t),patch.object(F.onchain,'velas',side_effect=velas),patch.object(F.conciencia,'velas'):F.completar()
        self.assertEqual(len(ventanas),1);self.assertLessEqual(ventanas[0],F.TRAMO+4*3600+F.HORIZONTE+2*F.BARRA)
