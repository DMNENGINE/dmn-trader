"""Regresiones aisladas: no usa la base real, GMGN, NinjaTrader ni datos de mercado."""
import copy
import http.client
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ['DMN_TRADER_DB'] = os.path.join(tempfile.mkdtemp(prefix='dmn-guardia-import-'), 'test.db')
from trader import agente as A, db, guardia, motor, piloto, piloto_fut, servidor


class Guardia(unittest.TestCase):
    def setUp(self):
        if db._con:
            db._con.close()
        db._con = None
        db.RUTA = os.path.join(tempfile.mkdtemp(prefix='dmn-guardia-'), 'test.db')
        self.parches = [patch.object(A, '_hilo', None), patch.object(A, 'arrancar_hilo'), patch.object(guardia, 'arrancar'),
                        patch.object(A, 'resumen', return_value={}), patch.object(A.onchain, 'disponible', return_value=False),
                        patch('urllib.request.urlopen', side_effect=AssertionError('red externa prohibida en estas pruebas')),
                        patch.object(piloto.real, '_cli', side_effect=AssertionError('GMGN prohibido en estas pruebas')),
                        patch.object(piloto_fut, 'puente', side_effect=AssertionError('NinjaTrader prohibido en estas pruebas'))]
        for p in self.parches:
            p.start()
        A.preparar()

    def tearDown(self):
        for p in reversed(self.parches):
            p.stop()

    def iniciar(self):
        A.iniciar(2, 1, 100, ['cripto'], duracion_min=1, perdida_max_pct=10)
        return guardia.estado()

    def test_validacion(self):
        for d, p in [(0, 10), (1, 0), (True, 10), (1, float('nan')), (1441, 10)]:
            with self.assertRaises(ValueError):
                guardia.validar(d, p)

    def test_sesion_persistida(self):
        g = self.iniciar()
        self.assertEqual(g['sesion'], A.estado()['sesion_id'])
        self.assertEqual(g['capitalInicial'], 2)
        self.assertEqual(len(g['cuentas']), 2)
        guardia.admitir(g['cuentas'][0])

    def test_duracion_independiente_de_apolo(self):
        g = self.iniciar()
        with patch.object(guardia.time, 'time', return_value=g['vence'] + 1):
            self.assertIn('duracion', guardia.evaluar())
        self.assertFalse(A.estado()['activo'])

    def test_entrada_fuera_de_plazo_bloqueada_en_motor(self):
        g = self.iniciar()
        with patch.object(guardia.time, 'time', return_value=g['vence'] + 1):
            with self.assertRaises(motor.ErrorOrden):
                motor.enviar(g['cuentas'][0], 'BTC-USD', 'buy', 1)
            guardia.admitir(g['cuentas'][0], reduce=True)

    def test_guardia_antes_del_movimiento_de_efectivo(self):
        g = self.iniciar()
        cid = g['cuentas'][0]
        oid = db.ex("INSERT INTO ordenes(cuenta_id,simbolo,lado,tipo,cantidad,estado,creada) VALUES(?,?,'buy','mercado',1,'pendiente',0)", (cid, 'BTC-USD'))
        c = motor._fila(cid)
        guardia.detener('prueba')
        motor._llenar(c, {'id': oid, 'simbolo': 'BTC-USD', 'cantidad': 1, 'lado': 'buy', 'reduce': False}, 1, int(time.time()), None)
        self.assertEqual(motor._fila(cid)['efectivo'], c['efectivo'])
        self.assertEqual(db.uno('SELECT estado FROM ordenes WHERE id=?', (oid,))['estado'], 'cancelada')

    def test_perdida_acumulada_no_se_borra_al_renovar_intentos(self):
        g = self.iniciar()
        a = A.agentes()[0]
        db.ex("INSERT INTO trades(cuenta_id,estado,pnl,comisiones) VALUES(?,'cerrado',-0.15,0)", (a['cuenta_id'],))
        A._nuevo_intento(A.estado(), a, A._g(a), 'prueba de reinicio')
        db.ex("INSERT INTO trades(cuenta_id,estado,pnl,comisiones) VALUES(?,'cerrado',-0.15,0)", (a['cuenta_id'],))
        self.assertIn('perdida', guardia.evaluar())
        self.assertFalse(A.estado()['activo'])

    def test_un_guardado_tardio_no_reactiva_el_enjambre(self):
        self.iniciar()
        viejo = copy.deepcopy(A.estado())
        A.parar('prueba')
        A.guardar(viejo)
        self.assertFalse(A.estado()['activo'])

    def test_una_sesion_antigua_no_sobrescribe_la_nueva(self):
        self.iniciar()
        viejo = A.estado()
        A.parar('prueba')
        self.iniciar()
        nueva = A.estado()['sesion_id']
        A.guardar(viejo)
        self.assertEqual(A.estado()['sesion_id'], nueva)

    def test_reinicio_del_servidor_conserva_el_vencimiento(self):
        g = self.iniciar()
        db._con.close()
        db._con = None
        with patch.object(guardia.time, 'time', return_value=g['vence'] + 1):
            self.assertIsNotNone(guardia.evaluar())
        self.assertFalse(A.estado()['activo'])

    def test_no_reinicia_capital_si_no_puede_cerrar(self):
        self.iniciar()
        a = A.agentes()[0]
        with patch.object(motor, 'posiciones', return_value=[{'simbolo': 'BTC-USD'}]), patch.object(motor, 'cerrar', side_effect=motor.ErrorOrden('sin precio')):
            with self.assertRaises(motor.ErrorOrden):
                A._nuevo_intento(A.estado(), a, A._g(a), 'prueba')

    def test_pilotos_rechazan_false_como_texto_y_numeros_no_finitos(self):
        for fn, clave in [(piloto.poner_cfg, 'activo'), (piloto_fut.poner_cfg, 'real_activo')]:
            with self.assertRaises(ValueError):
                fn({clave: 'false'})
        with self.assertRaises(ValueError):
            piloto.poner_cfg({'perdida_max_sol': float('nan')})
        with self.assertRaises(ValueError):
            piloto_fut.poner_cfg({'qty': 1.5})
        db.poner_ajuste(piloto.CFG, {'activo': 'false'})
        self.assertFalse(piloto.cfg()['activo'])
        db.poner_ajuste(piloto_fut.EST + '_cfg', {'real_activo': 'false'})
        self.assertFalse(piloto_fut.cfg()['real_activo'])

    def test_riesgo_futuros_cuenta_todos_los_contratos(self):
        self.assertEqual(piloto_fut.riesgo_total(12, 2), 24)
        for v in [float('nan'), -1, True]:
            with self.assertRaises(ValueError):
                piloto_fut.riesgo_total(v, 2)

    def test_api_simulada_no_admite_modo_real_ni_limites_omitidos(self):
        for b in [{'modo': 'real'}, {'modo': 'simulado'}, {'agentes': True}, {'agentes': 1.1}]:
            with self.assertRaises(ValueError):
                servidor.r_enj_ini({}, b)

    def test_horario_se_valida(self):
        for t in ['25:00', '1:30', '12:99']:
            with self.assertRaises(ValueError):
                servidor._hasta(t)

    def test_api_transmite_limites_a_la_guardia(self):
        servidor.r_enj_ini({}, {'modo': 'simulado', 'agentes': 1, 'capital': 1, 'meta': 100, 'mercados': ['cripto'], 'hasta': '23:59', 'duracionMin': 2, 'perdidaMaxPct': 5})
        self.assertEqual(guardia.estado()['perdidaMaxPct'], 5)
        self.assertEqual(A.estado()['hasta_solicitado'], '23:59')

    def test_nodo_antiguo_no_recibe_plan_que_no_puede_proteger(self):
        with self.assertRaisesRegex(ValueError, 'actualiza'):
            servidor.r_enj_ini({}, {'nodo': 'antiguo', 'modo': 'simulado', 'agentes': 1, 'duracionMin': 2, 'perdidaMaxPct': 5})

    def test_mapa_declara_solo_el_enjambre_como_simulado(self):
        with patch.object(A, 'precios', return_value=[]), patch.object(A, 'noticias', return_value={}):
            m = A.mapa()
        self.assertEqual(m['modo'], 'simulado')
        self.assertIn('dmn-guardia/1', m['capacidades'])

    def test_http_protege_origin_host_token_y_cuerpo(self):
        with patch.object(servidor, 'TOKEN', 'token-ficticio'), patch.object(servidor, 'RUTAS', [('POST', servidor.re.compile('^/api/prueba$'), lambda q, b: {'ok': True})]):
            srv = servidor.ThreadingHTTPServer(('127.0.0.1', 0), servidor.Manejador)
            hilo = threading.Thread(target=srv.serve_forever, daemon=True)
            hilo.start()
            try:
                for extras, esperado in [({}, 200), ({'Origin': 'https://ajeno.invalid'}, 403), ({'Host': 'ajeno.invalid'}, 421), ({'Content-Length': '-1'}, 400), ({'x-token': 'incorrecto'}, 401)]:
                    c = http.client.HTTPConnection('127.0.0.1', srv.server_port, timeout=3)
                    c.request('POST', '/api/prueba', '{}', headers={'x-token': 'token-ficticio', **extras})
                    r = c.getresponse()
                    self.assertEqual(r.status, esperado)
                    r.read()
                    c.close()
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == '__main__':
    unittest.main()
