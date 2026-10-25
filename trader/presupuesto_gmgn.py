"""Minimo conocido de fondos: no es una cotizacion completa ni limita los costes del proveedor."""
from decimal import Decimal, InvalidOperation, ROUND_CEILING

PRIORIDAD = '0.00001'
PROPINA = '0.00001'
L = Decimal(1000000000)


class EntradaNoEnviada(ValueError):
    """Validacion fallida antes de entregar la orden al broker."""


def numero(value):
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
            raise ValueError()
        result=Decimal(str(value))
        if not result.is_finite() or result < 0:raise ValueError()
        return result
    except (ValueError,InvalidOperation):
        raise EntradaNoEnviada('importe o reserva no verificable') from None


def presupuesto(config):
    amount=numero(config['por_op_sol']);reserve=numero(config['reserva_sol'])
    lamports=amount*L
    if amount <= 0 or amount > Decimal('0.1') or lamports != lamports.to_integral_value():
        raise EntradaNoEnviada('importe fuera del limite o de la precision SOL')
    # Reserva conservadora por 1% en ambas piernas sobre el nominal inicial.
    # El coste real de la venta depende de su valor; gas, renta y tasas DEX no estan cotizados aqui.
    fees=int((lamports*Decimal('.01')).to_integral_value(rounding=ROUND_CEILING))*2
    network=int((Decimal(PRIORIDAD)+Decimal(PROPINA))*L)*2
    reserved=max(int((reserve*L).to_integral_value(rounding=ROUND_CEILING)),fees+network)
    return {'importe_lamports':int(lamports),'reserva_lamports':reserved,'necesario_lamports':int(lamports)+reserved,'comisiones_base_lamports':fees,'prioridad_propina_lamports':network,'cotizacion_completa':False}
