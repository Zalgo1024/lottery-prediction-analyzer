@echo off
title 开奖数据浏览器 - market.db
cd /d "%~dp0"

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

if not exist "market\market.db" (
    echo [错误] 找不到 market\market.db
    echo 请先运行: python -m market.build --csv
    pause
    exit /b 1
)

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo ============================================
echo   开奖数据浏览器  market.db
echo ============================================
echo.
echo   地址: http://127.0.0.1:5001/
echo   浏览器会自动打开，关闭本窗口即停止服务
echo.

"%PY%" -m market.viewer --port 5001 --open
echo.
echo 服务已停止
pause
