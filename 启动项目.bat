@echo off
title 彩票预测分析系统 - 启动
cd /d "%~dp0"

echo ============================================
echo   彩票预测分析系统 - 启动 Web 服务
echo ============================================
echo.

:: 自动探测 Python
set "PY="
if exist "E:\Python\python.exe" set "PY=E:\Python\python.exe"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [错误] 未找到 Python，请安装 Python 3.10+ 或加入 PATH
    pause
    exit /b 1
)
echo [1/2] 使用 Python: %PY%

:: 清理残留进程
echo [2/2] 清理残留旧进程...
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/kill_flask.ps1 -IncludeSupervisor

:: 启动看门狗（run.py 会拉起最新代码的 Flask，并自动开浏览器）
start "彩票预测分析系统" "%PY%" run.py

echo.
echo ============================================
echo   已启动！服务窗口标题为「彩票预测分析系统」
echo   访问地址: http://127.0.0.1:5000
echo   关闭该服务窗口即停止程序。
echo   重启加载最新代码：点网页右上角「重启服务」或双击「重启项目.bat」
echo ============================================
echo.
timeout /t 2 >nul
