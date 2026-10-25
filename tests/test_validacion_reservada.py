import unittest,json
from unittest.mock import patch
import numpy as np
from types import SimpleNamespace
import test_guardia as base
from trader import fabrica as F,validacion_fabrica as V,estres_fabrica as E,db,supervision_gmgn as S

class Reservada(unittest.TestCase):
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown
    def llenar(self):
        F.preparar()
        for i in range(300):db.ex('INSERT INTO fab_muestras(ts,token,simbolo,origen,precio,f,ruta,lista) VALUES(?,?,?,?,?,?,?,1)',(1000+i*300,'t','T','hist',1,'{}','[]'))
    def test_cortes_no_se_mueven_al_llegar_datos(self):
        self.llenar();V.preparar(F.MANUALES,F.version);before=V.politica()
        db.ex('INSERT INTO fab_muestras(ts,token,simbolo,origen,precio,f,ruta,lista) VALUES(?,?,?,?,?,?,?,1)',(9999999999,'otro','O','hist',1,'{}','[]'))
        V.preparar(F.MANUALES,F.version);self.assertEqual(before,V.politica())
    def test_cambiar_regla_reinicia_fecha_y_conserva_historial(self):
        with patch.object(V.time,'time',return_value=100):F.preparar()
        modified={**F.MANUALES[0],'rr':4}
        with patch.object(V.time,'time',return_value=200):V.preparar([modified],F.version)
        self.assertEqual(V.inicio_manual('M1',modified,F.version),200)
        self.assertTrue(db.ajuste('fabrica_registro_reglas_v3')['M1']['historial'])
    def test_regla_desconocida_no_acredita_pasado(self):
        self.assertIsNone(V.inicio_manual('M999',F.MANUALES[0],F.version))
    def test_numero_de_busquedas_persiste(self):
        self.llenar();V.preparar(F.MANUALES,F.version);V.registrar_busqueda(3200);self.assertEqual(V.politica()['reglas_probadas'],3200)
    def test_evaluacion_no_usa_reserva_como_prueba(self):
        d=SimpleNamespace(n=4,F=np.ones((4,1)),col={'atr':0},ts=np.array([100,1000,2000,3000]),tok=np.arange(4),vivo=np.ones(4,dtype=bool),corte=500,seleccion_hasta=1500,reservado_desde=2000,H=np.ones((4,2))*2,L=np.zeros((4,2)),C=np.ones((4,2)),longitudes=np.full(4,2))
        e={'conds':[],'sl_k':1,'rr':2,'hold':1};out=F.evaluar(e,d,desde=1)
        self.assertEqual(out['prueba']['n'],1);self.assertEqual(out['vivo']['n'],2)
    def test_adverso_no_usa_stop_posterior_a_take_profit(self):
        d=SimpleNamespace(n=1,F=np.ones((1,1)),col={'atr':0},tok=np.array([0]),tokens=['x'],ts=np.array([100]),H=np.array([[10.,1.]]),L=np.array([[0.,-50.]]),C=np.array([[8.,0.]]))
        e={'sl_k':1,'rr':2,'hold':2}
        # Elegir un instante no rechazado por el hash del escenario.
        out=E.resultados(e,d,[(100,2.,0)],'adverso');self.assertGreater(out[0][1],-50)
    def test_rechazos_y_parciales_son_reproducibles(self):
        d=SimpleNamespace(n=1,F=np.ones((1,1)),col={'atr':0},tok=np.array([0]),tokens=['x'],ts=np.array([100]),H=np.array([[10.]]),L=np.array([[0.]]),C=np.array([[8.]]))
        e={'sl_k':1,'rr':2,'hold':1};ops=[(100,2.,0)]
        self.assertEqual(E.resultados(e,d,ops,'adverso'),E.resultados(e,d,ops,'adverso'))
        self.assertLess(E.resultados(e,d,ops,'parcial')[0][1],2)
    def test_demora_no_reutiliza_precio_de_entrada_anterior(self):
        d=SimpleNamespace(n=1,F=np.ones((1,1)),col={'atr':0},tok=np.array([0]),ts=np.array([100]),H=np.array([[10.,10.]]),L=np.array([[0.,10.]]),C=np.array([[10.,10.]]),longitudes=np.array([2]))
        out=E.resultados({'conds':[],'sl_k':1,'rr':20,'hold':2},d,[(100,4.,0)],'demora')
        self.assertLess(out[0][1],0)
    def test_incidencia_no_inunda_y_sobrevive(self):
        S.registrar('w','o','proteccion','sin stop');S.registrar('w','o','proteccion','sin stop')
        self.assertEqual(db.uno('SELECT COUNT(*) n FROM gmgn_incidencias')['n'],1)
        self.assertEqual(S.estado({'orden':{'id':'o'}},{'activo':True})['estado'],'requiere_revision')
    def test_cierre_no_resuelve_discrepancia_contable(self):
        S.registrar('w','o','proteccion','sin stop');S.registrar('w','o','libro','diferencia')
        S.resolver_posicion('w','o')
        with self.assertRaises(ValueError):S.admitir('w')
        self.assertEqual(db.uno('SELECT COUNT(*) n FROM gmgn_incidencias WHERE resuelta IS NULL')['n'],1)
    def test_piloto_apagado_no_aparece_como_activo(self):
        self.assertEqual(S.estado({}, {'activo':False})['estado'],'apagado')

    def test_reserva_alterada_no_cambia_evaluacion_de_seleccion(self):
        d=SimpleNamespace(n=4,F=np.ones((4,1)),col={'atr':0},ts=np.array([100,1000,2000,3000]),tok=np.arange(4),vivo=np.ones(4,dtype=bool),corte=500,seleccion_hasta=1500,reservado_desde=2000,H=np.ones((4,2))*2,L=np.zeros((4,2)),C=np.ones((4,2)),longitudes=np.full(4,2))
        e={'conds':[],'sl_k':1,'rr':2,'hold':1}
        first=F.evaluar(e,d,desde=1,solo_seleccion=True)
        d.H[2:]=999;d.C[2:]=999
        self.assertEqual(first,F.evaluar(e,d,desde=1,solo_seleccion=True))

    def test_vigilia_desconocida_no_anuncia_proteccion(self):
        S.registrar('w','o','consulta','No se pudo verificar la posicion.')
        state=S.estado({'orden':{'proteccion_verificada':True}}, {'activo':True})
        self.assertTrue(state['requiere_revision']);self.assertEqual(state['estado'],'requiere_revision')

    def test_variantes_se_registran_sin_aprobarse(self):
        F.preparar()
        self.assertEqual(len(F.MANUALES),9)
        registry=db.ajuste('fabrica_registro_reglas_v3')
        self.assertIn('M9',registry);self.assertGreater(registry['M9']['desde'],0)

    def test_no_se_promueve_sin_registro_valido(self):
        with patch.object(F.onchain,'_cacheado',side_effect=lambda k,t,fn:fn()),patch.object(F,'Datos') as data:
            self.assertFalse(F.promovidas())
        data.assert_not_called()

    def test_politica_invalida_no_se_reemplaza_silenciosamente(self):
        F.preparar();db.poner_ajuste(V.CLAVE,{'version':'otra'})
        with self.assertRaises(ValueError):V.preparar(F.MANUALES,F.version)
        self.assertEqual(db.ajuste(V.CLAVE),{'version':'otra'})
