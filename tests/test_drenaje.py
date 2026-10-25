import unittest
from unittest.mock import patch
import test_cierres as base
from trader import agente as A,db,motor

class Drenaje(unittest.TestCase):
    setUp=base.Cierres.setUp
    tearDown=base.Cierres.tearDown
    iniciar=base.Cierres.iniciar
    barra=base.Cierres.barra
    abrir=base.Cierres.abrir
    def test_apagado_recupera_cierre_cancelado_sin_resetear_capital(self):
        a=self.abrir(); cid=a['cuenta_id']; sesion=A.estado()['sesion_id']
        with patch.object(motor,'barra',return_value=self.barra(False)): A.parar()
        db.ex("UPDATE ordenes SET estado='cancelada' WHERE cuenta_id=?",(cid,))
        antes=db.uno('SELECT efectivo,capital_inicial FROM cuentas WHERE id=?',(cid,))
        with patch.object(motor,'barra',return_value=self.barra(False)):
            A.drenar_cierres();A.drenar_cierres()
        self.assertEqual(db.uno("SELECT COUNT(*) n FROM ordenes WHERE cuenta_id=? AND reduce=1 AND tipo='mercado' AND estado='pendiente'",(cid,))['n'],1)
        self.assertEqual(antes,db.uno('SELECT efectivo,capital_inicial FROM cuentas WHERE id=?',(cid,)))
        self.assertEqual(sesion,A.estado()['sesion_id']);self.assertFalse(A.estado()['activo'])
        with patch.object(motor,'barra',return_value=self.barra(True)): A.drenar_cierres()
        self.assertFalse(motor.posiciones(cid,False))
    def test_retirado_se_cierra_sin_reactivarse(self):
        a=self.abrir();db.ex('UPDATE ag_agentes SET activo=0 WHERE id=?',(a['id'],))
        with patch.object(motor,'barra',return_value=self.barra(False)): A.drenar_cierres()
        with patch.object(motor,'barra',return_value=self.barra(True)): A.drenar_cierres()
        self.assertFalse(motor.posiciones(a['cuenta_id'],False))
        self.assertEqual(db.uno('SELECT activo FROM ag_agentes WHERE id=?',(a['id'],))['activo'],0)
        self.assertTrue(A.estado()['activo'])
    def test_activo_no_solicita_salidas_del_enjambre_activo(self):
        self.abrir()
        with patch.object(A,'_preparar_cierre') as cerrar: A.drenar_cierres()
        cerrar.assert_not_called()
    def test_un_fallo_no_impide_atender_la_siguiente_cuenta(self):
        a=self.abrir();b=A.agentes()[1]
        with patch.object(motor,'barra',return_value=self.barra(True)): motor.enviar(b['cuenta_id'],'AMD','buy',.1,sl=.5,tp=2)
        e=A.estado();e['activo']=False;A.guardar(e)
        real=A._preparar_cierre
        def falla(ag,motivo):
            if ag['id']==a['id']:raise RuntimeError('fallo inyectado')
            return real(ag,motivo)
        with patch.object(A,'_preparar_cierre',side_effect=falla),patch.object(motor,'barra',return_value=self.barra(True)):
            r=A.drenar_cierres()
        self.assertEqual(r['errores'],1);self.assertFalse(motor.posiciones(b['cuenta_id'],False))

if __name__=='__main__':unittest.main()
