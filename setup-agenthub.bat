@echo off
setlocal
cd /d "%~dp0"

where uv >nul 2>nul || (echo [ERROR] Install uv and Python 3.12 first. & exit /b 1)
where npm >nul 2>nul || (echo [ERROR] Install Node.js 20+ and npm first. & exit /b 1)
call docker info >nul 2>nul || (echo [ERROR] Start Docker Desktop with Docker Compose first. & exit /b 1)

echo Installing locked Python dependencies...
call uv sync --locked || exit /b 1
".venv\Scripts\python.exe" -m scripts.local_setup init || exit /b 1

echo Installing locked frontend dependencies...
call npm --prefix apps\web ci --no-audit --no-fund || exit /b 1

echo Starting the local database, queue and retrieval index...
call docker compose up -d --wait --wait-timeout 90 postgres redis qdrant || exit /b 1
".venv\Scripts\python.exe" -m scripts.local_setup prepare || exit /b 1

echo Setup complete. Run start-agenthub.bat to open AgentHub.
exit /b 0
