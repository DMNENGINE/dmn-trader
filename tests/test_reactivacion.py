import unittest,time,copy
from unittest.mock import patch
import test_cierres as base
from trader import agente as A,db,motor,guardia,reactivacion as R

class Reactivacion(unittest.TestCase):
    setUp=base.Cierres.setUp
    tearDown=base.Cierres.tearDown
    iniciar=base.Cierres.iniciar
    barra=base.Cierres.barra
    abrir=base.Cierres.abrir
    def reiniciar(self):
        with patch.object(motor,'barra',return_value=self.barra(False)):
            return A.iniciar(2,2,100,['cripto'],duracion_min=10,perdida_max_pct=10)
    def test_cuenta_pendiente_no_bloquea_las_libres_ni_resetea_capital(self):
        a=self.abrir();before=db.uno('SELECT efectivo,capital_inicial FROM cuentas WHERE id=?',(a['cuenta_id'],));old=A.estado()['sesion_id'];count=db.uno('SELECT COUNT(*) n FROM ag_intentos WHERE agente_id=?',(a['id'],))['n']
        result=self.reiniciar()
        self.assertTrue(A.estado()['activo']);self.assertNotEqual(A.estado()['sesion_id'],old);self.assertEqual(R.estado(A.estado())['esperando_cierre'],1)
        self.assertEqual(before,db.uno('SELECT efectivo,capital_inicial FROM cuentas WHERE id=?',(a['cuenta_id'],)))
        self.assertEqual(count,db.uno('SELECT COUNT(*) n FROM ag_intentos WHERE agente_id=?',(a['id'],))['n'])
        self.assertEqual(len(A.agentes()),1);self.assertNotEqual(A.agentes()[0]['id'],a['id'])
        self.assertNotIn(a['cuenta_id'],guardia.estado()['cuentas']);self.assertEqual(guardia.estado()['capitalInicial'],2)
    def test_confirmar_cierre_reincorpora_sin_duplicar_intentos_ni_presupuesto(self):
        a=self.abrir();self.reiniciar();session=A.estado()['sesion_id']
        with patch.object(motor,'barra',return_value=self.barra(True)):
            result=A.drenar_cierres()
        self.assertEqual(result['reanudadas'],1);self.assertEqual(len(A.agentes()),2);self.assertEqual(session,A.estado()['sesion_id'])
        self.assertEqual(guardia.estado()['capitalInicial'],4);self.assertEqual(len(guardia.estado()['cuentas']),2)
        self.assertEqual(db.uno('SELECT capital_inicial FROM cuentas WHERE id=?',(a['cuenta_id'],))['capital_inicial'],2)
        attempts=db.uno('SELECT COUNT(*) n FROM ag_intentos')['n']
        with patch.object(motor,'barra',return_value=self.barra(True)):A.drenar_cierres()
        self.assertEqual(attempts,db.uno('SELECT COUNT(*) n FROM ag_intentos')['n']);self.assertEqual(guardia.estado()['capitalInicial'],4)
        self.assertEqual(db.uno("SELECT COUNT(*) n FROM trades WHERE estado='cerrado'")['n'],1)
    def test_la_cola_sobrevive_reinicio_sqlite(self):
        self.abrir();self.reiniciar();db._con.close();db._con=None
        self.assertEqual(R.estado(A.estado())['esperando_cierre'],1)
        with patch.object(motor,'barra',return_value=self.barra(True)):A.drenar_cierres()
        self.assertEqual(len(A.agentes()),2)
    def test_parar_cancela_la_incorporacion_pero_no_el_cierre(self):
        a=self.abrir();self.reiniciar()
        with patch.object(motor,'barra',return_value=self.barra(False)):A.parar()
        before=db.uno('SELECT COUNT(*) n FROM ag_intentos')['n']
        with patch.object(motor,'barra',return_value=self.barra(True)):A.drenar_cierres()
        self.assertFalse(A.estado()['activo']);self.assertFalse(motor.posiciones(a['cuenta_id'],False));self.assertEqual(before,db.uno('SELECT COUNT(*) n FROM ag_intentos')['n'])
        self.assertEqual(db.uno('SELECT activo FROM ag_agentes WHERE id=?',(a['id'],))['activo'],0)
    def test_sesion_vencida_no_reincorpora(self):
        a=self.abrir();self.reiniciar();g=guardia.estado();g['vence']=time.time()-1;db.poner_ajuste(guardia.CLAVE,g)
        with patch.object(motor,'barra',return_value=self.barra(True)):A.drenar_cierres()
        self.assertEqual(len(A.agentes()),1);self.assertEqual(guardia.estado()['capitalInicial'],2)
    def test_trabajo_de_sesion_anterior_no_reactiva(self):
        a=self.abrir();self.reiniciar();old=A.estado()['sesion_id'];self.reiniciar()
        self.assertFalse(R.reanudar(old,a['id']));self.assertEqual(R.estado(A.estado())['esperando_cierre'],1)
    def test_todos_pueden_esperar_con_duracion_sin_capital_ficticio(self):
        self.abrir();b=A.agentes()[1]
        with patch.object(motor,'barra',return_value=self.barra(True)):motor.enviar(b['cuenta_id'],'AMD','buy',.1,sl=.5,tp=2,origen='agente')
        self.reiniciar();self.assertEqual(len(A.agentes()),0);self.assertEqual(guardia.estado()['capitalInicial'],0);self.assertIsNone(guardia.evaluar())
        with patch.object(motor,'barra',return_value=self.barra(True)):A.drenar_cierres()
        self.assertEqual(len(A.agentes()),2);self.assertEqual(guardia.estado()['capitalInicial'],4)
    def test_error_de_una_cuenta_no_bloquea_al_resto(self):
        a=self.abrir();original=A._preparar_cierre
        def close(agent,reason):
            if agent['id']==a['id']:raise RuntimeError('precio ausente')
            return original(agent,reason)
        with patch.object(A,'_preparar_cierre',side_effect=close):result=A.iniciar(2,2,100,['cripto'],duracion_min=10,perdida_max_pct=10)
        self.assertEqual(R.estado(A.estado())['esperando_cierre'],1);self.assertEqual(len(A.agentes()),1)
    def test_la_cuenta_en_espera_no_puede_enviar_entrada(self):
        a=self.abrir();self.reiniciar()
        with patch.object(motor,'barra',return_value=self.barra(True)),self.assertRaises(motor.ErrorOrden):motor.enviar(a['cuenta_id'],'BTC-USD','buy',.1)
    def test_limite_de_perdida_no_se_reinicia_al_incorporar(self):
        self.abrir();self.reiniciar();g=guardia.estado();g['pnl']=-.1;db.poner_ajuste(guardia.CLAVE,g)
        with patch.object(motor,'barra',return_value=self.barra(True)):A.drenar_cierres()
        self.assertEqual(guardia.estado()['pnl'],-.1)
    def test_espera_bloquea_entradas_tambien_sin_plan_supervisado(self):
        a=self.abrir()
        with patch.object(motor,'barra',return_value=self.barra(False)):A.iniciar(2,2,100,['cripto'])
        self.assertEqual(guardia.estado(),{})
        with self.assertRaises(ValueError):guardia.admitir(a['cuenta_id'])
        guardia.admitir(a['cuenta_id'],reduce=True)
    def test_espera_no_crea_sustitutos_al_repetir_inicio_activo(self):
        self.abrir()
        with patch.object(motor,'barra',return_value=self.barra(False)):A.iniciar(2,2,100,['cripto'])
        fake=unittest.mock.Mock();fake.is_alive.return_value=True
        with patch.object(A,'_hilo',fake):A.iniciar(2,2,100,['cripto'])
        self.assertEqual(db.uno('SELECT COUNT(*) n FROM ag_agentes')['n'],2)
        self.assertEqual(len(A.agentes()),1);self.assertEqual(R.estado(A.estado())['esperando_cierre'],1)
    def test_todos_esperando_respetan_vencimiento(self):
        a=self.abrir();b=A.agentes()[1]
        with patch.object(motor,'barra',return_value=self.barra(True)):motor.enviar(b['cuenta_id'],'AMD','buy',.1,sl=.5,tp=2,origen='agente')
        self.reiniciar();g=guardia.estado();g['vence']=time.time()-1;db.poner_ajuste(guardia.CLAVE,g)
        with patch.object(motor,'barra',return_value=self.barra(False)):reason=guardia.evaluar()
        self.assertIn('duracion',reason);self.assertFalse(A.estado()['activo']);self.assertFalse(db.uno("SELECT 1 FROM ag_esperas WHERE estado='esperando'"))
    def test_rollback_si_falla_creacion_del_intento(self):
        a=self.abrir();self.reiniciar()
        with patch.object(motor,'barra',return_value=self.barra(True)):
            A._preparar_cierre(a,'test')
            motor.procesar(a['cuenta_id'])
        before=db.uno('SELECT efectivo,capital_inicial FROM cuentas WHERE id=?',(a['cuenta_id'],));g=copy.deepcopy(guardia.estado())
        with patch.object(A,'_nuevo_intento',side_effect=RuntimeError),self.assertRaises(RuntimeError):R.reanudar(A.estado()['sesion_id'],a['id'])
        self.assertEqual(before,db.uno('SELECT efectivo,capital_inicial FROM cuentas WHERE id=?',(a['cuenta_id'],)));self.assertEqual(g,guardia.estado());self.assertEqual(R.estado(A.estado())['esperando_cierre'],1)
