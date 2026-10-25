import unittest,time,json,datetime
from unittest.mock import patch
import test_guardia as base
from trader import db,observatorio_gmgn as O,rpc_lectura as R,supervision_gmgn as S,contabilidad_gmgn as C,piloto as P,mercado_gmgn as M

class Observatorio(unittest.TestCase):
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown
    def candidato(self):return {'est_id':'M1','token':'token','hold':1,'precio':1,'tp':20,'sl':10}
    def cfg(self):return dict(P.DEFECTO)
    def quotes(self):return [{'minimo':100,'esperado':110,'ts':time.monotonic()},{'minimo':9900000,'esperado':9950000,'ts':time.monotonic()}]
    def op(self):return dict(wallet='wallet',token='token',id_entrada='op')
    def crear(self):
        with patch.object(M,'cotizar',side_effect=self.quotes()):O.evaluar_senal('wallet',self.candidato(),self.cfg())
        return db.uno('SELECT * FROM gmgn_sombra')
    def test_sombra_solo_cotiza_con_piloto_apagado(self):
        with patch.object(M,'cotizar',side_effect=self.quotes()) as q:O.evaluar_senal('wallet',self.candidato(),self.cfg())
        self.assertEqual(q.call_count,2);self.assertIsNone(db.ajuste(P.EST));self.assertFalse(P.cfg()['activo'])
    def test_misma_senal_no_se_duplica_tras_recuperar_sqlite(self):
        self.crear();db._con.close();db._con=None
        with patch.object(M,'cotizar') as q:O.evaluar_senal('wallet',self.candidato(),self.cfg())
        q.assert_not_called();self.assertEqual(db.uno('SELECT COUNT(*) n FROM gmgn_sombra')['n'],1)
    def test_timeout_entrada_no_se_reinterpreta_como_cotizada(self):
        with patch.object(M,'cotizar',side_effect=TimeoutError('SECRETO')):O.evaluar_senal('wallet',self.candidato(),self.cfg())
        self.assertEqual(db.uno('SELECT estado FROM gmgn_sombra')['estado'],'sin_cotizacion')
        self.assertNotIn('SECRETO',json.dumps(O.informe()))
    def test_precio_de_salida_se_consulta_solo_al_vencer(self):
        row=self.crear()
        with patch.object(M,'cotizar') as q:O.completar()
        q.assert_not_called()
        with patch.object(O.time,'time',return_value=row['vence']+1),patch.object(M,'cotizar',return_value={'esperado':11000000,'minimo':10900000}):O.completar()
        self.assertEqual(db.uno('SELECT estado FROM gmgn_sombra')['estado'],'completa')
        self.assertAlmostEqual(O.informe()['media_cotizada_pct'],10)
    def test_reinicio_tardio_no_inventa_resultado_del_horizonte(self):
        row=self.crear()
        with patch.object(O.time,'time',return_value=row['vence']+301),patch.object(M,'cotizar') as q:O.completar()
        q.assert_not_called();self.assertEqual(db.uno('SELECT estado FROM gmgn_sombra')['estado'],'horizonte_perdido')
    def test_limite_de_peticiones_reintenta_consulta_no_orden(self):
        row=self.crear()
        with patch.object(O.time,'time',return_value=row['vence']+1),patch.object(M,'cotizar',side_effect=TimeoutError):O.completar()
        row=db.uno('SELECT * FROM gmgn_sombra');self.assertEqual(row['estado'],'observando');self.assertEqual(row['proxima'],row['vence']+61)
    def test_consulta_interrumpida_antes_de_persistir_respuesta(self):
        O.preparar();db.ex("INSERT INTO gmgn_sombra VALUES('x','wallet','token','M1',1,2,2,'consultando','{}')");O.completar()
        self.assertEqual(db.uno('SELECT estado FROM gmgn_sombra')['estado'],'interrumpida')
    def test_ruta_demasiado_costosa_rechazada(self):
        q=self.quotes();q[1]['minimo']=1
        with patch.object(M,'cotizar',side_effect=q):O.evaluar_senal('wallet',self.candidato(),self.cfg())
        self.assertEqual(db.uno('SELECT estado FROM gmgn_sombra')['estado'],'rechazada')
    def test_quotes_alejadas_en_tiempo_no_son_comparables(self):
        q=self.quotes();q[0]['ts']-=60
        with patch.object(M,'cotizar',side_effect=q):O.evaluar_senal('wallet',self.candidato(),self.cfg())
        self.assertEqual(db.uno('SELECT estado FROM gmgn_sombra')['estado'],'sin_cotizacion')
    def test_rpc_no_admite_metodos_de_envio(self):
        with self.assertRaises(ValueError):R.consultar('sendTransaction',[])
    def test_rpc_cuentas_de_otro_propietario_rechazadas(self):
        response={'context':{'slot':1},'value':[{'pubkey':'ata','account':{'data':{'parsed':{'info':{'owner':'otro','mint':'token','tokenAmount':{'amount':'100','decimals':6}}}}}}]}
        with patch.object(R,'consultar',return_value=response),self.assertRaises(ValueError):R.tokens('wallet','token')
    def test_rpc_cuentas_duplicadas_no_suman_dos_veces(self):
        x={'pubkey':'ata','account':{'data':{'parsed':{'info':{'owner':'wallet','mint':'token','tokenAmount':{'amount':'100','decimals':6}}}}}}
        with patch.object(R,'consultar',return_value={'context':{'slot':1},'value':[x,x]}),self.assertRaises(ValueError):R.tokens('wallet','token')
    def test_firma_ausente_no_es_fallo_y_no_autoriza_reenvio(self):
        with patch.object(R,'consultar',return_value={'value':[None]}):self.assertEqual(R.firmas(['sig']),[None])
    def test_firma_sin_estado_no_se_acepta(self):
        with patch.object(R,'consultar',return_value={'value':[{}]}),self.assertRaises(ValueError):R.firmas(['sig'])
    def test_saldo_distinto_bloquea_entradas_y_persiste(self):
        with patch.object(R,'tokens',return_value={'atomico':'100','decimales':2,'slot':1}):result=O.conciliar(self.op(),2)
        self.assertEqual(result['estado'],'discrepancia');db._con.close();db._con=None
        with self.assertRaises(ValueError):S.admitir('wallet')
    def test_igualdad_de_saldo_no_declara_pnl(self):
        with patch.object(R,'tokens',return_value={'atomico':'100','decimales':2,'slot':1}):result=O.conciliar(self.op(),1)
        self.assertEqual(result['estado'],'coincide');self.assertNotIn('pnl_sol',result)
    def test_rpc_caida_preserva_posicion_y_bloquea_nuevas_entradas(self):
        o=self.op();db.poner_ajuste(P.EST,{'orden':o})
        with patch.object(R,'tokens',side_effect=TimeoutError('PRIVADO')):result=O.conciliar(o,1)
        self.assertEqual(result['estado'],'no_disponible');self.assertEqual(P._est()['orden'],o)
        self.assertNotIn('PRIVADO',json.dumps(result))
        with self.assertRaises(ValueError):S.admitir('wallet')
    def test_firma_fallida_contradice_confirmacion_broker(self):
        o={**self.op(),'entrada_confirmada':True,'hash_entrada':'3'*88};C.anotar(o,'3'*88,'entrada')
        with patch.object(R,'tokens',return_value={'atomico':'0','decimales':None,'slot':1}),patch.object(R,'firmas',return_value=[{'confirmationStatus':'finalized','err':{'x':1}}]):result=O.conciliar(o,0)
        self.assertEqual(result['estado'],'discrepancia')
    def test_movimientos_previos_son_linea_base(self):
        with patch.object(R,'consultar',return_value=[{'signature':'vieja'}]):result=O.movimientos('wallet')
        self.assertEqual(result['nuevas_no_atribuidas'],0);self.assertEqual(result['cobertura'],'linea_base')
    def test_movimiento_externo_no_se_atribuye_a_ganancia(self):
        with patch.object(R,'consultar',return_value=[{'signature':'vieja'}]):O.movimientos('wallet')
        with patch.object(R,'consultar',return_value=[{'signature':'nueva'},{'signature':'vieja'}]):result=O.movimientos('wallet')
        self.assertEqual(result['nuevas_no_atribuidas'],1);self.assertFalse(result['beneficio_atribuido'])
    def test_historial_truncado_no_declara_cobertura_completa(self):
        with patch.object(R,'consultar',return_value=[{'signature':'vieja'}]):O.movimientos('wallet')
        with patch.object(R,'consultar',return_value=[{'signature':'nueva'}]):self.assertEqual(O.movimientos('wallet')['cobertura'],'incompleta')
    def test_informe_filtra_dia_utc_y_no_filtra_secretos_hacia_panel(self):
        with patch.object(O.time,'time',return_value=100):O.decision('sombra_rechazada',motivo='cotizacion_no_disponible',datos={'api_key':'SECRETO'})
        self.assertEqual(O.informe('1970-01-01')['decisiones']['sombra_rechazada'],1)
        self.assertEqual(O.informe('1970-01-02')['decisiones'],{})
        self.assertNotIn('SECRETO',json.dumps(O.informe('1970-01-01')))
    def test_sin_recibos_no_inventa_coste_ni_minimo(self):
        c=O.costes();self.assertIsNone(c['comision_red_sol']);self.assertIsNone(c['importe_minimo_viable_sol']);self.assertIsNone(c['minimo_parcial_solo_red_sol'])
    def test_costes_parciales_no_sustituyen_coste_simulacion(self):
        from test_contabilidad_gmgn import receipt,SIG
        o=self.op();C.anotar(o,SIG,'entrada');parsed=C.interpretar(receipt(),SIG,'wallet','token','entrada');C.guardar(SIG,'wallet','op','entrada',parsed)
        c=O.costes();self.assertEqual(c['comision_red_sol'],.000005);self.assertGreater(c['minimo_parcial_solo_red_sol'],0);self.assertIsNone(c['importe_minimo_viable_sol']);self.assertEqual(c['coste_simulacion_pct'],6.0)
    def test_cierre_no_borra_discrepancia_de_cadena(self):
        S.registrar('wallet','op','conciliacion_cadena','Saldo distinto');S.resolver_posicion('wallet','op')
        with self.assertRaises(ValueError):S.admitir('wallet')
    def test_fallo_de_escaneo_no_impide_informe_ni_movimientos(self):
        with patch.object(O.real,'_wallet',return_value='wallet'),patch.object(O,'escanear',side_effect=TimeoutError),patch.object(O,'movimientos') as movements,patch.object(O,'completar') as complete,patch.object(O.time,'sleep',side_effect=StopIteration),self.assertRaises(StopIteration):O._bucle()
        complete.assert_called_once();movements.assert_called_once()
        self.assertEqual(db.ajuste('gmgn_observatorio_servicio')['estado'],'consulta_pendiente')
        self.assertEqual(db.uno('SELECT COUNT(*) n FROM gmgn_informes')['n'],2)
    def test_posicion_real_pausa_consultas_de_investigacion(self):
        db.poner_ajuste(P.EST,{'orden':self.op()})
        with patch.object(O.real,'_wallet',return_value='wallet'),patch.object(O,'conciliar',return_value={'estado':'coincide'}) as reconcile,patch.object(O,'escanear') as scan,patch.object(O,'completar') as complete,patch.object(O,'movimientos'),patch.object(O.time,'sleep',side_effect=StopIteration),self.assertRaises(StopIteration):O._bucle()
        reconcile.assert_called_once();scan.assert_not_called();complete.assert_not_called()
    def test_control_compara_foto_disponible_sin_usar_futuro(self):
        from trader import fabrica as F
        F.preparar();row=self.crear();end=row['vence']+1
        # Columnas explicitas para no depender de los campos historicos opcionales.
        db.ex("INSERT INTO fab_muestras(token,simbolo,ts,precio,f,origen) VALUES('token','T',?,1.2,'{}','vivo')",(end-1,))
        db.ex("INSERT INTO fab_muestras(token,simbolo,ts,precio,f,origen) VALUES('token','T',?,100,'{}','vivo')",(end+1,))
        with patch.object(O.time,'time',return_value=end),patch.object(M,'cotizar',return_value={'esperado':11000000,'minimo':10900000}):O.completar()
        data=json.loads(db.uno('SELECT datos FROM gmgn_sombra')['datos']);self.assertAlmostEqual(data['control_hold_pct'],14)
        self.assertEqual(data['control_foto_ts'],end-1)
