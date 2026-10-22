@echo off
rem DMN Trader: arranca el servidor en segundo plano (sin ventana) y abre el panel.
cd /d "%~dp0"
powershell -NoProfile -Command "try { Invoke-RestMethod http://127.0.0.1:8777/api/salud -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 start "" pythonw -m trader.servidor
ping -n 3 127.0.0.1 >nul
if not "%1"=="/sinpanel" start "" http://127.0.0.1:8777
