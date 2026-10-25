"""Servidor aislado para probar APOLO + guardia; nunca arranca pilotos reales."""
import json
import os
import socket
import tempfile
import time
import urllib.request
os.environ['DMN_TRADER_DB'] = os.path.join(tempfile.mkdtemp(prefix='dmn-central-fixture-'), 'fixture.db')
os.environ['DMN_TRADER_CENTRAL'] = ''
os.environ['DMN_TRADER_TOKEN'] = 'token-fixture-sin-valor'
from trader import agente as A, db, guardia, servidor


def prohibido(*a, **k):
    raise AssertionError('conexion externa prohibida en fixture')


urllib.request.urlopen = prohibido
socket.socket.connect = prohibido
A.onchain.disponible = lambda: False
A.precios = lambda: []
A.noticias = lambda n=15: {'activo': False}


class HiloFicticio:
    def is_alive(self):
        return True


def iniciar_hilo_ficticio():
    A._hilo = HiloFicticio()


A.arrancar_hilo = iniciar_hilo_ficticio


@servidor.ruta('POST', '/api/_fixture/vencer')
def vencer(q, b):
    g = guardia.estado()
    g['vence'] = time.time() - 1
    db.poner_ajuste(guardia.CLAVE, g)
    return {'ok': True}


@servidor.ruta('GET', '/api/_fixture/guardia')
def ver(q, b):
    return guardia.estado()


A.preparar()
http = servidor.ThreadingHTTPServer(('127.0.0.1', 0), servidor.Manejador)
print(json.dumps({'puerto': http.server_port}), flush=True)
http.serve_forever()
