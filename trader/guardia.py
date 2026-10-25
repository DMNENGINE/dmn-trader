"""Limites persistentes del enjambre SIMULADO; no controla GMGN ni NinjaTrader."""
import math
import threading
import time
from . import db

CLAVE = 'enjambre_guardia'
_hilo = None


def validar(duracion_min, perdida_max_pct):
    if isinstance(duracion_min, bool) or not isinstance(duracion_min, (int, float)) or not math.isfinite(duracion_min) or not 1 <= duracion_min <= 1440:
        raise ValueError('duracionMin debe estar entre 1 y 1440')
    if isinstance(perdida_max_pct, bool) or not isinstance(perdida_max_pct, (int, float)) or not math.isfinite(perdida_max_pct) or not 0.1 <= perdida_max_pct <= 100:
        raise ValueError('perdidaMaxPct debe estar entre 0.1 y 100')


def configurar(e, duracion_min, perdida_max_pct):
    validar(duracion_min, perdida_max_pct)
    cuentas = [r['cuenta_id'] for r in db.q('SELECT cuenta_id FROM ag_agentes WHERE activo=1')]
    if not cuentas:
        from . import reactivacion as R
        if not R.estado(e)['esperando_cierre']:
            raise ValueError('no hay cuentas para supervisar')
    fin = time.time() + duracion_min * 60
    if e.get('hasta'):
        fin = min(fin, e['hasta'])
    g = {'contrato': 'dmn-guardia/1', 'modo': 'simulado', 'estado': 'activo', 'sesion': e['sesion_id'],
         'vence': fin, 'perdidaMaxPct': perdida_max_pct, 'capitalInicial': e['capital'] * len(cuentas), 'cuentas': cuentas,
         'tradeDesde': db.uno('SELECT COALESCE(MAX(id),0) id FROM trades')['id'], 'pnl': 0.0}
    db.poner_ajuste(CLAVE, g)
    return g


def estado():
    return db.ajuste(CLAVE) or {}


def admitir(cid, reduce=False):
    if reduce:
        return
    from . import reactivacion as R
    R.preparar()
    if db.uno("SELECT 1 FROM ag_agentes a JOIN ag_esperas q ON q.agente_id=a.id WHERE a.cuenta_id=? AND a.activo=0 AND q.estado='esperando' LIMIT 1", (cid,)):
        raise ValueError('agente esperando cierre previo; nuevas entradas bloqueadas')
    g = estado()
    if not g or not db.uno('SELECT 1 FROM ag_agentes WHERE cuenta_id=?', (cid,)):
        return
    e = db.ajuste('enjambre') or {}
    if cid not in g.get('cuentas', []) or g.get('estado') != 'activo' or not e.get('activo') or e.get('sesion_id') != g.get('sesion') or time.time() >= g.get('vence', 0):
        raise ValueError('guardia: entrada bloqueada, enjambre detenido o fuera de plazo')


def detener(motivo):
    # Persiste el bloqueo antes de intentar cerrar posiciones o consultar precios.
    with db.LOCK:
        g = estado()
        if g:
            g.update(estado='detenido', motivo=motivo)
            db.poner_ajuste(CLAVE, g)
        e = db.ajuste('enjambre')
        if e:
            e['activo'] = False
            db.poner_ajuste('enjambre', e)


def evaluar():
    from . import agente, motor
    g = estado()
    if not g or g.get('estado') != 'activo':
        return None
    motivo = None
    if time.time() >= g['vence']:
        motivo = 'duracion maxima de la central'
    else:
        try:
            # El historial no se borra al renovar un intento: las quiebras no reinician el limite.
            ids = g['cuentas']
            if not ids:
                return None  # La duracion sigue vigente mientras todos esperan un cierre.
            marcas = ','.join('?' for _ in ids)
            t = db.uno(f"SELECT COALESCE(SUM(CASE WHEN estado='cerrado' THEN pnl ELSE pnl-comisiones END),0) pnl FROM trades WHERE id>? AND cuenta_id IN ({marcas})", (g['tradeDesde'], *ids))
            pnl = t['pnl']
            for cid in ids:
                c = motor._fila(cid)
                for p in motor.posiciones(cid, False):
                    b = motor.barra(c, p['simbolo'])  # Sin precio valido no se supone que la posicion no pierde.
                    pnl += p['cantidad'] * (b['c'] - p['precio_medio']) * motor._fx(c, p['moneda'])
            if not math.isfinite(pnl):
                raise ValueError('pnl no finito')
            with db.LOCK:
                actual = estado()
                if actual.get('sesion') != g['sesion'] or actual.get('estado') != 'activo':
                    return None
                actual['pnl'] = pnl
                db.poner_ajuste(CLAVE, actual)
            if pnl <= -g['capitalInicial'] * g['perdidaMaxPct'] / 100:
                motivo = 'perdida maxima acumulada en la central'
        except Exception:
            motivo = 'riesgo no verificable en la central'
    if motivo:
        detener(motivo)
        agente.parar(motivo)
    return motivo


def arrancar():
    global _hilo
    if _hilo and _hilo.is_alive():
        return
    def bucle():
        while True:
            try:
                evaluar()
            except Exception:
                detener('error de la guardia')
            time.sleep(1)
    _hilo = threading.Thread(target=bucle, daemon=True, name='guardia-enjambre')
    _hilo.start()
