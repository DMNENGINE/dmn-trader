# Guardia persistente para el enjambre simulado

Esta guardia corresponde a `/api/enjambre/*`. No activa ni configura el piloto real de GMGN ni las cuentas reales de NinjaTrader, que son sistemas distintos presentes en este repositorio.

El inicio supervisado recibe `modo: "simulado"`, `duracionMin` y `perdidaMaxPct`, además de agentes, capital por agente, meta, mercados y horario. La central persiste la sesión, sus cuentas, vencimiento y capital asignado. Un hilo independiente comprueba el riesgo aunque APOLO esté cerrado. El límite también se comprueba al admitir entradas y antes de modificar efectivo en una ejecución simulada.

La pérdida acumulada incluye operaciones cerradas netas y posiciones abiertas. El historial se conserva al renovar intentos: ni una quiebra ni una evolución reinician el límite. Un error al verificar el riesgo bloquea nuevas entradas e intenta parar el enjambre. Las salidas reductoras siguen permitidas.

Un guardado tardío no puede reactivar una sesión detenida ni sustituir otra sesión. No se reinicia el capital de un intento si quedan posiciones que no pudieron cerrarse. La respuesta distingue `activo: false` de `parada_confirmada: true`, que requiere ausencia de posiciones pendientes.

`/api/enjambre/mapa` declara `modo: "simulado"`, `capacidades: ["dmn-guardia/1"]` y el estado de la guardia. Los nodos deben actualizarse antes de recibir planes supervisados; las órdenes remotas pendientes no se presentan como paradas confirmadas. APOLO bloquea el arranque contra centrales antiguas que no confirman este contrato.

El panel incorpora duración y pérdida máxima. Los arranques antiguos sin esos campos conservan su comportamiento de simulación para compatibilidad; no se presentan como sesiones supervisadas de APOLO. No se permite cambiar en caliente un plan que tenga una guardia activa.

## Seguridad adicional

- HTTP rechaza Host ajeno, cambios desde Origin ajeno, cuerpos excesivos y longitudes inválidas. Si hay token configurado también se exige desde localhost. Para escuchar fuera de localhost se requiere `DMN_TRADER_TOKEN`; nombres de host adicionales se declaran en `DMN_TRADER_HOSTS`.
- Los pilotos rechazan booleanos enviados como texto, números no finitos y configuraciones persistidas inválidas. La configuración inválida se trata como desactivada en memoria, sin borrar datos.
- Futuros calcula el riesgo de todos los contratos. Una cuenta usada en el plan simulado debe estar identificada como simulada por el puente.

## Verificación y despliegue

Pruebas aisladas: `python -m unittest discover -s tests -p test_guardia.py -v`. Usan bases temporales y bloquean llamadas externas a GMGN, NinjaTrader y proveedores de mercado.

Aplicar archivos al proyecto no reinicia ni actualiza procesos existentes. Es necesario reiniciar el servidor PC y desplegar los mismos cambios en la Pi para que la central activa anuncie el contrato nuevo. El AddOn `ninjatrader/DmnPuente.cs` (contrato `dmn-puente/2`, ruta `/orden/estado`) ya está copiado en `Documents/NinjaTrader 8/bin/Custom/AddOns/` (24/10/2026). Falta compilarlo en NinjaTrader (F5 en el editor de NinjaScript) y probar en Sim101 la idempotencia, los llenados parciales y los brackets. Con el AddOn antiguo el piloto de futuros se niega a enviar órdenes (exige `dmn-puente/2`).

Los límites observan precios y dependen del proceso de la central y sus fuentes. No garantizan un máximo matemático de pérdidas ante saltos de precio, cierres fallidos o caída del servidor. La guardia no liquida órdenes reales de otros pilotos.

## Piloto GMGN: PnL y revisión manual

- El neto de cada operación real queda «pendiente» hasta que sus recibos Solana (entrada y salida) están verificados por RPC; entonces se fija como el flujo de SOL de la wallet (incluye comisiones y la renta de la cuenta del token) y pasa a la conciencia de APOLO. Las operaciones anteriores al libro de recibos se dejan como estaban.
- Si queda una entrada sin confirmar o una incidencia crítica, las entradas se bloquean. Tras revisar GMGN a mano, el botón «✅ Ya lo revisé en GMGN» del panel (clave `conciliado` de la configuración) resuelve las incidencias y olvida la entrada sin confirmar. No activa el piloto.
- La fábrica cambió su versión de validación (`gmgn-causal-20261024-2`): las muestras vivas anteriores quedan en cuarentena y las estrategias deben volver a demostrar ventaja antes de ser promovidas.
