@echo off
rem 一键采集开奖证据（方案 B）。双击即可，或由计划任务调用。
rem ★ 为什么建议贴着开奖时刻跑：证据里的「首次可观测时刻」越接近开奖，
rem   交叉校验越灵敏；如果隔几小时才采，「上界」会很松，只能确认「不矛盾」，
rem   抓不到「声明值写晚」的情况。
rem 建议时段（福彩 21:15 / 体彩 21:25 / 快乐8 21:30）：21:05 起每 2 分钟，跑到 21:45。
chcp 65001 >nul
cd /d "%~dp0.."
echo [采集] %date% %time%
E:\Python\python.exe scripts\collect_draw_evidence.py --all
echo [退出码] %ERRORLEVEL%
if "%~1"=="" pause
