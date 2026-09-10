@echo off
chcp 65001 >nul
title 酒馆桌面应用
cd /d "%~dp0"

echo ╔══════════════════════════════════════╗
echo ║       酒馆 — AI 互动文字游戏        ║
echo ╚══════════════════════════════════════╝
echo.

:: 检查 Python 虚拟环境
if exist "bar\Scripts\python.exe" (
    echo [OK] Python 虚拟环境: bar
) else (
    echo [!!] 未找到 bar 虚拟环境，尝试系统 Python...
)

:: 检查 Node.js
where node >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [!!] 未找到 Node.js，请先安装
    pause
    exit /b 1
)
echo [OK] Node.js: 已安装

:: 检查 Electron
cd electron
if not exist "node_modules\electron" (
    echo.
    echo [..] 首次运行，安装 Electron 依赖...
    set NODE_OPTIONS=--use-system-ca
    call npm install
    if %ERRORLEVEL% neq 0 (
        echo [!!] Electron 安装失败
        pause
        exit /b 1
    )
    echo [OK] Electron 安装完成
)
echo [OK] Electron: 已安装

echo.
echo [..] 启动中... 日志输出到 logs\ 目录
echo [..] 酒馆对话模式: http://127.0.0.1:8000
echo [..] RPG 推演系统:  http://127.0.0.1:8080
echo.
echo     Ctrl+1 切换酒馆    Ctrl+2 切换RPG
echo     F12 开发者工具     F11 全屏
echo.
echo ──────────────────────────────────────
echo.

set NODE_OPTIONS=--use-system-ca
npx electron .

echo.
echo [OK] 应用已关闭
