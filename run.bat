@echo off
cd /d "%~dp0"
if not exist .env (
    copy .env.example .env >nul
    echo Создан .env. Заполните его своими данными и запустите файл ещё раз.
    pause
    exit /b 0
)

echo Запуск сервера...
python -m pip install -r requirements.txt >nul 2>&1
for /f %%i in ('python set_port.py') do set PORT=%%i
echo Запущен по адресу: http://127.0.0.1:%PORT%
python -m uvicorn app:app --host 127.0.0.1 --port %PORT%
pause
