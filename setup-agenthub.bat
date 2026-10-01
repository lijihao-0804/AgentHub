@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ========================================
echo   AgentHub First-time Setup
echo ========================================
echo.

where uv >nul 2>nul
if errorlevel 1 (
    echo [ERROR] uv not found.
    echo Install uv first.
    pause
    exit /b 1
)

where docker >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Docker not found.
    pause
    exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
    echo [ERROR] npm not found.
    pause
    exit /b 1
)

docker info >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Docker Desktop is not running.
    echo Please start Docker Desktop first.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [1/6] Creating .env...
    copy /Y ".env.example" ".env" >nul
) else (
    echo [1/6] Reusing existing .env
)

echo.
echo [2/6] Starting PostgreSQL / Redis / Qdrant...
docker compose up -d postgres redis qdrant
if errorlevel 1 goto :error

echo.
echo Waiting for PostgreSQL...

:waitdb
docker compose exec -T postgres pg_isready -U agenthub -d agenthub >nul 2>nul
if errorlevel 1 (
    timeout /t 2 /nobreak >nul
    goto :waitdb
)

echo PostgreSQL is ready.

echo.
echo [3/6] Preparing Python environment...

if not exist ".venv\Scripts\python.exe" (
    echo Python environment does not exist. Running uv sync...
    uv sync --locked
    if errorlevel 1 goto :error
) else (
    echo Existing .venv found. Reusing it.
)

echo.
echo [4/6] Preparing frontend environment...

if not exist "apps\web\node_modules" (
    pushd apps\web
    npm ci --no-audit --no-fund
    if errorlevel 1 (
        popd
        goto :error
    )
    popd
) else (
    echo Existing node_modules found. Reusing it.
)

echo.
echo [5/6] Applying AgentHub database migrations...

uv run --locked alembic upgrade head
if errorlevel 1 goto :error

echo.
echo [6/6] Initializing durable Agent checkpoint schema...

uv run --locked python -m scripts.bootstrap_langgraph_checkpoint ^
  --database-url "postgresql+asyncpg://agenthub:agenthub@localhost:5432/agenthub"

if errorlevel 1 goto :error

echo.
echo ========================================
echo   AgentHub setup completed successfully
echo ========================================
echo.
echo Now run:
echo   start-agenthub.bat
echo.
pause
exit /b 0

:error
echo.
echo ========================================
echo   Setup FAILED
echo ========================================
pause
exit /b 1