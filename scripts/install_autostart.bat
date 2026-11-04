@echo off
rem One-click install / remove / status for the logon auto-start entry.
rem   double-click            -> install
rem   install_autostart.bat -Status
rem   install_autostart.bat -Remove
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" %*
echo.
pause
