@echo off
rem Para el enjambre de este PC (cierra sus posiciones) y apaga el servidor local.
powershell -NoProfile -Command "try { Invoke-RestMethod http://127.0.0.1:8777/api/enjambre/parar -Method Post | Out-Null } catch {}; $p = (Get-NetTCPConnection -LocalPort 8777 -State Listen -ErrorAction SilentlyContinue).OwningProcess; if ($p) { Stop-Process -Id $p -Force }; Write-Host 'Enjambre del PC parado.'"
