# Install / remove / inspect the logon auto-start entry for the lottery system.
#
# Mechanism: HKCU\Software\Microsoft\Windows\CurrentVersion\Run  (per-user, no admin).
#
# Why not the two "obvious" options (both measured on this machine, 2026-11-04):
#   * Register-ScheduledTask  -> "Access denied" (needs elevation / blocked by policy).
#   * Startup folder .bat     -> write to %APPDATA%\...\Startup is denied by the OS
#                                (AuthorizationException, UnauthorizedAccessException).
#   * COM WScript.Shell (.lnk) -> blocked by the host security policy.
# The Run key is the only per-user mechanism that actually worked here.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Status
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Remove

param(
    [switch]$Remove,
    [switch]$Status,
    [string]$ProjectDir = 'E:\707'
)

$ErrorActionPreference = 'Stop'
$key  = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$name = 'LotteryAnalysis'
$bat  = Join-Path $ProjectDir '启动项目.bat'
$want = '"' + $bat + '"'

function Get-Current {
    try { (Get-ItemProperty -Path $key -Name $name -ErrorAction Stop).$name }
    catch { $null }
}

if ($Status) {
    $cur = Get-Current
    if ($null -eq $cur) {
        Write-Output "[NOT INSTALLED] $name is absent from $key"
    } else {
        Write-Output "[INSTALLED] $name = $cur"
        Write-Output ("  target exists : " + (Test-Path $bat))
        Write-Output ("  will run at   : next user logon")
    }
    exit 0
}

if ($Remove) {
    $cur = Get-Current
    if ($null -eq $cur) {
        Write-Output "[SKIP] not installed"
    } else {
        Remove-ItemProperty -Path $key -Name $name -Force
        Write-Output "[OK] removed $name"
    }
    exit 0
}

if (-not (Test-Path $bat)) {
    Write-Output "[FAIL] launcher not found: $bat"
    exit 1
}

Set-ItemProperty -Path $key -Name $name -Value $want -Type String
$cur = Get-Current

Write-Output "[OK] installed"
Write-Output ("  key    : " + $key + " \" + $name)
Write-Output ("  value  : " + $cur)
Write-Output ("  target : " + $bat + "  (exists=" + (Test-Path $bat) + ")")
Write-Output ""
Write-Output "Takes effect at next logon. This only restarts the WEB SERVICE;"
Write-Output "it does NOT create a second pipeline path (see README 'Autostart')."
