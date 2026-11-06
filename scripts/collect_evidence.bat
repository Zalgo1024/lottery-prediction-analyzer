@echo off
rem 方案 B 开奖证据采集（一键 / 窗口循环）。
rem   collect_evidence.bat            手工采一次，结束暂停看结果
rem   collect_evidence.bat once       手工采一次，不暂停
rem   collect_evidence.bat window     21:05-21:45 每 2 分钟循环采（供定时调用）
rem   collect_evidence.bat status     只看状态，不采集
rem
rem ★ 频率、窗口、单实例锁、日志的真源是 web/evidence_schedule.py；
rem   Flask 内置调度器在 21:05-21:45 也会自动按同一节拍采，两者共用锁与状态，
rem   同时存在不会重复采集。
rem ★ 采集只读数据源、只追加证据文件；不改 CSV / pending / 台账 / 预算。
chcp 65001 >nul
cd /d "%~dp0.."
set PY=E:\Python\python.exe
echo [采集] %date% %time% 模式=%~1

if /I "%~1"=="window" goto WINDOW
if /I "%~1"=="status" goto STATUS
if /I "%~1"=="once" goto ONCE

"%PY%" scripts\collect_evidence_window.py --once
echo [退出码] %ERRORLEVEL%
pause
goto END

:ONCE
"%PY%" scripts\collect_evidence_window.py --once
echo [退出码] %ERRORLEVEL%
goto END

:WINDOW
"%PY%" scripts\collect_evidence_window.py --window
echo [退出码] %ERRORLEVEL%
goto END

:STATUS
"%PY%" scripts\collect_evidence_window.py --status
goto END

:END
