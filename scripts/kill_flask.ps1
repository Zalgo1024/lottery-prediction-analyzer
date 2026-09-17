<#
.SYNOPSIS
    Kill Flask processes for the lottery-prediction system (port + command-line match).
.DESCRIPTION
    - Find processes listening on the given port;
    - Find processes by command-line signature (web/app.py / launcher.py, optionally run.py supervisor);
    - Exclude the script itself and its parent to avoid self-kill;
    - Kill the whole process tree with taskkill /T /F.
.PARAMETER Port
    Listen port, default 5000.
.PARAMETER IncludeSupervisor
    Also kill the run.py watchdog (used by "stop the whole program", since run.py does not bind a port).
#>
param(
    [int]$Port = 5000,
    [switch]$IncludeSupervisor
)

$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$me = $PID
$parent = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction SilentlyContinue).ParentProcessId

$candidates = @{}

# 1) port listener
$conns = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
if ($conns) {
    foreach ($c in $conns) { $candidates[$c.OwningProcess] = $true }
}

# 2) command-line signature
$filter = "CommandLine LIKE '%web/app.py%' OR CommandLine LIKE '%launcher.py%'"
if ($IncludeSupervisor) {
    $filter = $filter + " OR CommandLine LIKE '%run.py%'"
}
$wmi = Get-CimInstance Win32_Process -Filter $filter -ErrorAction SilentlyContinue
if ($wmi) {
    foreach ($w in $wmi) { $candidates[$w.ProcessId] = $true }
}

# exclude self and parent
$found = $candidates.Keys | Where-Object { $_ -ne $me -and $_ -ne $parent }

if (-not $found) {
    Write-Host "No residual Flask process (port $Port is free)"
    exit 0
}

foreach ($p in $found) {
    if ($p -eq $me -or $p -eq $parent) { continue }
    $name = (Get-CimInstance Win32_Process -Filter "ProcessId=$p" -ErrorAction SilentlyContinue).Name
    taskkill /PID $p /T /F | Out-Null
    Write-Host "Killed PID=$p ($name)"
}

exit 0