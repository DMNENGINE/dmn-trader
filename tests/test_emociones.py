import json,time,unittest
from unittest.mock import patch,MagicMock
import test_guardia as base
from trader import agente as A,db,emociones as E


class Emociones(unittest.TestCase):
    """Las emociones de los agentes: qué las provoca, cómo cambian su forma de operar y cómo le llegan a APOLO."""
    setUp=base.Guardia.setUp
    tearDown=base.Guardia.tearDown
    def hechos(self,**kw):
        return {'rs':[],'equity':1.0,'capital':1.0,'max_equity':1.0,'sin_operar_s':0,'descartes':0,**kw}
    def agentes(self,n=6):
        A.iniciar(n,1,100,['cripto'],duracion_min=1,perdida_max_pct=10)
        return A.agentes()
    def trades(self,a,rs,desde=None):
        t0=desde or int(time.time())
        for i,r in enumerate(rs):
            tid=db.ex("INSERT INTO trades(cuenta_id,simbolo,direccion,estado,abierto,cerrado,cantidad,entrada,pnl) VALUES(?,?,?,?,?,?,1,1,0)",(a['cuenta_id'],'BTC-USD','largo','cerrado',t0+i,t0+i))
            db.ex('INSERT INTO ag_trades(trade_id,agente_id,simbolo,genoma,rasgos,abierto,r,revisado) VALUES(?,?,?,?,?,?,?,1)',(tid,a['id'],'BTC-USD',a['genoma'],'{}',t0+i,r))

    def test_lo_que_pasa_provoca_cada_emocion(self):
        self.assertGreater(E.objetivo(self.hechos(rs=[-1,-1,-1,-1],equity=0.5,max_equity=1.2))['miedo'],0.8)
        self.assertGreater(E.objetivo(self.hechos(rs=[1,1.2,0.8,1.5]))['confianza'],0.8)
        self.assertGreater(E.objetivo(self.hechos(quiebra_reciente=True))['frustracion'],0.9)
        self.assertEqual(E.objetivo(self.hechos(meta_reciente=True))['orgullo'],1.0)
        self.assertGreater(E.objetivo(self.hechos(sin_operar_s=3*3600))['hambre'],0.75)
        self.assertGreater(E.objetivo(self.hechos(equity=6.0,rs=[2.5]))['euforia'],0.9)
        self.assertEqual(E.dominante(E.mezclar({},E.objetivo(self.hechos())))[0],'calma')

    def test_las_emociones_cambian_como_opera_sin_pasarse(self):
        self.assertLess(E.modular({'valores':{'miedo':1}})['riesgo'],0.6)
        self.assertEqual(E.modular({'valores':{'confianza':1}})['riesgo'],1.25)
        self.assertLess(E.modular({'valores':{'confianza':1,'euforia':1}})['riesgo'],1.0)
        self.assertEqual(E.modular({'valores':{'miedo':1,'euforia':1}})['riesgo'],0.4)
        self.assertEqual(E.modular({'valores':{'hambre':1}})['paciencia'],0.5)
        self.assertEqual(E.modular(None),{'riesgo':1.0,'paciencia':1.0,'pausa_hasta':0})

    def test_inercia_no_cambia_de_golpe(self):
        v=E.mezclar({'valores':{'miedo':0.0}},{**{k:0.0 for k in E.EMOCIONES},'miedo':1.0})
        self.assertAlmostEqual(v['miedo'],0.4)

    def test_actualizar_guarda_el_animo_y_escribe_el_diario_cuando_cambia(self):
        a=self.agentes(2)[0];self.trades(a,[-1,-1,-1,-1,-1])
        a=db.uno('SELECT * FROM ag_agentes WHERE id=?',(a['id'],))
        for _ in range(4):an=E.actualizar(a,A.estado(),A._intento_actual(a['id']),0.3,0,forzar=True);a=db.uno('SELECT * FROM ag_agentes WHERE id=?',(a['id'],))
        self.assertEqual(an['emocion'],'miedo');self.assertEqual(json.loads(a['animo'])['emoji'],'😰')
        d=E.diario(a['id']);self.assertEqual(d[0]['emocion'],'miedo');self.assertIn('Bajo el tamaño',d[0]['texto'])
        self.assertEqual(len(E.diario(a['id'])),len(d))  # sin cambio de emoción no se repite la entrada
        self.assertEqual(E.actualizar(a,A.estado(),None,0.3,0)['emocion'],'miedo')  # dentro del minuto devuelve lo guardado

    def test_frustracion_alta_para_al_agente_media_hora(self):
        a=self.agentes(2)[0]
        db.ex("UPDATE ag_intentos SET resultado='quiebra', fin=? WHERE agente_id=?",(int(time.time()),a['id']))
        for _ in range(4):an=E.actualizar(db.uno('SELECT * FROM ag_agentes WHERE id=?',(a['id'],)),A.estado(),None,0.05,0,forzar=True)
        self.assertEqual(an['emocion'],'frustracion');self.assertGreater(E.modular(an)['pausa_hasta'],time.time()+25*60)

    def test_el_animo_del_enjambre_y_lo_que_le_llega_a_apolo(self):
        ags=self.agentes(3)
        for a,emo in zip(ags,('miedo','miedo','confianza')):
            db.ex('UPDATE ag_agentes SET animo=? WHERE id=?',(json.dumps({'emocion':emo,'valores':{**{k:0 for k in E.EMOCIONES},emo:0.9}}),a['id']))
        col=E.colectivo(A.agentes());self.assertEqual((col['emocion'],col['cuenta']['miedo']),('miedo',2));self.assertIn('😰',col['texto'])
        c=E.cambios_apolo(col);self.assertTrue(all(-0.05<=v<=0.05 for v in c.values()));self.assertLess(c['animo'],0);self.assertGreater(c['preocupacion'],0)
        enviado={}
        class Ya:
            def __init__(s,target,daemon):s.t=target
            def start(s):s.t()
        def abrir(rq,timeout):enviado.update(url=rq.full_url,cuerpo=json.loads(rq.data));return MagicMock()
        with patch.object(E.threading,'Thread',Ya),patch('builtins.open',MagicMock(return_value=MagicMock(read=lambda:'tok'))),patch.object(E.urllib.request,'urlopen',side_effect=abrir):
            self.assertTrue(E.sentir_apolo(col,'http://apolo','token'))
        self.assertEqual(enviado['url'],'http://apolo/v1/conciencia/sentir');self.assertIn('mi equipo de traders',enviado['cuerpo']['motivo'])
        self.assertFalse(E.sentir_apolo(None,'http://apolo','token'))

    def test_constancia_premia_ganar_seguido(self):
        self.assertEqual(A.constancia([1,1,1,1,1,1]),1.0);self.assertEqual(A.constancia([-1]*6),0.0);self.assertLess(A.constancia([5]+[-1]*7),0.5)
        a,b=self.agentes(2)
        self.trades(a,[0.5]*10);self.trades(b,[4.5,-0.5,-0.5,-0.5,-0.5,-0.5,-0.5,-0.5,-0.5,-0.5])
        fa,fb=A.aptitud(db.uno('SELECT * FROM ag_agentes WHERE id=?',(a['id'],)))[0],A.aptitud(db.uno('SELECT * FROM ag_agentes WHERE id=?',(b['id'],)))[0]
        self.assertGreater(fa,fb)

    def test_evolucion_da_medallas_inmunidad_y_asciende_al_constante(self):
        ags=self.agentes(10)
        for i,a in enumerate(ags):self.trades(a,[0.6]*35 if i==0 else [0.3]*6 if i<3 else [-0.5]*6)
        with patch.object(A.motor,'avisar'):A.evolucionar(A.estado())
        a0=db.uno('SELECT * FROM ag_agentes WHERE id=?',(ags[0]['id'],))
        self.assertEqual(json.loads(a0['medallas'])['oro'],1);self.assertEqual(a0['ascendido'],1);self.assertGreater(a0['inmune_gen'],0)
        self.assertEqual(A.ascendidos()[-1]['agente'],a0['nombre']);self.assertIn('pendiente de revisión',A.ascendidos()[-1]['estado'])
        self.assertTrue(any(l['tipo']=='ascenso' for l in A.lecciones(20)))
        self.assertIn('Podio',[l for l in A.lecciones(20) if l['tipo']=='evolucion'][0]['texto'])
        nombres_antes={a['id']:a['nombre'] for a in A.agentes()}
        with patch.object(A.motor,'avisar'):A.evolucionar(A.estado())
        self.assertEqual(db.uno('SELECT nombre FROM ag_agentes WHERE id=?',(ags[0]['id'],))['nombre'],nombres_antes[ags[0]['id']])  # inmune: no se reemplaza

    def test_ranking_y_vista_llevan_emociones_y_medallas(self):
        ags=self.agentes(2);self.trades(ags[0],[0.4]*6)
        db.ex('UPDATE ag_agentes SET animo=? WHERE id=?',(json.dumps({'emocion':'confianza','emoji':'😎','frase':'va bien','valores':{}}),ags[0]['id']))
        r=A.ranking();self.assertEqual(r['ranking'][0]['nombre'],ags[0]['nombre']);self.assertEqual(r['ranking'][0]['animo']['emoji'],'😎')
        v={x['id']:x for x in A._vista_agentes()};self.assertEqual(v[ags[0]['id']]['animo']['emocion'],'confianza');self.assertIn('oro',v[ags[0]['id']]['medallas'])
