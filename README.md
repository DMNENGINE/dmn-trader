# DMN Trader

Simulador profesional de trading con **datos reales** (Yahoo Finance) y dinero **simulado**. Python 3 sin dependencias.

- **Mesa:** gráfico de velas (medias, Bollinger, VWAP, RSI), lista de vigilancia, órdenes de mercado / límite / stop / stop-límite / trailing, brackets SL+TP (OCO), largos y cortos, divisas convertidas a la moneda de la cuenta.
- **Riesgo:** % máximo por operación (rechaza órdenes que lo superen y da el tamaño correcto), pérdida diaria máxima (bloquea el día), máximo de posiciones, apalancamiento, stop obligatorio.
- **Diario:** cada trade con R, MFE/MAE, setup, emoción, si respetaste el plan y la lección.
- **Estadísticas:** acierto, factor de beneficio, esperanza, R medio, drawdown, Monte Carlo, por setup/símbolo/día y el **examen «¿listo para dinero real?»**.
- **Backtest:** 7 estrategias, comisiones y deslizamiento, stop/objetivo por ATR, tamaño por riesgo, comparación con comprar y mantener, Sharpe/Sortino/Calmar, Monte Carlo y optimizador con validación fuera de muestra.
- **Práctica (replay):** un trozo del pasado al azar, vela a vela, sin ver el futuro (→ avanza, espacio = auto).
- **Escáner y alertas** (con aviso a un webhook de Discord opcional).
- **APOLO:** paquete para [APOLO](https://github.com/DMNENGINE/apolo): se instala desde la tienda, APOLO arranca el motor y añade una pestaña al panel.

## Uso
- `iniciar.bat` → servidor en segundo plano + panel en http://127.0.0.1:8777
- `autoarranque.bat` → que arranque con Windows.
- En la Pi u otro equipo: `DMN_TRADER_TOKEN=secreto python3 -m trader.servidor --host 0.0.0.0` (el token se pide fuera de localhost).
- Datos en `datos/trader.db` (SQLite). Prueba: `python tests/prueba_motor.py`.

Los precios de acciones pueden ir con algo de retraso; las órdenes de acciones solo se ejecutan con el mercado abierto. No es asesoramiento financiero.

## Varios equipos (central y nodos)
Un equipo hace de **central** y los demás se unen como **nodos** con su propio enjambre:
- Central: `DMN_TRADER_TOKEN=<secreto> python3 -m trader.servidor --host 0.0.0.0` (en Linux, como servicio systemd con el token en `.env`).
  Panel: `http://<ip-de-la-central>:8777/#token=<token>` (se recuerda en el navegador).
- Nodo: `DMN_TRADER_CENTRAL=http://<ip-de-la-central>:8777 DMN_TRADER_CENTRAL_TOKEN=<token> DMN_TRADER_NODO=<nombre> python3 -m trader.servidor`.
  En Windows, `servidor-pc.bat` / `enjambre-pc.bat` leen la URL de `datos\central.txt` y el token de `datos\central_token.txt`.

## El reto de APOLO (`trader/agente.py`)
Agente que opera solo con microoperaciones (1m/5m/15m), hace la autopsia de cada pérdida, aplica arreglos, y al quebrar prueba candidatos en datos reales recientes antes del siguiente intento. Pestaña «🤖 APOLO» del panel o `trader.py agente …` desde APOLO.
