@echo off
chcp 65001 >nul 2>&1
title Tavern - AI Text Adventure

echo ============================================
echo     Tavern - AI Text Adventure Engine
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%a in ('python --version 2^>^&1') do set PYVER=%%a
echo [INFO] Python %PYVER%

if not exist "bar\Scripts\python.exe" (
    echo [INFO] Creating virtual environment...
    python -m venv bar
    if errorlevel 1 (
        echo [ERROR] Failed to create venv.
        pause
        exit /b 1
    )
    echo [OK] Venv created.
)

call bar\Scripts\activate.bat

echo [INFO] Checking dependencies...
pip install -r requirements.txt -q
if errorlevel 1 (
    echo [WARN] Some deps failed...
)
if exist "requirements-vector.txt" (
    pip install -r requirements-vector.txt -q 2>nul
)
echo [OK] Dependencies ready.

echo.
echo ============================================
echo  Starting server... Close window to stop.
echo ============================================
echo.

set TAVERN_PORT=8500
start /b cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:%TAVERN_PORT%"

python app.py
pause
