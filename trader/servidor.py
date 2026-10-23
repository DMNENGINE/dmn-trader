"""Servidor HTTP de DMN Trader: API JSON (/api/...) + panel web (web/). Un hilo de fondo ejecuta órdenes y alertas.
Arranque: python -m trader.servidor  [--host 0.0.0.0] [--puerto 8777]
Si se expone fuera de este PC, poner DMN_TRADER_TOKEN: se pedirá en la cabecera x-token (o ?token=)."""
import argparse, json, mimetypes, os, re, threading, time, traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from . import datos, db, motor, analisis, backtest, agente, real

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'web')
VERSION = '1.0.0'
TOKEN = os.environ.get('DMN_TRADER_TOKEN', '')
VIGILANCIA_INICIAL = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMZN', 'BTC-USD', 'ETH-USD', 'EURUSD=X', 'GC=F', '^GSPC']

RUTAS = []


def ruta(metodo, patron):
    def deco(fn):
        RUTAS.append((metodo, re.compile('^' + patron + '$'), fn))
        return fn
    return deco


def _f(x):
    return None if x in (None, '') else float(x)

# ---------- mercado ----------


@ruta('GET', '/api/salud')
def salud(q, b):
    return {'ok': True, 'version': VERSION, 'cuentas': len(db.q('SELECT id FROM cuentas')), 'hora': int(time.time())}


@ruta('GET', '/api/cotizacion')
def r_cotizacion(q, b):
    return [datos.cotizacion(s) for s in q['s'].split(',')] if ',' in q['s'] else datos.cotizacion(q['s'])


@ruta('GET', '/api/velas')
def r_velas(q, b):
    return datos.velas(q['s'], q.get('i', '1d'), q.get('r'))


@ruta('GET', '/api/buscar')
def r_buscar(q, b):
    return datos.buscar(q['q'])


@ruta('GET', '/api/analisis')
def r_analisis(q, b):
    return analisis.analizar(q['s'], q.get('i', '1d'))


@ruta('GET', '/api/escaner')
def r_escaner(q, b):
    sims = q['s'].split(',') if q.get('s') else [x['simbolo'] for x in db.q('SELECT simbolo FROM vigilancia ORDER BY anadido')]
    return analisis.escanear(sims, q.get('i', '1d'))


@ruta('GET', '/api/vigilancia')
def r_vig(q, b):
    sims = [x['simbolo'] for x in db.q('SELECT simbolo FROM vigilancia ORDER BY anadido')]
    if q.get('precios') == '0':
        return sims
    out = []
    for s in sims:
        try:
            c = datos.cotizacion(s)
            out.append({k: c[k] for k in ('simbolo', 'nombre', 'precio', 'cambio_pct', 'moneda', 'abierto', 'tipo')})
        except Exception as e:
            out.append({'simbolo': s, 'error': str(e)})
    return out


@ruta('POST', '/api/vigilancia')
def r_vig_add(q, b):
    for s in [x.strip().upper() for x in str(b['simbolo']).split(',') if x.strip()]:
        datos.cotizacion(s)  # valida que existe
        db.ex('INSERT OR IGNORE INTO vigilancia(simbolo,nota,anadido) VALUES(?,?,?)', (s, b.get('nota'), int(time.time() * 1000)))
    return r_vig({'precios': '0'}, None)


@ruta('DELETE', r'/api/vigilancia/(?P<s>[^/]+)')
def r_vig_del(q, b, s):
    db.ex('DELETE FROM vigilancia WHERE simbolo=?', (s.upper(),))
    return r_vig({'precios': '0'}, None)

# ---------- cuentas ----------


@ruta('GET', '/api/cuentas')
def r_cuentas(q, b):
    return motor.lista_cuentas()


@ruta('POST', '/api/cuentas')
def r_cuenta_nueva(q, b):
    return motor.crear_cuenta(b.get('nombre') or 'Cuenta', b.get('capital', 10000), b.get('moneda', 'USD'), b.get('config'),
                              'replay' if b.get('replay') else 'simulada', b.get('replay'))


@ruta('GET', r'/api/cuentas/(?P<cid>\d+)')
def r_cuenta(q, b, cid):
    return motor.cuenta(int(cid))


@ruta('PATCH', r'/api/cuentas/(?P<cid>\d+)')
def r_cuenta_cfg(q, b, cid):
    return motor.configurar(int(cid), b)


@ruta('DELETE', r'/api/cuentas/(?P<cid>\d+)')
def r_cuenta_del(q, b, cid):
    motor.borrar(int(cid))
    return {'ok': True}


@ruta('POST', r'/api/cuentas/(?P<cid>\d+)/reiniciar')
def r_reiniciar(q, b, cid):
    return motor.reiniciar(int(cid))


@ruta('GET', r'/api/cuentas/(?P<cid>\d+)/ordenes')
def r_ordenes(q, b, cid):
    return motor.ordenes(int(cid), q.get('estado'))


@ruta('POST', r'/api/cuentas/(?P<cid>\d+)/ordenes')
def r_orden(q, b, cid):
    return motor.enviar(int(cid), b['simbolo'], b['lado'], _f(b.get('cantidad')), b.get('tipo', 'mercado'), _f(b.get('limite')),
                        _f(b.get('stop')), _f(b.get('trail_pct')), _f(b.get('sl')), _f(b.get('tp')), _f(b.get('importe')),
                        b.get('nota'), b.get('setup'), b.get('origen', 'manual'))


@ruta('DELETE', r'/api/cuentas/(?P<cid>\d+)/ordenes/(?P<oid>\d+)')
def r_cancelar(q, b, cid, oid):
    return motor.cancelar(int(cid), int(oid))


@ruta('POST', r'/api/cuentas/(?P<cid>\d+)/cerrar')
def r_cerrar(q, b, cid):
    if b.get('simbolo') == '*':
        return [motor.cerrar(int(cid), p['simbolo'], 100, b.get('nota')) for p in motor.posiciones(int(cid), False)]
    return motor.cerrar(int(cid), b['simbolo'], b.get('porcentaje', 100), b.get('nota'))


@ruta('POST', r'/api/cuentas/(?P<cid>\d+)/stop')
def r_stop(q, b, cid):
    return motor.mover_stop(int(cid), b['simbolo'], _f(b.get('stop')), _f(b.get('objetivo')) if 'objetivo' in b else None, _f(b.get('trail_pct')))


@ruta('GET', r'/api/cuentas/(?P<cid>\d+)/tamano')
def r_tamano(q, b, cid):
    return motor.tamano_por_riesgo(int(cid), q['s'], _f(q.get('entrada')), float(q['stop']), _f(q.get('riesgo')))


@ruta('GET', r'/api/cuentas/(?P<cid>\d+)/diario')
def r_diario(q, b, cid):
    return motor.diario(int(cid), q.get('estado'))


@ruta('PATCH', r'/api/cuentas/(?P<cid>\d+)/diario/(?P<tid>\d+)')
def r_anotar(q, b, cid, tid):
    return motor.anotar(int(cid), int(tid), b)


@ruta('GET', r'/api/cuentas/(?P<cid>\d+)/estadisticas')
def r_stats(q, b, cid):
    return motor.estadisticas(int(cid))


@ruta('GET', r'/api/cuentas/(?P<cid>\d+)/equity')
def r_equity(q, b, cid):
    return motor.curva_equity(int(cid))


@ruta('GET', r'/api/cuentas/(?P<cid>\d+)/examen')
def r_examen(q, b, cid):
    return motor.examen(int(cid))


@ruta('GET', r'/api/cuentas/(?P<cid>\d+)/ejecuciones')
def r_ejec(q, b, cid):
    return db.q('SELECT * FROM ejecuciones WHERE cuenta_id=? ORDER BY id DESC LIMIT 300', (int(cid),))


@ruta('GET', r'/api/cuentas/(?P<cid>\d+)/replay')
def r_replay(q, b, cid):
    return motor.replay_velas(int(cid))


@ruta('POST', r'/api/cuentas/(?P<cid>\d+)/replay/avanzar')
def r_replay_av(q, b, cid):
    return motor.replay_avanzar(int(cid), (b or {}).get('pasos', 1))

# ---------- backtest ----------


@ruta('GET', '/api/estrategias')
def r_estr(q, b):
    return {k: {'params': d, 'descripcion': t} for k, (_, d, t) in backtest.ESTRATEGIAS.items()}


def _kw(b):
    return {k: b[k] for k in ('capital', 'comision_pct', 'comision_fija', 'deslizamiento_pct', 'cortos', 'tamano', 'fraccion',
                              'riesgo_pct', 'stop_atr', 'objetivo_atr') if k in b}


@ruta('POST', '/api/backtest')
def r_backtest(q, b):
    v = datos.velas(b['simbolo'], b.get('intervalo', '1d'), b.get('rango'))
    out = backtest.ejecutar(v, b.get('estrategia', 'cruce_medias'), b.get('params'), intervalo=b.get('intervalo', '1d'), **_kw(b))
    if b.get('comparar', True) and b.get('estrategia') != 'comprar_mantener':
        bh = backtest.ejecutar(v, 'comprar_mantener', intervalo=b.get('intervalo', '1d'), **_kw(b))
        out['referencia'] = {k: bh[k] for k in ('retorno_pct', 'cagr_pct', 'max_drawdown_pct', 'sharpe')} | {'curva': bh['curva']}
    out['simbolo'] = b['simbolo'].upper()
    return out


@ruta('POST', '/api/optimizar')
def r_optimizar(q, b):
    v = datos.velas(b['simbolo'], b.get('intervalo', '1d'), b.get('rango'))
    return backtest.optimizar(v, b['estrategia'], b['rangos'], b.get('criterio', 'sharpe'), b.get('entreno_pct', 70),
                              intervalo=b.get('intervalo', '1d'), **_kw(b))

# ---------- alertas y ajustes ----------


@ruta('GET', '/api/alertas')
def r_alertas(q, b):
    return db.q("SELECT * FROM alertas WHERE condicion<>'aviso' ORDER BY id DESC LIMIT 200")


@ruta('POST', '/api/alertas')
def r_alerta(q, b):
    if b['condicion'] not in ('mayor', 'menor', 'cambio_pct'):
        raise motor.ErrorOrden('condición: mayor, menor o cambio_pct')
    datos.cotizacion(b['simbolo'])
    aid = db.ex('INSERT INTO alertas(simbolo,condicion,valor,nota,activa,creada) VALUES(?,?,?,?,1,?)',
                (b['simbolo'].upper(), b['condicion'], float(b['valor']), b.get('nota'), int(time.time())))
    return db.uno('SELECT * FROM alertas WHERE id=?', (aid,))


@ruta('DELETE', r'/api/alertas/(?P<aid>\d+)')
def r_alerta_del(q, b, aid):
    db.ex('DELETE FROM alertas WHERE id=?', (int(aid),))
    return {'ok': True}


@ruta('GET', '/api/avisos')
def r_avisos(q, b):
    filas = db.q('SELECT * FROM alertas WHERE disparada IS NOT NULL' + (' AND vista=0' if q.get('nuevos') else '') + ' ORDER BY disparada DESC LIMIT 50')
    if q.get('marcar') != '0' and filas:
        db.ex('UPDATE alertas SET vista=1 WHERE disparada IS NOT NULL AND vista=0')
    return filas


@ruta('GET', '/api/ajustes')
def r_ajustes(q, b):
    return {'webhook': bool(db.ajuste('webhook', '')), 'cuenta_defecto': db.ajuste('cuenta_defecto')}


@ruta('POST', '/api/ajustes')
def r_ajustes_set(q, b):
    for k in ('webhook', 'cuenta_defecto'):
        if k in b:
            db.poner_ajuste(k, b[k])
    return r_ajustes(q, b)

# ---------- enjambre de agentes autónomos (el reto) ----------


def _hasta(hasta):
    if isinstance(hasta, str) and ':' in hasta:  # "07:00" → la próxima vez que sean esas horas (hora local del servidor)
        hh, mm = map(int, hasta.split(':'))
        t = time.localtime()
        fin = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hh, mm, 0, 0, 0, -1))
        return int(fin + 86400 if fin <= time.time() else fin)
    if hasta and float(hasta) < 1000:  # horas desde ahora
        return int(time.time() + float(hasta) * 3600)
    return int(hasta) if hasta else None


@ruta('GET', '/api/enjambre')
def r_enj(q, b):
    return agente.resumen()


@ruta('GET', '/api/enjambre/mapa')
def r_enj_mapa(q, b):
    return agente.mapa()


@ruta('POST', '/api/enjambre/iniciar')
def r_enj_ini(q, b):
    return agente.iniciar(int(b.get('agentes', 50)), float(b.get('capital', 1)), float(b.get('meta', 100)),
                          b.get('mercados') or ['cripto', 'acciones'], _hasta(b.get('hasta')))


@ruta('POST', '/api/enjambre/parar')
def r_enj_parar(q, b):
    return agente.parar()


@ruta('GET', r'/api/enjambre/agente/(?P<aid>\d+)')
def r_enj_ag(q, b, aid):
    return agente.agente(int(aid))


@ruta('GET', '/api/enjambre/lecciones')
def r_enj_lec(q, b):
    return agente.lecciones(int(q.get('n', 50)))


@ruta('GET', '/api/enjambre/noticias')
def r_enj_not(q, b):
    return agente.noticias(int(q.get('n') or 30))


@ruta('GET', '/api/enjambre/informe')
def r_enj_inf(q, b):
    return {'texto': agente.informe(q.get('desde'))}


@ruta('POST', '/api/enjambre/nodo')
def r_enj_nodo(q, b):
    return agente.recibir_nodo(b)


# ---------- cuenta real (GMGN, solo lectura) ----------


@ruta('GET', '/api/real')
def r_real(q, b):
    return real.estado()


@ruta('POST', '/api/real/nodo')
def r_real_nodo(q, b):
    return real.recibir(b)


# ---------- hilo de fondo ----------


def bucle():
    ultima_foto = {}
    while True:
        try:
            for c in db.q("SELECT id FROM cuentas WHERE tipo='simulada' AND archivada=0"):
                cid = c['id']
                activa = db.uno("SELECT 1 x FROM ordenes WHERE cuenta_id=? AND estado='pendiente' UNION SELECT 1 FROM posiciones WHERE cuenta_id=? LIMIT 1", (cid, cid))
                if activa:
                    motor.procesar(cid)
                if activa or time.time() - ultima_foto.get(cid, 0) > 3600:
                    if time.time() - ultima_foto.get(cid, 0) > (300 if activa else 3600):
                        r = motor.instantanea(cid)
                        motor._control_dia(motor._fila(cid), r['equity'])
                        ultima_foto[cid] = time.time()
            for a in db.q("SELECT * FROM alertas WHERE activa=1 AND condicion<>'aviso'"):
                try:
                    qq = datos.cotizacion(a['simbolo'])
                except Exception:
                    continue
                p = qq['precio']
                si = (a['condicion'] == 'mayor' and p >= a['valor']) or (a['condicion'] == 'menor' and p <= a['valor']) or \
                     (a['condicion'] == 'cambio_pct' and qq['cambio_pct'] is not None and abs(qq['cambio_pct']) >= a['valor'])
                if si:
                    db.ex('UPDATE alertas SET activa=0, disparada=?, precio_disparo=? WHERE id=?', (int(time.time()), p, a['id']))
                    url = db.ajuste('webhook', '')
                    if url:
                        motor._webhook(url, f"🔔 {a['simbolo']} {a['condicion']} {a['valor']} → {p}" + (f" · {a['nota']}" if a['nota'] else ''))
        except Exception:
            traceback.print_exc()
        time.sleep(15)

# ---------- HTTP ----------


class Manejador(BaseHTTPRequestHandler):
    server_version = 'DMN-Trader/' + VERSION

    def log_message(self, *a):
        pass

    def _json(self, codigo, obj):
        cuerpo = json.dumps(obj, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(codigo)
        self.send_header('content-type', 'application/json; charset=utf-8')
        self.send_header('content-length', str(len(cuerpo)))
        self.send_header('cache-control', 'no-store')
        self.end_headers()
        self.wfile.write(cuerpo)

    def _autorizado(self, qs):
        if not TOKEN or self.client_address[0] in ('127.0.0.1', '::1'):
            return True
        return self.headers.get('x-token') == TOKEN or qs.get('token') == TOKEN

    def _atender(self, metodo):
        u = urlparse(self.path)
        qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
        if not u.path.startswith('/api/'):
            return self._estatico(u.path)
        if not self._autorizado(qs):
            return self._json(401, {'error': 'falta el token (x-token)'})
        cuerpo = None
        n = int(self.headers.get('content-length') or 0)
        if n:
            try:
                cuerpo = json.loads(self.rfile.read(n).decode('utf-8'))
            except Exception:
                return self._json(400, {'error': 'JSON inválido'})
        for m, rx, fn in RUTAS:
            mt = rx.match(u.path)
            if m == metodo and mt:
                try:
                    return self._json(200, fn(qs, cuerpo or {}, **mt.groupdict()))
                except (motor.ErrorOrden, ValueError) as e:
                    return self._json(400, {'error': str(e)})
                except KeyError as e:
                    return self._json(400, {'error': f'falta el parámetro {e}'})
                except datos.ErrorDatos as e:
                    return self._json(502, {'error': str(e)})
                except Exception as e:
                    traceback.print_exc()
                    return self._json(500, {'error': f'{type(e).__name__}: {e}'})
        self._json(404, {'error': 'ruta desconocida'})

    def _estatico(self, ruta_):
        f = os.path.normpath(os.path.join(WEB, (ruta_.lstrip('/') or 'index.html')))
        if not f.startswith(WEB) or not os.path.isfile(f):
            f = os.path.join(WEB, 'index.html')
        with open(f, 'rb') as h:
            datos_ = h.read()
        self.send_response(200)
        self.send_header('content-type', (mimetypes.guess_type(f)[0] or 'application/octet-stream') + ('; charset=utf-8' if f.endswith(('.html', '.js', '.css')) else ''))
        self.send_header('content-length', str(len(datos_)))
        self.end_headers()
        self.wfile.write(datos_)

    def do_GET(self):
        self._atender('GET')

    def do_POST(self):
        self._atender('POST')

    def do_PATCH(self):
        self._atender('PATCH')

    def do_DELETE(self):
        self._atender('DELETE')


def preparar():
    db.con()
    if not db.q('SELECT 1 FROM vigilancia LIMIT 1') and db.ajuste('vigilancia_iniciada') is None:
        for i, s in enumerate(VIGILANCIA_INICIAL):
            db.ex('INSERT OR IGNORE INTO vigilancia(simbolo,anadido) VALUES(?,?)', (s, i))
        db.poner_ajuste('vigilancia_iniciada', True)
    agente.preparar()
    if (agente.estado() or {}).get('activo'):
        agente.arrancar_hilo()  # el reto sigue tras un reinicio
    real.arrancar()  # solo donde está gmgn-cli con la key (el PC): vigila la cuenta real y la manda a la central
    if not db.q('SELECT 1 FROM cuentas LIMIT 1'):
        c = motor.crear_cuenta('Principal', 10000, 'USD')
        db.poner_ajuste('cuenta_defecto', c['id'])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default=os.environ.get('DMN_TRADER_HOST', '127.0.0.1'))
    ap.add_argument('--puerto', type=int, default=int(os.environ.get('DMN_TRADER_PUERTO', 8777)))
    a = ap.parse_args()
    preparar()
    threading.Thread(target=bucle, daemon=True).start()
    print(f'DMN Trader {VERSION} en http://{a.host}:{a.puerto}  (base: {db.RUTA})', flush=True)
    ThreadingHTTPServer((a.host, a.puerto), Manejador).serve_forever()


if __name__ == '__main__':
    main()
