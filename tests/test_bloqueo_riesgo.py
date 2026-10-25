import unittest,time
from unittest.mock import patch
import test_guardia as base
from trader import piloto_fut as F,db

class Riesgo(unittest.TestCase):
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown
    def test_bloqueo_nativo_impide_buscar_entradas(self):
        e={}
        with patch.object(F,'cfg',return_value=F.DEFECTO),patch.object(F,'_est',return_value=e),patch.object(F,'puente',return_value={'cuentas':[],'contrato':'dmn-puente/2','libro_sano':True,'riesgo_bloqueado':True,'motivo_riesgo':'stop_rechazado'}),patch('trader.fabrica_fut.senales_ahora') as s:F.paso()
        s.assert_not_called();self.assertTrue(e['riesgo_bloqueado']);self.assertIn('stop_rechazado',e['error'])
    def test_riesgo_bloqueado_en_ultima_revision_impide_enviar(self):
        c={**F.DEFECTO,'activo':True}
        with patch.object(F,'cfg',return_value=c),patch.object(F,'puente',return_value={'contrato':'dmn-puente/2','libro_sano':True,'riesgo_bloqueado':True}),self.assertRaises(F.SinPuente):F._autorizar_envio(c,{},'Sim101',False,1)
    def test_posicion_propia_sin_proteccion_solicita_cierre_si_hay_rechazo_confirmado(self):
        o=dict(cuenta='Sim101',orden='DMN-E-x',nt='MGC 12-26',full='MGC DEC26',qty=1,lado=1,ts=int(time.time()),hold=10,real=False)
        e={'abiertas':{'x':o}}
        cu=dict(nombre='Sim101',conexion='sim',conectada=True,simulada=True,saldo=1000,realizado=0,no_realizado=0,posiciones=[{'inst':o['full'],'qty':1}])
        def puente(r,b=None):
            if r=='/estado':return {'cuentas':[cu],'capacidades':['dmn-orden-estado/1'],'libro_sano':True,'riesgo_bloqueado':True}
            if r.startswith('/orden/estado?'):return dict(cuenta=o['cuenta'],nombre=o['orden'],inst=o['full'],estado='abierta_sin_proteccion',cantidad=1,lado='buy')
            if r=='/cerrar':return {'ok':True,'estado':'solicitado'}
            raise AssertionError(r)
        with patch.object(F,'cfg',return_value={**F.DEFECTO,'activo':False}),patch.object(F,'_est',return_value=e),patch.object(F,'puente',side_effect=puente) as p:F.paso()
        self.assertEqual(sum(x.args[0]=='/cerrar' for x in p.call_args_list),1)
        self.assertEqual(o['estado'],'pendiente_conciliacion')

if __name__=='__main__':unittest.main()
