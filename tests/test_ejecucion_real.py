import unittest,copy
from unittest.mock import patch
import test_guardia as base
from trader import piloto as P,piloto_fut as F,db

class EjecucionReal(unittest.TestCase):
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown
    def orden(self):
        return dict(id='orden',wallet='wallet',token='token',simbolo='FIX',compra=1,tp=100,sl=10,atr=20,creada=100,entro=100,saldo_antes=.1)
    def test_saldo_desconocido_no_retira_protecciones(self):
        o=self.orden();e={'orden':o}
        with patch.object(P,'_tokens',side_effect=TimeoutError()),patch.object(P,'_cancelar') as cancelar,patch.object(P,'_vender') as vender:
            P._salir(e,o,'salir')
        cancelar.assert_not_called();vender.assert_not_called();self.assertFalse(o.get('vendido'))
    def test_cancelacion_sin_confirmacion_no_vende(self):
        o=self.orden();e={'orden':o}
        with patch.object(P,'_tokens',return_value=1),patch.object(P,'_cancelar'),patch.object(P,'_buscar',return_value=('open',{'order_statistic':{'buy_amount':'100','success_sell_amount':'0'},'status':'open','strategy_status':'running'})),patch.object(P,'_vender') as vender:
            P._salir(e,o,'salir')
        vender.assert_not_called();self.assertEqual(o['cancelacion_estado'],'pendiente_confirmacion')
    def test_tp_durante_cancelacion_no_envia_swap(self):
        o=self.orden();e={'orden':o}
        with patch.object(P,'_tokens',side_effect=[1,0]),patch.object(P,'_cancelar'),patch.object(P,'_buscar',return_value=('history',{'order_statistic':{'buy_amount':'100','success_sell_amount':'0'},'status':'closed','strategy_status':'stopped'})),patch.object(P,'_vender') as vender:
            P._salir(e,o,'salir')
        vender.assert_not_called();self.assertTrue(o['vendido'])
    def test_paginacion_y_ambito_wallet_token(self):
        buena=dict(order_id='orden',wallet_address='wallet',base_token='token')
        with patch.object(P.real,'_cli',side_effect=[{'list':[],'next_page_token':'pagina-2'},{'list':[buena]}]) as cli:
            tipo,o=P._buscar('orden','wallet','token')
        self.assertEqual(tipo,'open');self.assertEqual(o,buena)
        self.assertIn('pagina-2',cli.call_args.args);self.assertIn('--from',cli.call_args.args)
    def test_orden_otra_wallet_no_se_adopta(self):
        mala=dict(order_id='orden',wallet_address='otra',base_token='token')
        with patch.object(P.real,'_cli',return_value={'list':[mala]}):tipo,o=P._buscar('orden','wallet','token')
        self.assertIsNone(o);self.assertEqual(tipo,'desconocida')
    def test_compra_se_confirma_con_ejecucion_no_solo_buy_low(self):
        self.assertTrue(P._comprada({'open_sign_hash':'hash-confirmado'}))
        self.assertTrue(P._comprada({'order_statistic':{'buy_amount':'1000'}}))
        self.assertFalse(P._comprada({'order_statistic':{'buy_amount':'NaN'}}))
    def test_historial_con_tokens_no_finaliza_operacion(self):
        o=self.orden();e={'orden':o}
        remota={'order_statistic':{'buy_amount':'100','success_sell_amount':'0'},'status':'closed','open_sign_hash':'hash','condition_orders':[{'order_type':'profit_stop','status':'success'}]}
        with patch.object(P,'_buscar',return_value=('history',remota)),patch.object(P,'_tokens',return_value=1): P._seguir(e,P.DEFECTO)
        self.assertIs(e['orden'],o);self.assertEqual(o['estado'],'pendiente_conciliacion');self.assertFalse(e.get('ops'))
    def test_stop_dinamico_no_retrocede(self):
        o=self.orden();o['stop_dyn']=1.95;o['max']=2.1;e={'orden':o}
        with patch.object(P,'_precio',return_value=1.9),patch.object(P,'_salir') as salir:
            P._gestionar(e,P.DEFECTO,o)
        self.assertGreaterEqual(o['stop_dyn'],1.95);salir.assert_called_once()
    def test_futuros_riesgo_nan_impide_entradas(self):
        cu=dict(nombre='Sim101',saldo=1000,realizado=float('nan'),no_realizado=0)
        with patch.object(F,'cfg',return_value=F.DEFECTO),patch.object(F,'_est',return_value={}),patch.object(F,'puente',return_value={'cuentas':[cu]}),self.assertRaises(F.SinPuente):F.paso()
    def test_cerrar_todo_fallido_sigue_pendiente(self):
        cu=dict(nombre='Sim101',conexion='sim',conectada=True,simulada=True,saldo=1000,realizado=0,no_realizado=0,posiciones=[])
        o=dict(cuenta='Sim101',nt='MES 12-26',full='MES DEC26',ts=10**20,hold=10)
        e={'abiertas':{'x':o}}
        def puente(r,b=None):
            if r=='/estado':return {'cuentas':[cu]}
            raise F.SinPuente('fallo cierre')
        with patch.object(F,'cfg',return_value={**F.DEFECTO,'activo':False,'cerrar_todo':100}),patch.object(F,'_est',return_value=e),patch.object(F,'puente',side_effect=puente):F.paso()
        self.assertNotEqual(e.get('cerrar_hecho'),100)
    def test_libro_danado_bloquea_entradas(self):
        e={}
        with patch.object(F,'cfg',return_value=F.DEFECTO),patch.object(F,'_est',return_value=e),patch.object(F,'puente',return_value={'cuentas':[],'libro_sano':False}),patch('trader.fabrica_fut.senales_ahora') as senales:F.paso()
        senales.assert_not_called();self.assertIn('conciliacion',e['error'])
    def test_apagado_durante_ia_impide_envio(self):
        with patch.object(P,'cfg',return_value={**P.DEFECTO,'activo':False}),patch.object(P,'_crear') as crear,self.assertRaises(ValueError):
            P._crear_seguro({'saldo_inicio':.1},'wallet',{'token':'token'},1)
        crear.assert_not_called()
    def test_wallet_cambiada_no_reutiliza_presupuesto(self):
        with patch.object(P,'cfg',return_value={**P.DEFECTO,'activo':True}),patch.object(P,'_saldo',return_value=(.1,'wallet')),self.assertRaises(ValueError):
            P._autorizar_entrada({'saldo_inicio':.1,'riesgo_wallet':'otra'},'wallet')
    def test_salida_solicitada_no_se_olvida_si_desaparece_senal(self):
        o=self.orden();o['salida_solicitada']='limite';o['reintentos']=50
        with patch.object(P,'_salir') as salir: P._gestionar({'orden':o},P.DEFECTO,o)
        salir.assert_called_once()
    def test_futuros_apagado_despues_de_senal_no_envia(self):
        c={**F.DEFECTO,'activo':True}
        with patch.object(F,'cfg',return_value={**c,'real_activo':False}),self.assertRaises(F.SinPuente):
            F._autorizar_envio(c,{},'real',True,1)
    def test_simulacion_no_acepta_cuenta_real_aunque_se_llame_sim(self):
        c={**F.DEFECTO,'activo':True,'cuenta_sim':'Sim-Falsa'}
        est={'contrato':'dmn-puente/2','cuentas':[{'nombre':'Sim-Falsa','conectada':True,'simulada':False}]}
        with patch.object(F,'cfg',return_value=c),patch.object(F,'puente',return_value=est),self.assertRaises(F.SinPuente):
            F._autorizar_envio(c,{},'Sim-Falsa',False,1)
    def test_libro_no_pierde_perdidas_al_recortar_historial_visible(self):
        import time
        ops=[]
        for i in range(310):
            op=dict(cuenta='Real',orden=str(i),nt='MES',full='MES',cerrada=int(time.time()),real=True,pnl=-1)
            F._registrar_resultado(op);ops.append(op)
        with patch.object(F,'cfg',return_value={'cuenta_real':'Real'}):
            self.assertEqual(F._pnl_hoy({'ops':ops[-299:]}),-310)
    def test_resultado_durable_no_se_duplica_y_correccion_bloquea(self):
        import time
        op=dict(cuenta='Real',orden='x',nt='MES',cerrada=int(time.time()),real=True,pnl=-2)
        F._registrar_resultado(op);F._registrar_resultado(op)
        self.assertEqual(db.uno('SELECT COUNT(*) n FROM fut_resultados')['n'],1)
        with self.assertRaises(F.SinPuente):F._registrar_resultado({**op,'pnl':10})
    def test_venta_solo_cantidad_propia_no_porcentaje_wallet(self):
        with patch.object(P.real,'_cli',return_value={'order_id':'swap'}) as cli:
            P._vender({**self.orden(),'cantidad_salida_atomica':'9007199254740993'})
        self.assertIn('--amount',cli.call_args.args)
        self.assertIn('9007199254740993',cli.call_args.args)
        self.assertNotIn('--percent',cli.call_args.args)
    def test_cantidad_propia_exacta_sin_redondeo_float(self):
        self.assertEqual(P._cantidad_propia({'order_statistic':{'buy_amount':'9007199254740993','success_sell_amount':'1'}}),9007199254740992)
        self.assertEqual(P._cantidad_propia({'order_statistic':{'buy_amount':str(2**256-1),'success_sell_amount':'1'}}),2**256-2)
        for st in ({},{'buy_amount':'NaN','success_sell_amount':'0'},{'buy_amount':'1','success_sell_amount':'2'}):
            with self.assertRaises(ValueError):P._cantidad_propia({'order_statistic':st})

if __name__=='__main__':unittest.main()
