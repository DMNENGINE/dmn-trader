import unittest,copy
from unittest.mock import patch,Mock
import test_guardia as base
from trader import piloto as P,real,ejecutor as E
from trader.presupuesto_gmgn import presupuesto,EntradaNoEnviada

class Presupuesto(unittest.TestCase):
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown

    def test_reserva_cero_no_permite_gastar_saldo_entero(self):
        cfg={**P.DEFECTO,'activo':True,'reserva_sol':0}
        with patch.object(P,'cfg',return_value=cfg),patch.object(P,'_saldo',return_value=(.01,'wallet')),patch.object(P,'_crear') as send,self.assertRaises(ValueError):
            P._crear_seguro({'saldo_inicio':.01},'wallet',{'token':'token','atr':5},1)
        send.assert_not_called()

    def test_reserva_existente_no_se_reduce(self):
        plan=presupuesto(P.DEFECTO)
        self.assertEqual(plan['necesario_lamports'],15000000)
        self.assertFalse(plan['cotizacion_completa'])

    def test_frontera_de_un_lamport(self):
        cfg={**P.DEFECTO,'activo':True,'reserva_sol':0}
        for balance,allowed in ((.010239999,False),(.01024,True)):
            with patch.object(P,'cfg',return_value=cfg),patch.object(P,'_saldo',return_value=(balance,'wallet')):
                if allowed:self.assertEqual(P._autorizar_entrada({'saldo_inicio':balance},'wallet'),cfg)
                else:
                    with self.assertRaises(ValueError):P._autorizar_entrada({'saldo_inicio':balance},'wallet')

    def test_saldo_consultado_cubre_plan_existente_sin_ordenar(self):
        cfg={**P.DEFECTO,'activo':True}
        with patch.object(P,'cfg',return_value=cfg),patch.object(P,'_saldo',return_value=(.021454192,'wallet')),patch.object(real,'_cli') as cli:
            P._autorizar_entrada({'saldo_inicio':.021454192},'wallet')
        cli.assert_not_called()

    def test_importes_invalidos_o_sub_lamport_no_se_redondean(self):
        for value in (True,-1,0,float('nan'),float('inf'),.100000001,.0050000001):
            with self.subTest(value=value),self.assertRaises(EntradaNoEnviada):presupuesto({**P.DEFECTO,'por_op_sol':value})

    def test_cambio_durante_balance_bloquea(self):
        cfg={**P.DEFECTO,'activo':True}
        with patch.object(P,'cfg',side_effect=[cfg,{**cfg,'activo':False}]),patch.object(P,'_saldo',return_value=(.1,'wallet')),self.assertRaises(EntradaNoEnviada):P._autorizar_entrada({'saldo_inicio':.1},'wallet')

    def test_buy_low_por_encima_no_envia_ni_deja_intencion(self):
        cfg={**P.DEFECTO,'activo':True};state={'saldo_inicio':.1}
        with patch.object(P,'cfg',return_value=cfg),patch.object(P,'_saldo',return_value=(.1,'wallet')),patch.object(real,'_cli') as cli:
            with self.assertRaises(EntradaNoEnviada):P._crear_seguro(state,'wallet',{'token':'token','atr':5},1,compra=1.01)
        cli.assert_not_called();self.assertNotIn('intencion_entrada',state)

    def test_precio_que_redondea_a_igual_no_envia(self):
        cfg={**P.DEFECTO,'activo':True}
        with patch.object(P,'cfg',return_value=cfg),patch.object(real,'_cli') as cli,self.assertRaises(EntradaNoEnviada):P._crear('wallet',{'token':'token','atr':5},1,compra=.99999999999,config=cfg)
        cli.assert_not_called()

    def test_importe_en_registro_no_cambia_tras_respuesta(self):
        cfg={**P.DEFECTO,'activo':True};state={'saldo_inicio':.1};candidate={'token':'token','atr':5}
        def send(*args,**kwargs):
            cfg['por_op_sol']=.02
            return 'id',.9,20,10
        # La configuracion autorizada es una copia: una actualizacion posterior no debe alterar el registro.
        authorized=copy.deepcopy(cfg)
        with patch.object(P,'_autorizar_entrada',return_value=authorized),patch.object(P,'_crear',side_effect=send):P._crear_seguro(state,'wallet',candidate,1)
        self.assertEqual(state['orden']['sol'],.01)

    def test_preflight_se_ejecuta_despues_de_autoridad_antes_del_cli(self):
        order=[]
        def gate(provider,wallet,callback):order.append('autoridad');return callback()
        def preflight():order.append('config');raise EntradaNoEnviada('apagado durante consulta de autoridad')
        with patch.object(E,'ejecutar',side_effect=gate),patch.object(real,'_cli_directo') as direct:
            with self.assertRaises(EntradaNoEnviada):
                ORIGINAL_CLI('order','strategy','create','--chain','sol','--from','wallet',preflight=preflight)
        self.assertEqual(order,['autoridad','config']);direct.assert_not_called()

    def test_apagar_durante_autoridad_no_envia_y_retira_solo_intencion_nueva(self):
        current={**P.DEFECTO,'activo':True};state={'saldo_inicio':.1}
        def gate(provider,wallet,callback):
            current['activo']=False
            return callback()
        with patch.object(P,'cfg',side_effect=lambda:dict(current)),patch.object(P,'_saldo',return_value=(.1,'wallet')),patch.object(real,'_cli',ORIGINAL_CLI),patch.object(E,'ejecutar',side_effect=gate),patch.object(real,'_cli_directo') as direct:
            with self.assertRaises(EntradaNoEnviada):P._crear_seguro(state,'wallet',{'token':'token','atr':5},1)
        direct.assert_not_called();self.assertNotIn('intencion_entrada',state)

ORIGINAL_CLI=real._cli
