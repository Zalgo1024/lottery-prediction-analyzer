@echo off
echo ============================================
echo   彩票自动流水线 - 安装 Windows 计划任务
echo ============================================
echo   任务1: LotteryAutoDaily         每天 22:05 主流水线（抓取+评估+预测）
echo   任务2: LotteryAutoLogon         每次登录/开机 兜底补跑（-Force）
echo   任务3: LotteryAutoDaytime_xx    白天 09/12/15/18 点 漏跑检测+按需补跑（-Daytime）
echo   双保险: 与 Flask 内置调度器共用锁文件，不会并发写数据
echo --------------------------------------------
schtasks /Create /TN "LotteryAutoDaily" /TR "powershell -NoProfile -ExecutionPolicy Bypass -File E:\707\scripts\auto_scheduled.ps1" /SC DAILY /ST 22:05 /F
if not errorlevel 1 (echo [OK] LotteryAutoDaily 创建成功) else (echo [失败] LotteryAutoDaily 错误码 %errorlevel%)

schtasks /Create /TN "LotteryAutoLogon" /TR "powershell -NoProfile -ExecutionPolicy Bypass -File E:\707\scripts\auto_scheduled.ps1 -Force" /SC ONLOGON /F
if not errorlevel 1 (echo [OK] LotteryAutoLogon 创建成功) else (echo [失败] LotteryAutoLogon 错误码 %errorlevel%)

for %%T in (09 12 15 18) do (
  schtasks /Create /TN "LotteryAutoDaytime_%%T" /TR "powershell -NoProfile -ExecutionPolicy Bypass -File E:\707\scripts\auto_scheduled.ps1 -Daytime" /SC DAILY /ST %%T:00 /F
  if not errorlevel 1 (echo [OK] LotteryAutoDaytime_%%T 创建成功) else (echo [失败] LotteryAutoDaytime_%%T 错误码 %errorlevel%)
)
echo.
echo 验证: schtasks /Query /TN LotteryAutoDaily /V
echo.
pause
