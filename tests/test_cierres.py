import unittest
from unittest.mock import patch
import test_guardia as base
from trader import agente as A, db, motor


class Cierres(unittest.TestCase):
    setUp = base.Guardia.setUp
    tearDown = base.Guardia.tearDown
    iniciar = base.Guardia.iniciar

    def barra(self, abierta):
        return {'o': 1, 'h': 1, 'l': 1, 'c': 1, 't': 1700000000, 'moneda': 'USD', 'abierto': abierta, 'tipo_activo': 'EQUITY'}

    def abrir(self):
        self.iniciar()
        a = A.agentes()[0]
        with patch.object(motor, 'barra', return_value=self.barra(True)):
            motor.enviar(a['cuenta_id'], 'AMD', 'buy', 0.1, sl=0.5, tp=2, origen='agente')
        self.assertEqual(len(motor.posiciones(a['cuenta_id'], False)), 1)
        return a

    def test_parar_con_mercado_cerrado_conserva_cierre_y_protecciones(self):
        a = self.abrir()
        with patch.object(motor, 'barra', return_value=self.barra(False)):
            A.parar()
        self.assertTrue(motor.posiciones(a['cuenta_id'], False))
        pendientes = db.q("SELECT tipo FROM ordenes WHERE cuenta_id=? AND reduce=1 AND estado='pendiente'", (a['cuenta_id'],))
        self.assertIn('mercado', [o['tipo'] for o in pendientes])
        self.assertIn('stop', [o['tipo'] for o in pendientes])

    def test_repetir_cierre_total_no_duplica_ordenes_pendientes(self):
        a = self.abrir()
        with patch.object(motor, 'barra', return_value=self.barra(False)):
            x = motor.cerrar(a['cuenta_id'], 'AMD')
            y = motor.cerrar(a['cuenta_id'], 'AMD')
        self.assertEqual(x['id'], y['id'])

    def test_arranque_revisa_todas_las_cuentas_sin_cambiar_capital_ni_sesion(self):
        a = self.abrir()
        antes = db.q('SELECT id,efectivo,capital_inicial FROM cuentas ORDER BY id')
        sesion = A.estado()['sesion_id']
        intentos = db.uno('SELECT COUNT(*) n FROM ag_intentos')['n']
        with patch.object(motor, 'barra', return_value=self.barra(False)):
            with self.assertRaisesRegex(motor.ErrorOrden, 'CIERRES_PENDIENTES'):
                A.iniciar(2, 1, 100, ['cripto'], duracion_min=1, perdida_max_pct=10)
        self.assertEqual(antes, db.q('SELECT id,efectivo,capital_inicial FROM cuentas ORDER BY id'))
        self.assertEqual(sesion, A.estado()['sesion_id'])
        self.assertEqual(intentos, db.uno('SELECT COUNT(*) n FROM ag_intentos')['n'])
        self.assertFalse(A.estado()['activo'])

    def test_al_abrir_mercado_se_cierra_y_luego_se_puede_iniciar(self):
        a = self.abrir()
        with patch.object(motor, 'barra', return_value=self.barra(False)):
            A.parar()
        with patch.object(motor, 'barra', return_value=self.barra(True)):
            motor.procesar(a['cuenta_id'])
        self.assertFalse(motor.posiciones(a['cuenta_id'], False))
        self.assertEqual(db.uno("SELECT COUNT(*) n FROM trades WHERE estado='cerrado'")['n'], 1)
        A.iniciar(2, 1, 100, ['cripto'], duracion_min=1, perdida_max_pct=10)
        self.assertTrue(A.estado()['activo'])

    def test_fallo_de_precio_no_cancela_los_stops_anteriores(self):
        a = self.abrir()
        with patch.object(motor, 'barra', side_effect=motor.ErrorOrden('sin precio')):
            A.parar()
        self.assertTrue(db.uno("SELECT 1 FROM ordenes WHERE cuenta_id=? AND reduce=1 AND tipo='stop' AND estado='pendiente'", (a['cuenta_id'],)))


if __name__ == '__main__':
    unittest.main()
