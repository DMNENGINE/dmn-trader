import contextlib,hashlib,http.server,json,os,subprocess,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from trader import ejecutor as E,real,servidor as S,piloto as P

NODE='a'*64;OTHER='b'*64;AID='c'*64

class Ejecutor(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patches=[patch.object(E,'directorio',return_value=self.root),patch.object(E,'identidad',return_value=NODE),patch.dict(os.environ,{'DMN_TRADER_TOKEN':'token-prueba','DMN_EJECUTOR_TOKEN':'token-prueba','DMN_EJECUTOR_AUTORIDAD':AID,'DMN_EJECUTOR_URL':''}),patch.object(E,'_locks',{})]
        for p in self.patches:p.start()
        self.policy={'id':AID,'propietarios':{'gmgn-sol|wallet':NODE,'ninjatrader|RealTest':NODE}}
        self.save()

    def save(self):
        (self.root/'autoridad.json').write_text(json.dumps(self.policy),encoding='utf-8')

    def tearDown(self):
        for f in E._locks.values():f.close()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    @contextlib.contextmanager
    def autoridad(self,transform=lambda x:x):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(h):
                body=json.loads(h.rfile.read(int(h.headers['Content-Length'])))
                assert h.headers['x-token']=='token-prueba'
                result=transform(E.verificar(body))
                raw=json.dumps(result).encode();h.send_response(200);h.send_header('Content-Length',str(len(raw)));h.end_headers();h.wfile.write(raw)
            def log_message(self,*args):pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        with patch.dict(os.environ,{'DMN_EJECUTOR_URL':'http://127.0.0.1:'+str(server.server_port)}):
            try:yield
            finally:server.shutdown();server.server_close();thread.join()

    def test_equipo_propietario_envia_una_vez(self):
        action=Mock(return_value='ok')
        with self.autoridad():self.assertEqual(E.ejecutar('gmgn-sol','wallet',action),'ok')
        action.assert_called_once()

    def test_otro_equipo_no_envia(self):
        action=Mock()
        with self.autoridad(),patch.object(E,'identidad',return_value=OTHER),self.assertRaises(ValueError):E.ejecutar('gmgn-sol','wallet',action)
        action.assert_not_called()

    def test_sin_autoridad_no_envia(self):
        action=Mock()
        with self.assertRaises(ValueError):E.ejecutar('gmgn-sol','wallet',action)
        action.assert_not_called()

    def test_firma_alterada_no_envia(self):
        with self.autoridad(lambda x:{**x,'firma':'0'*64}),self.assertRaises(ValueError):E.autorizar('gmgn-sol','wallet')

    def test_respuesta_de_otro_intento_no_autoriza(self):
        def stale(x):
            x['nonce']='0'*64;x.pop('firma');x['firma']=E._firma('token-prueba',x);return x
        with self.autoridad(stale),self.assertRaises(ValueError):E.autorizar('gmgn-sol','wallet')

    def test_autoridad_distinta_no_autoriza(self):
        with self.autoridad(),patch.dict(os.environ,{'DMN_EJECUTOR_AUTORIDAD':'d'*64}),self.assertRaises(ValueError):E.autorizar('gmgn-sol','wallet')

    def test_red_caida_no_transfiere_propietario(self):
        before=(self.root/'autoridad.json').read_bytes()
        with patch.dict(os.environ,{'DMN_EJECUTOR_URL':'http://127.0.0.1:1'}),self.assertRaises(ValueError):E.autorizar('gmgn-sol','wallet')
        self.assertEqual(before,(self.root/'autoridad.json').read_bytes())

    def test_cuenta_no_asignada_se_deniega(self):
        body={'proveedor':'gmgn-sol','cuenta':'otra','nodo':NODE,'nonce':'d'*64}
        self.assertFalse(E.verificar(body)['permitido'])

    def test_politica_ausente_o_corrupta_no_se_recrea(self):
        f=self.root/'autoridad.json';f.write_text('{')
        with self.assertRaises(ValueError):E.verificar({'proveedor':'gmgn-sol','cuenta':'wallet','nodo':NODE,'nonce':'d'*64})
        self.assertEqual(f.read_text(),'{')

    def test_endpoint_exige_token_aun_en_loopback(self):
        with patch.object(S,'TOKEN',''),self.assertRaises(ValueError):S.r_ejecutor({},dict(proveedor='gmgn-sol',cuenta='wallet',nodo=NODE,nonce='d'*64))

    def test_gmgn_lecturas_no_requieren_ejecutor(self):
        with patch.object(real,'_cli_directo',return_value={}) as cli,patch.object(E,'ejecutar') as gate:
            real._cli('order','strategy','list');real._cli('portfolio','holdings')
        self.assertEqual(cli.call_count,2);gate.assert_not_called()

    def test_gmgn_mutaciones_bloqueadas_sin_autoridad(self):
        with patch.object(real,'_cli_directo') as cli:
            for args in [('swap',),('order','strategy','create'),('order','strategy','cancel')]:
                with self.assertRaises(ValueError):real._cli(*args,'--chain','sol','--from','wallet')
        cli.assert_not_called()

    def test_segundo_proceso_no_toma_la_cuenta(self):
        E._retener('gmgn-sol','wallet')
        code="from pathlib import Path;from trader import ejecutor as E;import sys;E.directorio=lambda:Path(sys.argv[1]);E._retener('gmgn-sol','wallet')"
        result=subprocess.run([sys.executable,'-c',code,str(self.root)],capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0);self.assertIn('otro proceso',result.stderr)

    def test_reinicio_conserva_propietario(self):
        code="from pathlib import Path;from trader import ejecutor as E;import sys,json;E.directorio=lambda:Path(sys.argv[1]);print(json.dumps(E.verificar(dict(proveedor='gmgn-sol',cuenta='wallet',nodo='b'*64,nonce='d'*64))))"
        result=subprocess.run([sys.executable,'-c',code,str(self.root)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertFalse(json.loads(result.stdout)['permitido'])

    def test_bloqueo_se_libera_por_terminacion_real(self):
        code="from pathlib import Path;from trader import ejecutor as E;import sys;E.directorio=lambda:Path(sys.argv[1]);E._retener('gmgn-sol','wallet')"
        result=subprocess.run([sys.executable,'-c',code,str(self.root)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        E._retener('gmgn-sol','wallet')

    def test_cliente_persistente_solo_usa_token_de_misma_central(self):
        (self.root/'cliente.json').write_text(json.dumps({'url':'http://127.0.0.1:8777','autoridad':AID}))
        env={'DMN_EJECUTOR_URL':'','DMN_EJECUTOR_AUTORIDAD':'','DMN_EJECUTOR_TOKEN':'','DMN_TRADER_CENTRAL':'http://127.0.0.1:8777','DMN_TRADER_CENTRAL_TOKEN':'token-central'}
        with patch.dict(os.environ,env):self.assertEqual(E.cliente(),('http://127.0.0.1:8777',AID,'token-central'))
        with patch.dict(os.environ,{**env,'DMN_TRADER_CENTRAL':'http://otro-equipo:8777'}),self.assertRaises(ValueError):E.cliente()

    def test_configuracion_explicita_incompleta_no_hace_fallback(self):
        (self.root/'cliente.json').write_text(json.dumps({'url':'http://127.0.0.1:8777','autoridad':AID}))
        with patch.dict(os.environ,{'DMN_EJECUTOR_URL':'http://127.0.0.1:1','DMN_EJECUTOR_AUTORIDAD':'','DMN_EJECUTOR_TOKEN':''}):self.assertEqual(E.cliente(),('http://127.0.0.1:1','',''))

