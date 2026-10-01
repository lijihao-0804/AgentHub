@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "ROOT=%CD%"

echo ========================================
echo   Starting AgentHub
echo ========================================
echo.

docker info >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Docker Desktop is not running.
    echo Please start Docker Desktop first.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [ERROR] .env not found.
    echo Please run setup-agenthub.bat first.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python environment not found.
    echo Please run setup-agenthub.bat first.
    pause
    exit /b 1
)

if not exist "apps\web\node_modules" (
    echo [ERROR] Frontend dependencies not found.
    echo Please run setup-agenthub.bat first.
    pause
    exit /b 1
)

echo [1/5] Starting infrastructure...
docker compose up -d postgres redis qdrant

echo Waiting for PostgreSQL...

:waitdb
docker compose exec -T postgres pg_isready -U agenthub -d agenthub >nul 2>nul
if errorlevel 1 (
    timeout /t 2 /nobreak >nul
    goto :waitdb
)

echo.
echo [2/5] Starting API...
start "AgentHub API" cmd /k "cd /d ""%ROOT%"" && set AGENTHUB_PROCESS_ROLE=api&& uv run --locked uvicorn apps.api.main:app --reload --host 127.0.0.1 --port 8000"

echo.
echo [3/5] Starting Celery Worker...
start "AgentHub Worker" cmd /k "cd /d ""%ROOT%"" && set AGENTHUB_PROCESS_ROLE=worker&& uv run --locked celery -A apps.worker.celery_app worker --loglevel=INFO --pool=solo --concurrency=1"

echo.
echo [4/5] Starting Celery Beat...
start "AgentHub Beat" cmd /k "cd /d ""%ROOT%"" && set AGENTHUB_PROCESS_ROLE=beat&& uv run --locked celery -A apps.worker.celery_app beat --loglevel=INFO"

echo.
echo [5/5] Starting Web UI...
start "AgentHub Web" cmd /k "cd /d ""%ROOT%\apps\web"" && set AGENTHUB_API_PROXY_TARGET=http://127.0.0.1:8000&& npm run dev"

echo.
echo Waiting for services...
timeout /t 5 /nobreak >nul

echo.
echo Opening AgentHub...
start "" "http://localhost:3000"

echo.
echo ========================================
echo AgentHub is running
echo.
echo Web:  http://localhost:3000
echo API:  http://localhost:8000
echo Health:
echo       http://localhost:8000/api/v1/health
echo ========================================
echo.
echo You may close this window.
timeout /t 3 /nobreak >nul
exit /b 0