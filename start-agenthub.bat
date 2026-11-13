@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Run setup-agenthub.bat first.
    exit /b 1
)
if not exist "apps\web\node_modules\next\dist\bin\next" (
    echo [ERROR] Run setup-agenthub.bat to install frontend dependencies.
    exit /b 1
)
".venv\Scripts\python.exe" -m scripts.local_setup check-env || exit /b 1
call docker compose up -d --wait --wait-timeout 90 postgres redis qdrant || exit /b 1
".venv\Scripts\python.exe" -m scripts.local_setup prepare || exit /b 1

rem The supervisor keeps only its own processes and logs together. Ctrl+C stops them.
".venv\Scripts\python.exe" -m scripts.local_dev
exit /b %ERRORLEVEL%
