import copy,time,unittest
from unittest.mock import patch
import test_guardia as base
from trader import piloto_fut as F,db

class Conciliacion(unittest.TestCase):
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown
    def orden(self):
        return dict(cuenta='Sim101',nt='MGC 12-26',full='MGC DEC26',orden='DMN-E-PRUEBA',estado='pendiente_conciliacion',ts=int(time.time()),hold=10,real=False,lado=1,qty=1,estrategia='fixture',est_id='fixture',inst='MGC=F')
    def remota(self,estado='abierta_protegida'):
        return dict(cuenta='Sim101',nombre='DMN-E-PRUEBA',inst='MGC DEC26',estado=estado,cantidad=1,lado='buy')
    def correr(self,e,remota):
        cu=dict(nombre='Sim101',conexion='Simulated Data Feed',conectada=True,simulada=True,saldo=1000,realizado=0,no_realizado=0,posiciones=[{'inst':'MGC DEC26','qty':1}],ordenes=[])
        def puente(r,b=None):
            if r=='/estado':return dict(contrato='dmn-puente/2',libro_sano=True,capacidades=['dmn-orden-estado/1'],cuentas=[cu])
            if r.startswith('/orden/estado?'):return remota
            raise AssertionError('no reenviar ni cerrar sin conciliacion: '+r)
        with patch.object(F,'cfg',return_value={**F.DEFECTO,'activo':False}),patch.object(F,'puente',side_effect=puente) as llamadas,patch('trader.conciencia.operacion_real'):
            F.paso()
        return llamadas
    def test_reinicio_recupera_confirmacion_perdida_sin_reenviar(self):
        o=self.orden();db.poner_ajuste(F.EST,{'abiertas':{'Sim101|MGC 12-26':o}})
        llamadas=self.correr({},self.remota())
        e=db.ajuste(F.EST);self.assertEqual(e['abiertas']['Sim101|MGC 12-26']['estado'],'enviada')
        self.assertEqual(e['abiertas']['Sim101|MGC 12-26']['qty_ejecutada'],1)
        self.assertFalse(any(x.args[0]=='/orden' for x in llamadas.call_args_list))
    def test_posicion_manual_no_se_adopta_ni_se_cierra(self):
        o=self.orden();o['ts']-=10000
        db.poner_ajuste(F.EST,{'abiertas':{'x':o}})
        self.correr({},self.remota('desconocida'))
        self.assertEqual(db.ajuste(F.EST)['abiertas']['x']['estado'],'pendiente_conciliacion')
    def test_rechazo_sin_ejecucion_libera_intencion_sin_inventar_trade(self):
        db.poner_ajuste(F.EST,{'abiertas':{'x':self.orden()}})
        self.correr({},self.remota('sin_ejecucion')|{'cantidad':0})
        e=db.ajuste(F.EST);self.assertFalse(e['abiertas']);self.assertFalse(e.get('ops'))
    def test_cierre_confirmado_no_cierra_posicion_manual_y_no_duplica_pnl(self):
        o=self.orden();db.poner_ajuste(F.EST,{'abiertas':{'x':o}})
        n=self.remota('cerrada');n['cierre']=dict(cuenta='Sim101',nombre=o['orden'],inst=o['full'],qty=1,pnl=5)
        self.correr({},n);self.correr({},n)
        e=db.ajuste(F.EST);self.assertFalse(e['abiertas']);self.assertEqual(len(e['ops']),1)
        self.assertEqual(db.uno('SELECT COUNT(*) n FROM fut_resultados')['n'],1)
        self.assertEqual(db.uno('SELECT pnl FROM fut_resultados')['pnl'],5)
    def test_sin_proteccion_no_se_declara_recuperada(self):
        db.poner_ajuste(F.EST,{'abiertas':{'x':self.orden()}})
        self.correr({},self.remota('abierta_sin_proteccion'))
        self.assertEqual(db.ajuste(F.EST)['abiertas']['x']['estado'],'pendiente_conciliacion')
    def test_ambito_y_cantidades_invalidas_se_bloquean(self):
        for cambios in ({'cuenta':'otra'},{'nombre':'otra'},{'inst':'MES DEC26'},{'cantidad':True},{'cantidad':0},{'cantidad':2},{'lado':'sell'},{'estado':'inventado'}):
            with self.subTest(cambios=cambios),patch.object(F,'puente',return_value=self.remota()|cambios),self.assertRaises(F.SinPuente):F._conciliar_orden(self.orden())
    def test_cierre_nan_no_se_contabiliza(self):
        o=self.orden();db.poner_ajuste(F.EST,{'abiertas':{'x':o}})
        n=self.remota('cerrada');n['cierre']=dict(cuenta='Sim101',nombre=o['orden'],inst=o['full'],qty=1,pnl=float('nan'))
        self.correr({},n)
        self.assertTrue(db.ajuste(F.EST)['abiertas']);self.assertFalse(db.ajuste(F.EST).get('ops'))

if __name__=='__main__':unittest.main()
