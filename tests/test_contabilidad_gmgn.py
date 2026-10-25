import unittest,copy,json
from unittest.mock import patch
import test_guardia as base
from trader import db,contabilidad_gmgn as C,supervision_gmgn as S

SIG='3'*88

def receipt(side='entrada',failed=False):
    before,after=('0','100') if side=='entrada' else ('100','0')
    native=(100000000,87995000) if side=='entrada' else (87995000,98990000)
    return {'slot':100,'transaction':{'signatures':[SIG],'message':{'accountKeys':[{'pubkey':'wallet'},{'pubkey':'ata'}]}},'meta':{'err':{'failed':True} if failed else None,'fee':5000,'preBalances':[native[0],0 if side=='entrada' else 2000000],'postBalances':[native[1],2000000],'preTokenBalances':[{'accountIndex':1,'mint':'token','owner':'wallet','uiTokenAmount':{'amount':before}}],'postTokenBalances':[{'accountIndex':1,'mint':'token','owner':'wallet','uiTokenAmount':{'amount':after}}]}}

class Contabilidad(unittest.TestCase):
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown
    def op(self):return dict(wallet='wallet',id_entrada='op',id='strategy',token='token',cantidad_comprada_atomica='100')
    def test_movimiento_y_comision_no_se_suman_dos_veces(self):
        p=C.interpretar(receipt(),SIG,'wallet','token','entrada')
        self.assertEqual(p['delta_lamports'],'-12005000');self.assertEqual(p['comision_red_lamports'],'5000');self.assertEqual(p['renta_cuentas_lamports'],'2000000')
    def test_idempotencia_del_recibo(self):
        o=self.op();C.anotar(o,SIG,'entrada');p=C.interpretar(receipt(),SIG,'wallet','token','entrada')
        self.assertTrue(C.guardar(SIG,'wallet','op','entrada',p));self.assertTrue(C.guardar(SIG,'wallet','op','entrada',p))
        self.assertEqual(C.resumen(o)['recibos_verificados'],1)
    def test_recibo_cambiado_bloquea_sin_reescribir_importe(self):
        o=self.op();C.anotar(o,SIG,'entrada');p=C.interpretar(receipt(),SIG,'wallet','token','entrada');C.guardar(SIG,'wallet','op','entrada',p)
        self.assertFalse(C.guardar(SIG,'wallet','op','entrada',{**p,'delta_lamports':'1'}))
        self.assertEqual(C.resumen(o)['flujo_sol_identificado'],-.012005)
        with self.assertRaises(ValueError):S.admitir('wallet')
    def test_wallet_ajena_no_se_atribuye(self):
        with self.assertRaises(ValueError):C.interpretar(receipt(),SIG,'otra','token','entrada')
    def test_mint_ajeno_no_se_atribuye(self):
        with self.assertRaises(ValueError):C.interpretar(receipt(),SIG,'wallet','otro','entrada')
    def test_firma_ajena_no_se_atribuye(self):
        with self.assertRaises(ValueError):C.interpretar(receipt(),'4'*88,'wallet','token','entrada')
    def test_propietario_ausente_queda_pendiente(self):
        x=receipt();x['meta']['postTokenBalances'][0].pop('owner')
        with self.assertRaises(ValueError):C.interpretar(x,SIG,'wallet','token','entrada')
    def test_indice_duplicado_no_duplica_cantidad(self):
        x=receipt();x['meta']['postTokenBalances']*=2
        with self.assertRaises(ValueError):C.interpretar(x,SIG,'wallet','token','entrada')
    def test_cuentas_cargadas_son_conciliadas(self):
        x=receipt();x['transaction']['message']['accountKeys'].pop();x['meta']['loadedAddresses']={'writable':['ata'],'readonly':[]}
        self.assertEqual(C.interpretar(x,SIG,'wallet','token','entrada')['delta_token_atomico'],'100')
    def test_rpc_incompleto_no_certifica(self):
        for key in ('fee','err','postBalances'):
            x=receipt();x['meta'].pop(key)
            with self.assertRaises(ValueError):C.interpretar(x,SIG,'wallet','token','entrada')
    def test_fallo_con_tokens_movidos_no_certifica(self):
        with self.assertRaises(ValueError):C.interpretar(receipt(failed=True),SIG,'wallet','token','fallo')
    def test_fallo_paga_red_pero_no_es_cero_perdida(self):
        x=receipt(failed=True);x['meta']['postTokenBalances'][0]['uiTokenAmount']['amount']='0';x['meta']['postBalances']=[99995000,0]
        parsed=C.interpretar(x,SIG,'wallet','token','fallo');self.assertTrue(parsed['fallida']);self.assertEqual(parsed['delta_lamports'],'-5000')
    def test_rpc_ausente_reintenta_con_espera_sin_transaccion(self):
        o=self.op();C.anotar(o,SIG,'entrada')
        with patch.object(C,'consultar',side_effect=TimeoutError()) as rpc:C.procesar();C.procesar()
        self.assertEqual(rpc.call_count,1);self.assertEqual(C.resumen(o)['recibos_pendientes'],1)
    def test_cantidad_rpc_distinta_de_broker_queda_en_conflicto(self):
        o=self.op();o['cantidad_comprada_atomica']='101';C.anotar(o,SIG,'entrada')
        with patch.object(C,'consultar',return_value=receipt()):C.procesar()
        self.assertEqual(db.uno('SELECT estado FROM gmgn_recibos')['estado'],'conflicto')
        with self.assertRaises(ValueError):S.admitir('wallet')
    def test_caja_identificada_no_se_publica_como_neto_completo(self):
        o=self.op();C.anotar(o,SIG,'entrada');p=C.interpretar(receipt(),SIG,'wallet','token','entrada');C.guardar(SIG,'wallet','op','entrada',p)
        out=C.resumen(o);self.assertIsNone(out['pnl_sol']);self.assertFalse(out['costes_completos_confirmados'])
    def test_libro_sobrevive_al_limite_visual_de_operaciones(self):
        for i in range(205):C.operacion({**self.op(),'id_entrada':str(i)})
        self.assertEqual(db.uno('SELECT COUNT(*) n FROM gmgn_libro')['n'],205)
    def test_otra_operacion_no_reutiliza_firma(self):
        C.anotar(self.op(),SIG,'entrada');C.anotar({**self.op(),'id_entrada':'otra'},SIG,'entrada')
        self.assertEqual(db.uno('SELECT operacion FROM gmgn_recibos')['operacion'],'op')
        with self.assertRaises(ValueError):S.admitir('wallet')
    def test_firma_invalida_no_sale_a_rpc(self):
        C.anotar(self.op(),'NO-ES-UNA-FIRMA','entrada')
        with patch.object(C,'consultar') as rpc:C.procesar()
        rpc.assert_not_called()

    def test_cantidad_de_firma_registrada_no_cambia(self):
        o=self.op();C.anotar(o,SIG,'entrada');C.anotar({**o,'cantidad_comprada_atomica':'101'},SIG,'entrada')
        with self.assertRaises(ValueError):S.admitir('wallet')
        self.assertEqual(json.loads(db.uno('SELECT datos FROM gmgn_recibos')['datos'])['esperado_atomico'],'100')

    def test_rpc_solo_solicita_recibos_finalizados(self):
        requests=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return json.dumps({'result':receipt()}).encode()
        def open(req,**kw):requests.append(json.loads(req.data));return Response()
        with patch.object(C.urllib.request,'urlopen',side_effect=open):C.consultar(SIG)
        self.assertEqual(requests[0]['method'],'getTransaction')
        self.assertEqual(requests[0]['params'][1]['commitment'],'finalized')

    def test_pnl_no_se_infiere_de_dos_transacciones_fallidas(self):
        o=self.op();C.operacion(o)
        for signature,role in ((SIG,'entrada'),('4'*88,'salida')):
            value={'firma':signature,'wallet':'wallet','token':'token','rol':role,'slot':1,'fallida':True,'delta_lamports':'-5000','delta_token_atomico':'0','comision_red_lamports':'5000','renta_cuentas_lamports':'0','costes_externos_verificados':False}
            C.guardar(signature,'wallet','op',role,value)
        out=C.resumen(o);self.assertFalse(out['posicion_con_recibos_cerrada']);self.assertIsNone(out['pnl_sol']);self.assertEqual(out['flujo_sol_identificado'],-.00001)

    def test_firma_secundaria_no_duplica_mismo_movimiento(self):
        x=receipt();x['transaction']['signatures']=['4'*88,SIG]
        with self.assertRaises(ValueError):C.interpretar(x,SIG,'wallet','token','entrada')

    def test_recibo_de_otra_wallet_no_se_guarda_en_libro(self):
        o=self.op();C.operacion(o);value=C.interpretar(receipt(),SIG,'wallet','token','entrada')
        with self.assertRaises(ValueError):C.guardar(SIG,'otra','op','entrada',value)

    def test_releer_rpc_sin_metadato_interno_no_crea_falso_conflicto(self):
        C.anotar(self.op(),SIG,'entrada')
        with patch.object(C,'consultar',return_value=receipt()):C.procesar()
        value=C.interpretar(receipt(),SIG,'wallet','token','entrada')
        self.assertTrue(C.guardar(SIG,'wallet','op','entrada',value));S.admitir('wallet')
