import os, sys, tempfile, unittest, subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DMN_TRADER_DB'] = str(Path(tempfile.mkdtemp(prefix='dmn-regresion-')) / 'prueba.db')
from trader import motor, db, datos, piloto, backtest, fabrica, ia, alpaca, piloto_fut, metricas

def barra(px=100):
    return dict(t=1800000000, o=px, h=px, l=px, c=px, moneda='USD', abierto=True)

class Reparacion(unittest.TestCase):
    def setUp(self):
        self.red = patch('urllib.request.urlopen', side_effect=AssertionError('red prohibida'))
        self.red.start()
        self.proceso = patch.object(subprocess, 'run', side_effect=AssertionError('ejecucion prohibida'))
        self.proceso.start()
        self.barra = patch.object(motor, 'barra', return_value=barra())
        self.barra.start()
        self.addCleanup(patch.stopall)
    def cuenta(self, capital=1000, **cfg):
        return motor.crear_cuenta('regresion', capital, config={'deslizamiento_pct': 0, **cfg})['id']
    def test_nan_inf_y_booleanos_no_crean_ordenes(self):
        c=self.cuenta(exigir_stop=True)
        for campo in ('sl','tp','cantidad','limite','stop','trail_pct','importe'):
            for v in (float('nan'),float('inf'),-1,True):
                kw={'cantidad':1,'sl':90, campo:v}
                with self.assertRaises(motor.ErrorOrden): motor.enviar(c,'FIX','buy',**kw)
        self.assertFalse(db.q('SELECT * FROM ordenes WHERE cuenta_id=?',(c,)))
    def test_reserva_capital_y_cancelacion_libera(self):
        c=self.cuenta(100)
        with patch.object(motor,'barra',return_value=barra(2)):
            o=motor.enviar(c,'A','buy',60,tipo='limite',limite=1)
            with self.assertRaises(motor.ErrorOrden): motor.enviar(c,'B','buy',60,tipo='limite',limite=1)
            motor.cancelar(c,o['id'])
            motor.enviar(c,'B','buy',60,tipo='limite',limite=1)
        with patch.object(motor,'barra',return_value=barra(1)): motor.procesar(c)
        self.assertGreaterEqual(motor._fila(c)['efectivo'],0)
    def test_fallo_revierte_caja_trade_y_ejecucion(self):
        c=self.cuenta(); original=db.ex
        def falla(sql,args=()):
            if sql.startswith('INSERT INTO posiciones'): raise RuntimeError('fallo inyectado')
            return original(sql,args)
        with patch.object(db,'ex',side_effect=falla), self.assertRaises(RuntimeError): motor.enviar(c,'A','buy',1)
        self.assertEqual(motor._fila(c)['efectivo'],1000)
        self.assertFalse(db.q('SELECT * FROM trades WHERE cuenta_id=?',(c,)))
        motor.procesar(c)
        self.assertEqual(motor._fila(c)['efectivo'],900)
    def test_cancelada_durante_cotizacion_no_se_llena(self):
        c=self.cuenta()
        with patch.object(motor,'barra',return_value=barra(2)): o=motor.enviar(c,'A','buy',10,tipo='limite',limite=1)
        def cancela(*a): motor.cancelar(c,o['id']); return barra(1)
        with patch.object(motor,'barra',side_effect=cancela): motor.procesar(c)
        self.assertEqual(db.uno('SELECT estado FROM ordenes WHERE id=?',(o['id'],))['estado'],'cancelada')
        self.assertEqual(motor._fila(c)['efectivo'],1000)
    def test_gap_no_excede_poder_compra(self):
        c=self.cuenta(100)
        with patch.object(motor,'barra',return_value={**barra(100),'abierto':False}): o=motor.enviar(c,'A','buy',1)
        with patch.object(motor,'barra',return_value=barra(120)): motor.procesar(c)
        self.assertEqual(motor._fila(c)['efectivo'],100)
        self.assertEqual(db.uno('SELECT estado FROM ordenes WHERE id=?',(o['id'],))['estado'],'cancelada')
    def test_stop_anterior_sobrevive_fallo(self):
        c=self.cuenta(); motor.enviar(c,'A','buy',1,sl=90,tp=120)
        with patch.object(motor,'barra',side_effect=RuntimeError('sin precio')), self.assertRaises(RuntimeError): motor.mover_stop(c,'A',stop=95)
        self.assertEqual(db.uno("SELECT stop FROM ordenes WHERE cuenta_id=? AND tipo='stop' AND estado='pendiente'",(c,))['stop'],90)
    def test_transacciones_anidadas(self):
        with db.transaccion():
            db.poner_ajuste('externo',1)
            try:
                with db.transaccion(): db.poner_ajuste('interno',1); raise RuntimeError()
            except RuntimeError: pass
        self.assertEqual(db.ajuste('externo'),1); self.assertIsNone(db.ajuste('interno'))
    def test_configuracion_invalida(self):
        for cfg in ({'permitir_cortos':'false'},{'apalancamiento':float('nan')},{'comision_por_tipo':{'X':float('inf')}}):
            with self.assertRaises(motor.ErrorOrden): self.cuenta(**cfg)
    def test_pnl_backtest_concilia_equity(self):
        v=[dict(barra(),t=1700000000+i*86400,v=1) for i in range(5)]
        r=backtest.ejecutar(v,'comprar_mantener',capital=1000,comision_pct=1,deslizamiento_pct=0,comision_fija=2)
        self.assertAlmostEqual(r['final']-1000,r['beneficio_neto'],places=2)
    def test_saldo_desconocido_no_certifica_venta(self):
        e={}; o=dict(wallet='w',token='t',id='x',simbolo='FIX'); e['orden']=o
        with patch.object(piloto,'_cancelar'),patch.object(piloto.onchain,'_api',side_effect=TimeoutError()),patch.object(piloto,'_vender') as vender:
            piloto._salir(e,o,'prueba')
        self.assertFalse(o.get('vendido')); vender.assert_not_called()
    def test_swap_ambiguo_no_se_duplica_ni_se_da_por_vendido(self):
        e={}; o=dict(wallet='w',token='t',id='x',simbolo='FIX'); e['orden']=o
        with patch.object(piloto,'_cancelar'),patch.object(piloto,'_buscar',return_value=('history',{'order_statistic':{'buy_amount':'100','success_sell_amount':'0'},'status':'closed','strategy_status':'stopped'})),patch.object(piloto,'_tokens',return_value=1),patch.object(piloto,'_vender',side_effect=TimeoutError()) as vender:
            piloto._salir(e,o,'prueba'); piloto._salir(e,o,'prueba')
        self.assertEqual(vender.call_count,1); self.assertFalse(o.get('vendido'))
        self.assertTrue(db.ajuste(piloto.EST)['orden']['venta_intento'])
    def test_entrada_gmgn_ambigua_persiste_y_no_reintenta(self):
        e={'saldo':.1}; candidato={'token':'t','simbolo':'FIX','precio':1,'atr':2}
        with patch.object(piloto,'_autorizar_entrada'),patch.object(piloto,'_crear',side_effect=TimeoutError()) as enviar:
            with self.assertRaises(TimeoutError): piloto._crear_seguro(e,'wallet',candidato,1)
            with self.assertRaises(RuntimeError): piloto._crear_seguro(e,'wallet',candidato,1)
        self.assertEqual(enviar.call_count,1)
        self.assertTrue(db.ajuste(piloto.EST)['intencion_entrada'])
    def test_config_no_borra_perdida(self):
        e=dict(saldo_inicio=.1,inicio_cfg=1,ops=[])
        with patch.object(piloto,'cfg',return_value={**piloto.DEFECTO,'activo':True,'cambiado':2}),patch.object(piloto,'_est',return_value=e),patch.object(piloto,'_saldo',return_value=(.05,'w')),patch.object(piloto,'_ult',{'scan':10000,'smart':10000}),patch.object(piloto.time,'time',return_value=10000),patch.object(piloto,'poner_cfg') as cambiar:
            piloto.paso()
        self.assertEqual(e['saldo_inicio'],.1); self.assertTrue(cambiar.called)
    def test_datos_nt_rechazan_obsoletas_corruptas_y_duplicadas(self):
        for v in ([dict(barra(),t=1,v=1)],[dict(barra(),t=999990,v=1,h=50)],[dict(barra(),t=999990,v=1)]*2):
            with patch.object(datos.time,'time',return_value=1000000),self.assertRaises(datos.ErrorDatos): datos.recibir_nt({'MES=F':{'1m':v}})
    def test_mutacion_no_hereda_identidad(self):
        p=dict(id='antiguo',desde=100,conds=[['rsi','<',40]],sl_k=2,rr=2,hold=12)
        with patch.object(fabrica.random,'random',return_value=.95),patch.object(fabrica.random,'choice',return_value='rr'),patch.object(fabrica.random,'uniform',return_value=1.1): h=fabrica._mutar(p,object())
        self.assertNotIn('id',h); self.assertNotIn('desde',h); self.assertEqual(p['rr'],2)
    def test_umbral_no_ve_test(self):
        d=SimpleNamespace(F=np.array([[1.]]*70+[[1000.]]*30),ts=np.arange(100),col={'rsi':0},corte=70)
        with patch.object(fabrica.random,'uniform',return_value=.9): self.assertEqual(fabrica._umbral(d,'rsi'),1)
    def test_alpaca_timeout_persiste_y_concilia_sin_reenviar(self):
        c=self.cuenta(); motor.enviar(c,'A','buy',1)
        pos=motor.posiciones(c,False)
        def api(m,r,b=None):
            if m=='POST': raise TimeoutError()
            return {'status':'filled','filled_qty':'0.1','filled_avg_price':'100','id':'broker-1'}
        with patch('trader.agente.aptitud'),patch.object(alpaca,'lider',return_value={'cuenta_id':c,'nombre':'FIX'}),patch.object(alpaca,'config',return_value={'nominal':10}),patch.object(alpaca,'_api',side_effect=api) as enviar:
            alpaca.espejo(); alpaca.espejo()
        self.assertEqual(sum(x.args[0]=='POST' for x in enviar.call_args_list),1)
        self.assertEqual(db.uno('SELECT estado FROM alp_espejo WHERE trade_id=?',(pos[0]['trade_id'],))['estado'],'abierta')

    def test_alpaca_cierre_solo_cantidad_propia(self):
        c=self.cuenta(); motor.enviar(c,'A','buy',1)
        def api(m,r,b=None):
            return {'status':'filled','filled_qty':'0.1','filled_avg_price':'100','id':'broker-2'}
        with patch.object(alpaca,'lider',return_value={'cuenta_id':c,'nombre':'FIX'}),patch.object(alpaca,'config',return_value={'nominal':10}),patch.object(alpaca,'_api',side_effect=api) as enviar:
            alpaca.espejo(); alpaca.espejo(); motor.cerrar(c,'A'); alpaca.espejo()
        cierres=[x for x in enviar.call_args_list if x.args[0]=='POST' and x.args[2].get('side')=='sell']
        self.assertEqual(len(cierres),1); self.assertEqual(cierres[0].args[2]['qty'],'0.1')
        self.assertFalse(any(x.args[0]=='DELETE' for x in enviar.call_args_list))

    def test_ia_conserva_pesos_medidos_y_no_promueve_filas_sin_fechas(self):
        n=max(200,ia.MIN_TRADES)
        filas=[({'rsi':30},{},1,1 if i%2 else -1) for i in range(n)]
        with patch.object(ia,'_entrenar',wraps=ia._entrenar) as entrenar:
            m=ia.entrenar(filas=filas)
        self.assertEqual(entrenar.call_count,1)
        self.assertFalse(m['ok']); self.assertTrue(m['modelo_congelado'])
        self.assertEqual(m['n_entreno'],int(n*.75)); self.assertEqual(m['n_validacion'],n-int(n*.75))

    def test_bootstrap_reproducible_con_distintos_finales(self):
        a=metricas.bootstrap_bloques([10,-8,5,-4,6],100,n=100)
        self.assertEqual(a,metricas.bootstrap_bloques([10,-8,5,-4,6],100,n=100))
        self.assertLess(a['final_5'],a['final_95'])
        self.assertFalse(metricas.montecarlo([10,-8,5,-4,6],100)['incertidumbre_beneficio'])

    def test_futuros_timeout_guarda_intencion_y_no_repite(self):
        e={}; c={**piloto_fut.DEFECTO,'activo':True,'real_activo':False}
        cu=dict(nombre='Sim101',conexion='SIM',conectada=True,simulada=True,saldo=1000,realizado=0,no_realizado=0,posiciones=[])
        x=dict(nt='MES 12-26',full='MES DEC26',inst='MES=F',lado=1,sl_ticks=10,tp_ticks=20,est_id='FIX',hold=12,estrategia='FIX',riesgo_usd=12.5)
        def puente(r,b=None):
            if r=='/estado': return dict(contrato='dmn-puente/2',cuentas=[cu])
            if r=='/cerradas': return {'cerradas':[]}
            if r=='/orden': raise piloto_fut.SinPuente('timeout')
            raise AssertionError(r)
        with patch.object(piloto_fut,'cfg',return_value=c),patch.object(piloto_fut,'_est',return_value=e),patch.object(piloto_fut,'_barras',return_value=[]),patch('trader.fabrica_fut._candidatas',return_value=[]),patch('trader.fabrica_fut.promovidas',return_value=[]),patch('trader.fabrica_fut.senales_ahora',return_value=[x]),patch.object(piloto_fut,'puente',side_effect=puente) as enviar:
            piloto_fut.paso(); piloto_fut.paso()
        self.assertEqual(sum(x.args[0]=='/orden' for x in enviar.call_args_list),1)
        self.assertEqual(e['abiertas']['Sim101|MES 12-26']['estado'],'pendiente_conciliacion')
        self.assertTrue(db.ajuste(piloto_fut.EST)['abiertas'])

if __name__=='__main__': unittest.main()
