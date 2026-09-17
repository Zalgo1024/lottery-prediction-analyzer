@echo off
echo 正在删除计划任务 ...
schtasks /Delete /TN "LotteryAutoDaily" /F
schtasks /Delete /TN "LotteryAutoLogon" /F
schtasks /Delete /TN "LotteryAutoDaytime_09" /F
schtasks /Delete /TN "LotteryAutoDaytime_12" /F
schtasks /Delete /TN "LotteryAutoDaytime_15" /F
schtasks /Delete /TN "LotteryAutoDaytime_18" /F
echo [OK] 已删除（若提示找不到某任务属正常）
echo.
pause
