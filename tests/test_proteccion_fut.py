import time,unittest
from unittest.mock import patch
import test_guardia as base
from trader import piloto_fut as F,db
from trader.fabrica_fut import FUT


class Proteccion(unittest.TestCase):
    """Stop virtual (breakeven/trailing) de las posiciones Sim y la sombra que compara con y sin protección."""
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown
    def orden(self,**kw):
        return dict(cuenta='Sim101',nt='MES 12-26',full='MES DEC26',inst='MES=F',orden='DMN-E-P',estado='enviada',ts=int(time.time())-200,hold=24,
                    real=False,lado=1,qty=1,qty_ejecutada=1,estrategia='fixture',est_id='fx',sl_ticks=40,tp_ticks=80,**kw)
    def cuenta(self):
        return dict(nombre='Sim101',posiciones=[{'inst':'MES DEC26','lado':'Long','qty':1,'precio':7000.0}])
    def correr(self,o,velas,rechazar=False):
        e={'abiertas':{'x':o}};llamadas=[]
        def puente(r,b=None):
            llamadas.append(r)
            if r.startswith('/barras'):return {'velas':[[o['ts']+60*(i+1)+60,*v,1] for i,v in enumerate(velas)]+[[int(time.time())+50,7000,7000,6000,6000,1]]}  # la que se forma, con basura
            if r=='/cerrar':
                if rechazar:raise F.SinPuente('CIERRE_PENDIENTE_DATOS')
                return {'ok':True}
            raise AssertionError(r)
        with patch.object(F,'puente',side_effect=puente):
            try:cerro=F._proteger(e,o,self.cuenta())
            except F.SinPuente:cerro=None
        return e,cerro,llamadas

    def test_sin_recorrido_no_toca_nada(self):
        e,cerro,ll=self.correr(self.orden(),[(7000,7005,6999,7004)])  # +20 ticks de 80: aún no
        self.assertFalse(cerro);self.assertNotIn('/cerrar',ll);self.assertIsNone(e['abiertas']['x'].get('stop_virtual'))

    def test_breakeven_cierra_si_vuelve_a_la_entrada_y_deja_sombra(self):
        o=self.orden()
        e,cerro,ll=self.correr(o,[(7000,7011,6999,7010),(7010,7010,7000,7000.25)])  # llegó a +44 ticks y vuelve a +1
        self.assertTrue(cerro);self.assertIn('/cerrar',ll);self.assertEqual(o['stop_virtual'],2)
        self.assertEqual(e['sombras'][0]['entrada'],7000.0);self.assertEqual(e['sombras'][0]['tp_ticks'],80)

    def test_trailing_guarda_la_mitad_de_lo_maximo(self):
        o=self.orden()
        e,cerro,_=self.correr(o,[(7000,7016,6999,7015),(7015,7016,7007,7007.5)])  # +64 ticks (80 %) → stop en +32; vuelve a +30
        self.assertEqual(o['stop_virtual'],32);self.assertTrue(cerro);self.assertEqual(o['proteccion']['ticks'],30)

    def test_cierre_rechazado_no_deja_sombra_y_se_vuelve_a_decidir(self):
        o=self.orden()
        e,cerro,_=self.correr(o,[(7000,7011,6999,7010),(7010,7010,7000,7000.25)],rechazar=True)
        self.assertIsNone(cerro);self.assertFalse(e.get('sombras'));self.assertFalse(o['proteccion'].get('enviado'))
        e,cerro,_=self.correr(o,[(7000,7011,6999,7010),(7010,7010,7000,7008)])  # el precio se recupera: sigue abierta
        self.assertFalse(cerro);self.assertNotIn('proteccion',o)

    def test_sin_velas_recientes_no_decide(self):
        o=self.orden();o['ts']-=3600
        e,cerro,ll=self.correr(o,[(7000,7011,6999,7010),(7010,7010,7000,7000.25)])
        self.assertFalse(cerro);self.assertNotIn('/cerrar',ll)

    def test_real_nunca_se_protege(self):
        e,cerro,ll=self.correr(dict(self.orden(),real=True),[(7000,7020,6999,7000)])
        self.assertFalse(cerro);self.assertEqual(ll,[])

    def test_sombra_stop_objetivo_y_tiempo(self):
        s=dict(entrada=7000.0,lado=1,sl_ticks=40,tp_ticks=80,qty=1,desde=1000,hasta=5000)
        self.assertEqual(F.resolver_sombra(s,[(1000,7005,6999,7004),(1060,7021,7001,7020)],FUT['MES=F'],ahora=2000),100.0)
        self.assertEqual(F.resolver_sombra(s,[(1000,7005,6989,6990)],FUT['MES=F'],ahora=2000),-50.0)
        self.assertEqual(F.resolver_sombra(s,[(1000,7021,6989,7000)],FUT['MES=F'],ahora=2000),-50.0)  # ambos en la vela: cuenta el stop
        self.assertIsNone(F.resolver_sombra(s,[(1000,7005,6999,7004)],FUT['MES=F'],ahora=2000))
        self.assertEqual(F.resolver_sombra(s,[(1000,7005,6999,7004),(5000,7030,6900,7030)],FUT['MES=F'],ahora=6000),20.0)
        corto=dict(s,lado=-1)
        self.assertEqual(F.resolver_sombra(corto,[(1000,7001,6979,6980)],FUT['MES=F'],ahora=2000),100.0)

    def test_comparativa_con_y_sin(self):
        e={'ops':[dict(nt='MES 12-26',estrategia='a',protegida=True,pnl=2.5,pnl_sin=-50,proteccion={'mfe_ticks':44,'enviado':True},cerrada=1),
                  dict(nt='MCL 12-26',estrategia='b',protegida=True,pnl=30,pnl_sin=90,proteccion={'mfe_ticks':60,'enviado':True},cerrada=2),
                  dict(nt='MCL 12-26',estrategia='f',protegida=True,pnl=-45,proteccion={'mfe_ticks':65},cerrada=2),  # NinjaTrader rechazó el cierre: igual
                  dict(nt='MNQ 12-26',estrategia='c',protegida=True,pnl=-20,cerrada=3),
                  dict(nt='MGC 12-26',estrategia='d',protegida=True,pnl=5,proteccion={'mfe_ticks':9,'enviado':True},cerrada=4),  # sombra pendiente
                  dict(nt='MYM 12-26',estrategia='e',pnl=99,cerrada=5)]}  # anterior a la protección: no cuenta
        c=F.comparativa(e)
        self.assertEqual((c['ops'],c['actuo'],c['pendientes']),(4,2,1))
        self.assertEqual((c['con'],c['sin'],c['diferencia']),(-32.5,-25.0,-7.5))
        self.assertEqual((c['salvadas'],c['recortadas']),(1,1))

    def test_sombra_se_aplica_a_la_operacion_cerrada(self):
        e={'ops':[{'orden':'DMN-E-P','pnl':2.5,'proteccion':{'enviado':True}}],'sombras':[dict(orden='DMN-E-P',inst='MES=F',nt='MES 12-26',desde=1,hasta=2,pnl=-50.0)],'log':[]}
        F._sombras(e)
        self.assertEqual(e['ops'][0]['pnl_sin'],-50.0);self.assertEqual(e['sombras'],[])


if __name__=='__main__':unittest.main()
