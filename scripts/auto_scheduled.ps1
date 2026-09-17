# ============================================================
# auto_scheduled.ps1 - Windows 计划任务入口（每日 22:05 主任务 + 白天兜底）
# 与 Flask 内置调度器共用锁文件互斥，绝不并发写 CSV。
# 用法：
#   auto_scheduled.ps1           正常模式：跑当天该跑的全部彩种（LotteryAutoDaily 22:05）
#   auto_scheduled.ps1 -Daytime  白天兜底：仅补跑数据滞后的彩种（LotteryAutoDaytime_xx 每 3 小时）
#   auto_scheduled.ps1 -Force    强制独立跑（忽略一切，手动/测试用）
# 组数按彩种分流读 config.GROUPS_BY_LOTTERY（勿在此写死；数字型 5 / 乐透型 100）
# ============================================================

param([switch]$Force, [switch]$Daytime)

$ErrorActionPreference = 'Continue'
Set-Location 'E:\707'

$logDir = 'E:\707\logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logFile = Join-Path $logDir 'auto_scheduled.log'
$runLog  = Join-Path $logDir 'auto_scheduled_run.log'

function Write-Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

# 每期组数：按彩种分流（数字型 5 / 乐透型 100），统一读 config.resolve_groups
# 说明：输出纯数字，避免中文键 JSON 在 PowerShell 侧的编码坑。
function Get-Groups($lot) {
    $g = & 'E:\Python\python.exe' -c "import sys;sys.path.insert(0,r'E:\707');from config import resolve_groups;print(resolve_groups(r'$lot'))" 2>$null
    if ($g) { return [int]$g }
    return 100
}

if ($Daytime) {
    Write-Log "==== 白天兜底触发（组数按彩种）===="
} else {
    Write-Log "==== 计划任务触发（组数按彩种）===="
}
if ($Force) { Write-Log '强制模式(-Force)：始终尝试运行' }

# ---------- 1. 锁文件互斥（防并发写 CSV） ----------
$lockFile = 'E:\707\lottery_data\.auto_scheduled.lock'
if (Test-Path $lockFile) {
    try {
        $age = (Get-Date) - (Get-Item $lockFile).LastWriteTime
        if ($age.TotalHours -lt 6) {
            Write-Log '检测到锁文件且 <6h，认为有实例在跑，跳过'
            exit 0
        }
        Write-Log '锁文件过期(>6h)，清除后继续'
        Remove-Item $lockFile -Force -ErrorAction SilentlyContinue
    } catch {
        Remove-Item $lockFile -Force -ErrorAction SilentlyContinue
    }
}
New-Item -ItemType File -Path $lockFile -Force | Out-Null
Write-Log '已创建锁文件'

try {
    # ---------- 2. 按当天星期几计算该跑的彩种 ----------
    $dow = [int](Get-Date).DayOfWeek
    $lots = @('排列5', '福彩3D', '排列3')      # 每日开奖
    if ($dow -in @(2, 4, 0)) { $lots += '双色球' }   # 周二、周四、周日
    if ($dow -in @(1, 3, 6)) { $lots += '大乐透' }   # 周一、周三、周六
    if ($dow -in @(2, 5, 0)) { $lots += '七星彩' }   # 周二、周五、周日
    Write-Log "当天(星期$dow)流水线彩种: $($lots -join ', ')"

    if ($Daytime) {
        # ---------- 3a. 白天兜底：仅补跑数据滞后的彩种（无滞后跳过，不重复出号） ----------
        foreach ($lot in $lots) {
            $lag = -1
            $lagJson = & 'E:\Python\python.exe' 'E:\707\cli.py' lag $lot --json 2>$null
            try {
                $j = $lagJson | ConvertFrom-Json
                $prop = $j.PSObject.Properties[$lot]
                if ($prop) { $lag = [int]$prop.Value }
            } catch { $lag = -1 }
            if ($lag -gt 0) {
                Write-Log ">>> $lot 数据滞后 ${lag} 天 -> 补跑"
                & 'E:\Python\python.exe' 'E:\707\cli.py' auto $lot --mode fresh --groups (Get-Groups $lot) *>> $runLog 2>&1
                if ($LASTEXITCODE -eq 0) { Write-Log "<<< $lot 补跑完成" }
                else { Write-Log "<<< $lot 补跑失败 exit=$LASTEXITCODE - 详情见 auto_scheduled_run.log" }
            } else {
                Write-Log "    $lot 无滞后（lag=$lag），跳过"
            }
        }
    } else {
        # ---------- 3b. 正常模式：跑当天该跑的全部彩种 ----------
        foreach ($lot in $lots) {
            $start = Get-Date
            Write-Log ">>> $lot 开始"
            & 'E:\Python\python.exe' 'E:\707\cli.py' auto $lot --mode fresh --groups (Get-Groups $lot) *>> $runLog 2>&1
            $dur = ((Get-Date) - $start).TotalSeconds
            if ($LASTEXITCODE -eq 0) {
                Write-Log "<<< $lot 完成 ($([math]::Round($dur,1))s)"
            } else {
                Write-Log "<<< $lot 失败 exit=$LASTEXITCODE ($([math]::Round($dur,1))s) - 详情见 auto_scheduled_run.log"
            }
        }
    }

    # ---------- 4. 七星彩每日补抓（纯数据，幂等；两种模式都做） ----------
    Write-Log '>>> 七星彩 每日补抓(纯数据)'
    & 'E:\Python\python.exe' 'E:\707\cli.py' update 七星彩 *>> $runLog 2>&1
    if ($LASTEXITCODE -eq 0) { Write-Log '<<< 七星彩 每日补抓完成' }
    else { Write-Log "<<< 七星彩 每日补抓失败 exit=$LASTEXITCODE - 详情见 auto_scheduled_run.log" }

    Write-Log '==== 全部完成 ===='
} finally {
    Remove-Item $lockFile -Force -ErrorAction SilentlyContinue
    Write-Log '已释放锁文件'
}
