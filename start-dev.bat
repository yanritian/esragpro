@echo off
REM esRAGPro Development Startup Script for Windows

echo ========================================
echo   esRAGPro Development Environment
echo ========================================
echo.

REM Check if .env exists
if not exist .env (
    echo Creating .env from .env.example...
    copy .env.example .env
    echo Please edit .env with your configuration and run again.
    pause
    exit /b 1
)

REM Start Elasticsearch (requires Docker)
echo Checking Elasticsearch...
curl -s http://localhost:9200/_cluster/health >nul 2>&1
if errorlevel 1 (
    echo Starting Elasticsearch with Docker...
    docker run -d --name esragpro-es -p 9200:9200 -p 9300:9300 -e "discovery.type=single-node" -e "xpack.security.enabled=false" -e "ES_JAVA_OPTS=-Xms1g -Xmx1g" docker.elastic.co/elasticsearch/elasticsearch:8.12.0
    echo Waiting for Elasticsearch to start...
    timeout /t 30 /nobreak >nul
)

REM Start Backend
echo.
echo Starting Backend...
cd backend
if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)
call venv\Scripts\activate
pip install -e . -q
start "esRAGPro Backend" cmd /k "venv\Scripts\activate && uvicorn app.main:app --reload --port 8000"
cd ..

REM Start Frontend
echo.
echo Starting Frontend...
cd frontend
if not exist node_modules (
    echo Installing frontend dependencies...
    npm install --registry=https://registry.npmmirror.com
)
start "esRAGPro Frontend" cmd /k "npm run dev"
cd ..

echo.
echo ========================================
echo   Services starting...
echo   Backend:  http://localhost:8000
echo   Frontend: http://localhost:3000
echo   ES:       http://localhost:9200
echo ========================================
echo.
echo Press any key to open browser...
pause >nul
start http://localhost:3000