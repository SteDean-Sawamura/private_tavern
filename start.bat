@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title 酒馆 — AI 互动文字游戏

echo.
echo   酒馆 - AI 互动文字游戏
echo   ========================
echo.

:: 检查 Python
where python >nul 2>&1
if errorlevel 1 (
    echo   [!!] 未找到 Python，请安装 Python 3.10+
    pause
    exit /b 1
)
for /f "tokens=2 delims= " %%a in ('python --version 2^>^&1') do echo   [OK] Python %%a

:: 检查/创建虚拟环境
if not exist "bar\Scripts\python.exe" (
    echo   [..] 创建虚拟环境...
    python -m venv bar
    if errorlevel 1 (
        echo   [!!] 虚拟环境创建失败
        pause
        exit /b 1
    )
    call bar\Scripts\activate.bat
    echo   [..] 安装依赖...
    pip install -r requirements.txt -q
    if exist "requirements-vector.txt" pip install -r requirements-vector.txt -q 2>nul
    echo   [OK] 依赖安装完成
) else (
    echo   [OK] 虚拟环境: bar
    call bar\Scripts\activate.bat
)

echo.
echo   ========================
echo   酒馆服务启动中...
echo   统一入口: http://127.0.0.1:8000/shell
echo   对话模式: http://127.0.0.1:8000
echo   RPG推演:  http://127.0.0.1:8000/rpg
echo   ========================
echo.

:: 延迟打开浏览器
start /b cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:8000/shell"

:: 启动单一服务（包含对话+RPG）
python app.py
pause
