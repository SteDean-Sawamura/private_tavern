@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title RPG Multi-Agent Engine

echo ============================================
echo     RPG Multi-Agent Engine
echo ============================================
echo.

if not exist "bar\Scripts\python.exe" (
    echo [ERROR] Venv 'bar' not found. Please run start.bat first to set up the tavern environment.
    pause
    exit /b 1
)

call bar\Scripts\activate.bat

echo [INFO] Using tavern venv: bar
echo.
echo ============================================
echo  Starting RPG server on http://127.0.0.1:8080
echo  Close this window to stop.
echo ============================================
echo.

start /b cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:8080"

python tavern_rpg_engine.py
pause
