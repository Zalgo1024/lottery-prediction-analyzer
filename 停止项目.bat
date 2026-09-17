@echo off
title 彩票预测分析系统 - 停止
cd /d "%~dp0"

echo 正在停止彩票预测分析系统（run.py 看门狗 + Flask 子进程）...
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/kill_flask.ps1 -IncludeSupervisor
echo 已停止。
timeout /t 2 >nul
