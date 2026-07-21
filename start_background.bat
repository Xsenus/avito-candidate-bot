@echo off
setlocal
cd /d "%~dp0"
if not exist .env (
    copy .env.example .env >nul
    echo Created .env. Fill it with your values and run the file again.
    pause
    exit /b 0
)

where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=py"
) else (
    where python >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_CMD=python"
    ) else (
        echo Python was not found. Install Python 3 and add it to PATH.
        pause
        exit /b 1
    )
)

start /b %PYTHON_CMD% -m uvicorn app:app --host 127.0.0.1 --port 8000 > server.log 2>&1
start /b %PYTHON_CMD% poller.py > poller.log 2>&1

echo Bot started in the background.
echo Logs: server.log and poller.log
pause
