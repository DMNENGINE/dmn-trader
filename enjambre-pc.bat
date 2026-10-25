@echo off
rem Enjambre en ESTE PC como nodo extra de la central (otro equipo; su URL en datos\central.txt). Uso: enjambre-pc.bat [agentes] [hasta]
rem   enjambre-pc.bat            -> 250 agentes sin hora de fin
rem   enjambre-pc.bat 300 07:00  -> 300 agentes hasta las 7:00
rem Los agentes del PC aparecen en Mission Control (Trading) en su propia zona e intercambian sus mejores sistemas con la Pi.
cd /d "%~dp0"
set "N=%~1"
if "%N%"=="" set "N=250"
set "HASTA=%~2"
set /p DMN_TRADER_CENTRAL_TOKEN=<datos\central_token.txt
set /p DMN_TRADER_CENTRAL=<datos\central.txt
set "DMN_TRADER_NODO=pc"
powershell -NoProfile -Command "try { Invoke-RestMethod http://127.0.0.1:8777/api/salud -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 start "" pythonw -m trader.servidor
ping -n 5 127.0.0.1 >nul
powershell -NoProfile -Command "$b = @{ agentes = [int]$env:N; capital = 1; meta = 100; mercados = @('cripto','acciones') }; if ($env:HASTA) { $b.hasta = $env:HASTA }; $r = Invoke-RestMethod http://127.0.0.1:8777/api/enjambre/iniciar -Method Post -ContentType 'application/json' -Body ($b | ConvertTo-Json); Write-Host ('Enjambre del PC: ' + $r.agentes + ' agentes operando. Míralo en Mission Control > Trading.')"
