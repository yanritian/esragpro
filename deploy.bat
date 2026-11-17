@echo off
REM esRAGPro Production Deployment Script for Windows

echo ========================================
echo   esRAGPro Production Deployment
echo ========================================
echo.

REM Check if .env exists
if not exist .env (
    echo Error: .env file not found!
    echo Please copy .env.example to .env and configure it.
    pause
    exit /b 1
)

echo Building and starting services...
docker compose up -d --build

echo.
echo Waiting for services to be healthy...
timeout /t 10 /nobreak >nul

REM Check Elasticsearch
echo Checking Elasticsearch...
:check_es
curl -s http://localhost:9200/_cluster/health >nul 2>&1
if errorlevel 1 (
    echo Waiting for Elasticsearch...
    timeout /t 5 /nobreak >nul
    goto check_es
)
echo Elasticsearch is healthy!

REM Check Backend
echo Checking Backend...
:check_backend
curl -s http://localhost:8000/health >nul 2>&1
if errorlevel 1 (
    echo Waiting for Backend...
    timeout /t 5 /nobreak >nul
    goto check_backend
)
echo Backend is healthy!

echo.
echo ========================================
echo   Deployment Complete!
echo   Frontend: http://localhost
echo   Backend:  http://localhost:8000
echo   ES:       http://localhost:9200
echo ========================================
pause