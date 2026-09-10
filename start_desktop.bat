@echo off
chcp 65001 >nul
title 酒馆 RPG — 多Agent推演系统
echo 正在启动酒馆 RPG 桌面应用...
cd /d "%~dp0electron"
npx electron .
