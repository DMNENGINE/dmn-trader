import unittest,time,copy,json
from unittest.mock import patch
import test_guardia as base
from trader import piloto as P,mercado_gmgn as M,real,ejecutor as E,db
from trader.presupuesto_gmgn import EntradaNoEnviada
CLI_ORIGINAL=real._cli

class Mercado(unittest.TestCase):
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown

    def config(self):return {**P.DEFECTO,'activo':True}
    def candidato(self):return dict(token='token',simbolo='TEST',precio=1,tp=20,sl=10,atr=5,estrategia='fixture',est_id='x',hold=4,vivo={'medio':1,'n':5})
    def estado(self):return dict(saldo_inicio=.1,saldo=.1)
    def cotizacion(self,src,dst,amount):
        return dict(input_token=src,output_token=dst,input_amount=amount,output_amount='1100' if src==real.SOL else '9900000',min_output_amount='1000' if src==real.SOL else '9800000',slippage=10)
    def respuesta(self,**kw):return dict(order_id='swap-id',input_token=real.SOL,output_token='token',confirmation={'state':'confirmed'},hash='hash-compra',filled_input_amount='10000000',filled_output_amount='1000',**kw)
    def orden(self):return dict(self.candidato(),id='swap-id',id_entrada='swap-id',modo='mercado',wallet='wallet',creada=int(time.time()),compra=1,sol=.01,cantidad_lamports='10000000',saldo_antes=.1)
    def proteccion(self):return dict(order_id='strategy-id',wallet_address='wallet',base_token='token',status='open',strategy_status='running',open_sign_hash='hash-compra',open_price=1,order_statistic={'buy_amount':'1000'},condition_orders=[dict(order_type='profit_stop',side='sell',status='check',price_scale='20',sell_ratio='100'),dict(order_type='loss_stop',side='sell',status='check',price_scale='10',sell_ratio='100')])
    def cli(self,args):
        if args[:2]==('order','quote'):
            return self.cotizacion(args[args.index('--input-token')+1],args[args.index('--output-token')+1],args[args.index('--amount')+1])
        return {'order_id':'swap-id','strategy_order_id':'strategy-id'}

    def test_entrada_inmediata_es_un_swap_con_protecciones(self):
        e=self.estado()
        with patch.object(P,'cfg',return_value=self.config()),patch.object(P,'_saldo',return_value=(.1,'wallet')),patch.object(P,'_tokens',return_value=0),patch.object(real,'_cli',side_effect=lambda *a,**k:self.cli(a)) as cli:
            M.abrir(e,'wallet',self.candidato(),1)
        swaps=[x for x in cli.call_args_list if x.args[0]=='swap'];self.assertEqual(len(swaps),1)
        args=swaps[0].args;conditions=json.loads(args[args.index('--condition-orders')+1])
        self.assertEqual({x['order_type'] for x in conditions},{'profit_stop','loss_stop'})
        self.assertEqual(args[args.index('--sell-ratio-type')+1],'buy_amount');self.assertIn('--anti-mev',args)
        self.assertEqual(e['orden']['id_entrada'],'swap-id');self.assertNotIn('intencion_entrada',e)
        self.assertFalse(e['orden'].get('proteccion_verificada',False))

    def test_timeout_no_repite_entrada(self):
        e=self.estado();sent=[]
        def cli(*args,**kw):
            if args[0]=='swap':sent.append(1);raise TimeoutError()
            return self.cli(args)
        with patch.object(P,'cfg',return_value=self.config()),patch.object(P,'_saldo',return_value=(.1,'wallet')),patch.object(P,'_tokens',return_value=0),patch.object(real,'_cli',side_effect=cli):
            with self.assertRaises(TimeoutError):M.abrir(e,'wallet',self.candidato(),1)
            with self.assertRaises(ValueError):M.abrir(e,'wallet',self.candidato(),1)
        self.assertEqual(sent,[1]);self.assertIn('intencion_entrada',e)

    def test_token_preexistente_no_se_mezcla(self):
        with patch.object(P,'_autorizar_entrada',return_value=self.config()),patch.object(P,'_tokens',return_value=1),patch.object(real,'_cli') as cli,self.assertRaises(EntradaNoEnviada):M.abrir(self.estado(),'wallet',self.candidato(),1)
        cli.assert_not_called()

    def test_quote_otro_token_no_envia(self):
        e=self.estado();q=self.cotizacion(real.SOL,'otro','10000000')
        with patch.object(P,'_autorizar_entrada',return_value=self.config()),patch.object(P,'_tokens',return_value=0),patch.object(real,'_cli',return_value=q) as cli,self.assertRaises(EntradaNoEnviada):M.abrir(e,'wallet',self.candidato(),1)
        self.assertEqual(cli.call_count,1);self.assertNotIn('intencion_entrada',e)

    def test_ruta_de_salida_supera_riesgo_no_envia(self):
        e=self.estado();reverse=self.cotizacion('token',real.SOL,'1000');reverse['min_output_amount']='1'
        with patch.object(P,'_autorizar_entrada',return_value=self.config()),patch.object(P,'_tokens',return_value=0),patch.object(real,'_cli',side_effect=[self.cotizacion(real.SOL,'token','10000000'),reverse]) as cli,self.assertRaises(EntradaNoEnviada):M.abrir(e,'wallet',self.candidato(),1)
        self.assertEqual(cli.call_count,2);self.assertNotIn('intencion_entrada',e)

    def test_falta_id_conserva_intencion(self):
        e=self.estado()
        def cli(*args,**kw):return {} if args[0]=='swap' else self.cli(args)
        with patch.object(P,'cfg',return_value=self.config()),patch.object(P,'_saldo',return_value=(.1,'wallet')),patch.object(P,'_tokens',return_value=0),patch.object(real,'_cli',side_effect=cli),self.assertRaises(RuntimeError):M.abrir(e,'wallet',self.candidato(),1)
        self.assertIn('intencion_entrada',e)

    def test_denegacion_previa_no_deja_intencion_ambigua(self):
        e=self.estado()
        def cli(*args,**kw):
            if args[0]=='swap':raise E.Bloqueado('no autorizado')
            return self.cli(args)
        with patch.object(P,'cfg',return_value=self.config()),patch.object(P,'_saldo',return_value=(.1,'wallet')),patch.object(P,'_tokens',return_value=0),patch.object(real,'_cli',side_effect=cli),self.assertRaises(E.Bloqueado):M.abrir(e,'wallet',self.candidato(),1)
        self.assertNotIn('intencion_entrada',e)

    def test_confirmada_sin_estrategia_no_finge_proteccion(self):
        e={'orden':self.orden()}
        def cli(*a,**k):return self.respuesta() if a[:2]==('order','get') else {'list':[]}
        with patch.object(real,'_cli',side_effect=cli) as llamada,patch.object(P,'_seguir_estrategia') as follow:M.seguir(e,self.config())
        self.assertIsNotNone(e['orden']);self.assertTrue(e['orden']['entrada_confirmada']);self.assertFalse(e['orden'].get('proteccion_verificada',False));follow.assert_not_called()
        self.assertFalse(P.cfg()['activo']);self.assertFalse([x for x in llamada.call_args_list if x.args[0]=='swap'])

    # formato actual de GMGN (oct 2026): status + report, sin strategy_order_id y con open_sign_hash vacío
    def actual(self,status='confirmed',informe=True):
        x={'status':status,'hash':'hash-compra','order_id':'swap-id'}
        if informe:x['report']={'input_token':real.SOL,'input_amount':'10000000','output_token':'token','output_amount':'1000'}
        return x
    def seguir_actual(self,x,lista):
        e={'orden':self.orden()}
        def cli(*a,**k):
            if a[:2]==('order','get'):return x
            return {'list':lista if a[a.index('--type')+1]=='open' and a[a.index('--group-tag')+1]=='STMix' else []}
        with patch.object(real,'_cli',side_effect=cli) as llamada,patch.object(P,'_seguir_estrategia') as follow:M.seguir(e,self.config())
        self.assertFalse([c for c in llamada.call_args_list if c.args[0]=='swap'])
        return e,follow

    def test_formato_actual_adopta_la_unica_proteccion_con_la_cantidad_comprada(self):
        e,follow=self.seguir_actual(self.actual(),[{**self.proteccion(),'open_sign_hash':''}])
        o=e['orden'];self.assertEqual((o['id'],o['id_entrada'],o['hash_entrada'],o['cantidad_comprada_atomica']),('strategy-id','swap-id','hash-compra','1000'))
        self.assertTrue(o['proteccion_verificada']);follow.assert_called_once()

    def test_formato_actual_con_dos_candidatas_no_adopta_ninguna(self):
        otra={**self.proteccion(),'order_id':'otra','open_sign_hash':''}
        e,follow=self.seguir_actual(self.actual(),[{**self.proteccion(),'open_sign_hash':''},otra])
        self.assertFalse(e['orden'].get('proteccion_verificada',False));follow.assert_not_called();self.assertFalse(P.cfg()['activo'])

    def test_formato_actual_otra_cantidad_no_se_adopta(self):
        e,follow=self.seguir_actual(self.actual(),[{**self.proteccion(),'open_sign_hash':'','order_statistic':{'buy_amount':'999'}}])
        self.assertFalse(e['orden'].get('proteccion_verificada',False));follow.assert_not_called()

    def test_formato_actual_pendiente_sin_informe_espera(self):
        e,follow=self.seguir_actual(self.actual('pending',False),[])
        self.assertEqual(e['orden']['estado'],'pendiente_confirmacion_entrada');self.assertNotIn('incidencia',e['orden'])

    def test_formato_actual_confirmada_sin_informe_no_se_da_por_buena(self):
        e,follow=self.seguir_actual(self.actual('confirmed',False),[])
        self.assertFalse(e['orden'].get('entrada_confirmada',False));self.assertIn('informe',e['orden']['incidencia'])

    def test_formato_actual_fallida_sin_tokens_cierra_sin_pnl(self):
        with patch.object(P,'_tokens',return_value=0):e,_=self.seguir_actual(self.actual('failed',False),[])
        self.assertIsNone(e['orden']);self.assertEqual(e['ops'][0]['resultado'],'fallo_compra')

    def test_formato_actual_otro_token_se_bloquea(self):
        x=self.actual();x['report']['output_token']='otro'
        e,_=self.seguir_actual(x,[]);self.assertIn('fuera de ambito',e['orden']['incidencia'])

    def test_confirmada_y_protegida_asocia_identificadores_correctos(self):
        e={'orden':self.orden()}
        with patch.object(real,'_cli',return_value=self.respuesta(strategy_order_id='strategy-id')),patch.object(P,'_buscar',return_value=('open',self.proteccion())),patch.object(P,'_seguir_estrategia') as follow:M.seguir(e,self.config())
        self.assertEqual(e['orden']['id'],'strategy-id');self.assertEqual(e['orden']['id_entrada'],'swap-id');self.assertTrue(e['orden']['proteccion_verificada']);follow.assert_called_once()

    def test_proteccion_otra_compra_o_cantidad_no_se_adopta(self):
        for change in ({'open_sign_hash':'otro'},{'order_statistic':{'buy_amount':'999'}}):
            e={'orden':self.orden()};protection={**self.proteccion(),**change}
            with patch.object(real,'_cli',return_value=self.respuesta(strategy_order_id='strategy-id')),patch.object(P,'_buscar',return_value=('open',protection)),patch.object(P,'_seguir_estrategia') as follow:M.seguir(e,self.config())
            self.assertFalse(e['orden'].get('proteccion_verificada',False));follow.assert_not_called()

    def test_objetivo_no_sustituye_stop_ausente(self):
        e={'orden':self.orden()};p=self.proteccion();p['condition_orders'].pop()
        with patch.object(real,'_cli',return_value=self.respuesta(strategy_order_id='strategy-id')),patch.object(P,'_buscar',return_value=('open',p)):M.seguir(e,self.config())
        self.assertFalse(e['orden'].get('proteccion_verificada',False));self.assertIsNotNone(e['orden'])

    def test_processed_no_es_confirmed(self):
        e={'orden':self.orden()};x=self.respuesta();x['confirmation']['state']='processed'
        with patch.object(real,'_cli',return_value=x),patch.object(P,'_buscar') as find:M.seguir(e,self.config())
        self.assertFalse(e['orden'].get('entrada_confirmada',False));find.assert_not_called()

    def test_respuesta_otro_order_id_se_bloquea(self):
        e={'orden':self.orden()};x=self.respuesta();x['order_id']='otro'
        with patch.object(real,'_cli',return_value=x),patch.object(P,'_buscar') as find:M.seguir(e,self.config())
        self.assertFalse(e['orden'].get('entrada_confirmada',False));find.assert_not_called()

    def test_fallo_con_parcial_no_borra_posicion(self):
        e={'orden':self.orden()};x=self.respuesta();x['confirmation']['state']='failed'
        with patch.object(real,'_cli',return_value=x):M.seguir(e,self.config())
        self.assertIsNotNone(e['orden']);self.assertFalse(e.get('ops'))

    def test_fallo_sin_ejecucion_no_inventa_pnl_cero(self):
        e={'orden':self.orden()};x=self.respuesta();x.update(filled_input_amount='0',filled_output_amount='0');x['confirmation']['state']='failed'
        with patch.object(real,'_cli',return_value=x),patch.object(P,'_tokens',return_value=0):M.seguir(e,self.config())
        self.assertIsNone(e['orden']);self.assertIsNone(e['ops'][0]['pnl_sol'])

    def test_timeout_de_consulta_no_reenvia_swap(self):
        e={'orden':self.orden()}
        with patch.object(real,'_cli',side_effect=TimeoutError()) as cli:M.seguir(e,self.config());M.seguir(e,self.config())
        self.assertEqual(cli.call_count,2);self.assertTrue(all(x.args[:2]==('order','get') for x in cli.call_args_list))

    def test_consultas_no_requieren_propiedad(self):
        with patch.object(real,'_cli_directo',return_value={}) as direct,patch.object(E,'ejecutar') as gate:
            CLI_ORIGINAL('order','get','--chain','sol','--order-id','id');CLI_ORIGINAL('order','quote','--chain','sol')
        gate.assert_not_called();self.assertEqual(direct.call_count,2)

    def test_paso_fabrica_conserva_estado_durable_mercado(self):
        from trader import fabrica
        e=self.estado();b=self.candidato()
        def open_market(*a):e['orden']=self.orden();return 'swap-id',1,20,10
        with patch.object(P,'cfg',return_value=self.config()),patch.object(P,'_est',return_value=e),patch.object(P,'_saldo',return_value=(.1,'wallet')),patch.object(P,'_ult',{'smart':int(time.time()),'scan':0}),patch.object(P,'_ia_entrada',return_value=(True,'sin IA')),patch.object(fabrica,'promovidas',return_value=[{}]),patch.object(fabrica,'senales_ahora',return_value=[b]),patch.object(M,'abrir',side_effect=open_market),patch.object(P,'_crear_seguro') as old:P.paso()
        self.assertEqual(e['orden']['id_entrada'],'swap-id');self.assertEqual(e['orden']['modo'],'mercado');old.assert_not_called()

    def test_deposito_ajeno_no_se_presenta_como_ganancia(self):
        o=self.orden();o.pop('modo');o['id']='strategy-id';e={'orden':o}
        x=self.proteccion();x.update(status='closed',strategy_status='stopped');x['order_statistic'].update(success_sell_amount='1000')
        with patch.object(P,'_buscar',return_value=('history',x)),patch.object(P,'_saldo',return_value=(2,'wallet')),patch.object(P.conciencia,'operacion_real') as notify:P._seguir_estrategia(e,self.config())
        self.assertIsNone(e['orden']);self.assertIsNone(e['ops'][0]['pnl_sol']);self.assertGreater(e['ops'][0]['variacion_saldo_sol'],1);notify.assert_not_called()

    def test_saldo_cae_durante_preparacion_no_envia(self):
        e=self.estado();sent=[]
        def cli(*args,**kw):
            if args[0]=='swap':kw['preflight']();sent.append(1)
            return self.cli(args)
        with patch.object(P,'cfg',return_value=self.config()),patch.object(P,'_saldo',side_effect=[(.1,'wallet'),(.1,'wallet'),(.001,'wallet')]),patch.object(P,'_tokens',return_value=0),patch.object(real,'_cli',side_effect=cli),self.assertRaises(EntradaNoEnviada):M.abrir(e,'wallet',self.candidato(),1)
        self.assertFalse(sent);self.assertNotIn('intencion_entrada',e)

    def test_lectura_fallida_previa_no_es_timeout_de_envio(self):
        def gate(provider,wallet,callback):return callback()
        with patch.object(E,'ejecutar',side_effect=gate),patch.object(real,'_cli_directo') as direct,self.assertRaises(EntradaNoEnviada):CLI_ORIGINAL('swap','--chain','sol','--from','wallet',preflight=lambda:(_ for _ in ()).throw(TimeoutError()))
        direct.assert_not_called()

    def test_cotizacion_caducada_no_envia(self):
        e=self.estado();quote={'minimo':1000,'esperado':1100,'ts':0};reverse={'minimo':9800000,'esperado':9900000,'ts':0}
        with patch.object(P,'cfg',return_value=self.config()),patch.object(P,'_saldo',return_value=(.1,'wallet')),patch.object(P,'_tokens',return_value=0),patch.object(M,'cotizar',side_effect=[quote,reverse]),patch.object(real,'_cli') as cli,self.assertRaises(EntradaNoEnviada):M.abrir(e,'wallet',self.candidato(),1)
        cli.assert_not_called();self.assertNotIn('intencion_entrada',e)

    def test_perdida_posterior_de_stop_no_conserva_bandera_protegida(self):
        o=self.orden();o.update(id='strategy-id',entrada_confirmada=True,grupo_proteccion='STMix',proteccion_verificada=True,hash_entrada='hash-compra',cantidad_comprada_atomica='1000');e={'orden':o}
        protection=self.proteccion();protection['condition_orders'].pop()
        with patch.object(P,'_buscar',return_value=('open',protection)),patch.object(P,'_seguir_estrategia') as follow,patch.object(real,'_cli') as cli:M.seguir(e,self.config())
        self.assertFalse(o['proteccion_verificada']);follow.assert_not_called();cli.assert_not_called()

    def test_salida_en_curso_con_tp_sl_retirado_no_es_proteccion_perdida(self):
        o=self.orden();o.update(id='strategy-id',entrada_confirmada=True,grupo_proteccion='STMix',hash_entrada='hash-compra',cantidad_comprada_atomica='1000',salida_solicitada='APOLO lo ve mal');e={'orden':o}
        x={**self.proteccion(),'status':'canceled','strategy_status':'canceled'}
        with patch.object(P,'_buscar',return_value=('history',x)),patch.object(P,'_seguir_estrategia') as follow:M.seguir(e,self.config())
        follow.assert_called_once();self.assertNotIn('incidencia',o);self.assertFalse(o['proteccion_verificada'])
