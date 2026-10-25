@echo off
rem Servidor de DMN Trader en ESTE PC conectado a la central (otro equipo; su URL en datos\central.txt), sin arrancar el enjambre.
rem Vigila la cuenta REAL de GMGN (solo lectura, la key está en este PC) y la manda a la Pi cada 10 s: se ve en el panel → 💰 Real (GMGN).
rem Si el enjambre del PC estaba activo, sigue (para eso está enjambre-pc.bat).
cd /d "%~dp0"
set /p DMN_TRADER_CENTRAL_TOKEN=<datos\central_token.txt
set /p DMN_TRADER_CENTRAL=<datos\central.txt
set "DMN_TRADER_NODO=pc"
powershell -NoProfile -Command "try { Invoke-RestMethod http://127.0.0.1:8777/api/salud -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 start "" pythonw -m trader.servidor
echo Servidor del PC en marcha: cuenta real visible en el panel de la Pi (pestaña Real).
