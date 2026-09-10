@echo off
chcp 65001 >nul
cd /d "%~dp0electron"

if not exist "node_modules\electron" (
    echo Installing Electron...
    set NODE_OPTIONS=--use-system-ca
    call npm install
)

echo Starting Tavern RPG Desktop...
set NODE_OPTIONS=--use-system-ca
npx electron .
