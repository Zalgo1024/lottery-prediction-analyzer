@echo off
chcp 65001 >nul
echo ============================================
echo  🎯  彩票预测分析系统 — 构建 exe
echo ============================================
echo.
echo 正在打包，请等待（约 2-5 分钟）...
echo.

pyinstaller LotteryAnalyzer.spec

echo.
echo ============================================
if %ERRORLEVEL% EQU 0 (
    echo  ✅ 构建成功！
    echo.
    echo  双击运行: dist\LotteryAnalyzer\LotteryAnalyzer.exe
    echo  或发送快捷方式到桌面
) else (
    echo  ❌ 构建失败，请检查错误信息
)
echo ============================================
pause
