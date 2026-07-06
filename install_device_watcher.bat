@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$startup=[Environment]::GetFolderPath('Startup'); $target=(Resolve-Path '.\Git Haptic Device Watcher.vbs').Path; $shortcut=Join-Path $startup 'Git Haptic Device Watcher.lnk'; $ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut($shortcut); $s.TargetPath=$target; $s.WorkingDirectory=(Get-Location).Path; $s.Save(); Write-Host 'Installed startup watcher:' $shortcut"
echo.
echo The watcher will start automatically when you log into Windows.
echo It opens the Git Haptic app when the FRDM board appears.
echo.
pause
