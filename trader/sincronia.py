"""Validacion de versiones remotas; no constituye exclusividad de ejecucion."""
import time


def fecha(config):
    ts = config.get('cambiado', 0)
    if isinstance(ts, bool) or not isinstance(ts, int) or ts < 0 or ts > time.time() + 300:
        raise ValueError('fecha de configuracion invalida; revisar relojes de PC y Pi')
    return ts
