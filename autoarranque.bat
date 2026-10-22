@echo off
rem Crea un acceso directo en Inicio de Windows para que DMN Trader arranque solo (sin abrir el panel).
set "DEST=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\DMN Trader.lnk"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%DEST%'); $s.TargetPath='%~dp0iniciar.bat'; $s.Arguments='/sinpanel'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save()"
echo Listo: DMN Trader arrancara con Windows.
