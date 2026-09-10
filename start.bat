@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Tavern

echo.
echo   Tavern - AI Interactive Fiction
echo   ================================
echo.

if not exist "bar\Scripts\python.exe" (
    echo   [..] Creating virtual environment...
    python -m venv bar
    if errorlevel 1 (
        echo   [!!] Failed to create venv
        pause
        exit /b 1
    )
    bar\Scripts\pip.exe install -r requirements.txt -q
    if exist "requirements-vector.txt" bar\Scripts\pip.exe install -r requirements-vector.txt -q 2>nul
    echo   [OK] Setup complete
)

echo   [OK] Starting server...
echo.
echo   Shell:  http://127.0.0.1:8500/shell
echo   Chat:   http://127.0.0.1:8500
echo   RPG:    http://127.0.0.1:8500/rpg
echo   ================================
echo.

set TAVERN_PORT=8500
start /b cmd /c "timeout /t 4 /nobreak >nul && start http://127.0.0.1:8500/shell"

bar\Scripts\python.exe app.py
pause