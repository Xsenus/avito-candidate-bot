@echo off
cd /d "%~dp0"
start "Avito Bot" powershell.exe -NoExit -NoProfile -ExecutionPolicy Bypass -File ".\start.ps1"
if errorlevel 1 (
    echo Ошибка запуска.
    pause
    exit /b 1
)
exit /b 0
