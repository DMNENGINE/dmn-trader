# DMN Trader

Simulador profesional de trading con **datos reales** (Yahoo Finance) y dinero **simulado**. Python 3 sin dependencias.

- **Mesa:** gráfico de velas (medias, Bollinger, VWAP, RSI), lista de vigilancia, órdenes de mercado / límite / stop / stop-límite / trailing, brackets SL+TP (OCO), largos y cortos, divisas convertidas a la moneda de la cuenta.
- **Riesgo:** % máximo por operación (rechaza órdenes que lo superen y da el tamaño correcto), pérdida diaria máxima (bloquea el día), máximo de posiciones, apalancamiento, stop obligatorio.
- **Diario:** cada trade con R, MFE/MAE, setup, emoción, si respetaste el plan y la lección.
- **Estadísticas:** acierto, factor de beneficio, esperanza, R medio, drawdown, Monte Carlo, por setup/símbolo/día y el **examen «¿listo para dinero real?»**.
- **Backtest:** 7 estrategias, comisiones y deslizamiento, stop/objetivo por ATR, tamaño por riesgo, comparación con comprar y mantener, Sharpe/Sortino/Calmar, Monte Carlo y optimizador con validación fuera de muestra.
- **Práctica (replay):** un trozo del pasado al azar, vela a vela, sin ver el futuro (→ avanza, espacio = auto).
- **Escáner y alertas** (con aviso a un webhook de Discord opcional).
- **APOLO:** skill `trader` (D:\RobotCompanion\skills\finanzas\trader) que maneja todo por voz/chat.

## Uso
- `iniciar.bat` → servidor en segundo plano + panel en http://127.0.0.1:8777
- `autoarranque.bat` → que arranque con Windows.
- En la Pi u otro equipo: `DMN_TRADER_TOKEN=secreto python3 -m trader.servidor --host 0.0.0.0` (el token se pide fuera de localhost).
- Datos en `datos/trader.db` (SQLite). Prueba: `python tests/prueba_motor.py`.

Los precios de acciones pueden ir con algo de retraso; las órdenes de acciones solo se ejecutan con el mercado abierto. No es asesoramiento financiero.
