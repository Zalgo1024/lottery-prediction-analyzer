# ============================================================
# auto_scheduled.ps1 - Windows 计划任务入口（每日 22:05 主任务 + 白天兜底）
# 与 Flask 内置调度器共用锁文件互斥，绝不并发写 CSV。
# 用法：
#   auto_scheduled.ps1           正常模式：跑当天该跑的全部彩种（LotteryAutoDaily 22:05）
#   auto_scheduled.ps1 -Daytime  白天兜底：仅补跑数据滞后的彩种（LotteryAutoDaytime_xx 每 3 小时）
#   auto_scheduled.ps1 -Force    强制独立跑（忽略一切，手动/测试用）
# 组数按彩种分流读 config.resolve_groups（滑轨当前生效值；勿在此写死数字）
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
    # ★ 回落值必须**喊出来**：静默用 100 会让这一期的注数与滑轨不一致，
    #   而启动自愈会按滑轨把 pending 重生成一遍 —— 从日志看像是「无缘无故又出了一次号」。
    Write-Log "⚠️ $lot 组数解析失败（resolve_groups 无输出），本次回落 100 注"
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
    # ★ 开奖日的**唯一真源是 config.LOTTERY_CONFIG[*]["draw_days"]**（Python weekday：
    #   周一=0 … 周日=6）。本脚本用的是 .NET `[DayOfWeek]`（周日=0），换算公式：
    #       .NET = (weekday + 1) % 7
    #   两套编码混用极易错位 ⇒ 例如七乐彩 draw_days=[0,2,4]（一/三/五）
    #   在本脚本里必须写成 @(1, 3, 5)，写成 @(0,2,4) 就变成「日/二/四」。
    # ★★ 2026-11-03：七乐彩、快乐8 此前**不在本脚本的清单里**（手写 6 个彩种）——
    #   一旦本任务被启用，这两个彩种就永远不会被它跑，而日志里没有任何一行说
    #   "跳过了谁"（典型静默缺席）。现按 draw_days 补齐。
    #   注：本机 2026-09-28 之后并未注册该任务（夜间由 Flask 内置调度器承担），
    #   所以这是「补齐潜在缺口」，不是「修一个正在发生的漏跑」。
    $dow = [int](Get-Date).DayOfWeek
    # 每日开奖：快乐8 + 数字型三兄弟（draw_days 均为 [0..6]）
    $lots = @('排列5', '福彩3D', '排列3', '快乐8')
    if ($dow -in @(2, 4, 0)) { $lots += '双色球' }   # 周二、周四、周日  (draw_days=[1,3,6])
    if ($dow -in @(1, 3, 6)) { $lots += '大乐透' }   # 周一、周三、周六  (draw_days=[0,2,5])
    if ($dow -in @(2, 5, 0)) { $lots += '七星彩' }   # 周二、周五、周日  (draw_days=[1,4,6])
    if ($dow -in @(1, 3, 5)) { $lots += '七乐彩' }   # 周一、周三、周五  (draw_days=[0,2,4])
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

    # ---------- 5. 项目自检（仅晚间主任务；跑完自动推体检摘要） ----------
    # 2026-09-18：health_check.py 此前从没有任何自动调用者，现在并入晚间流水线收尾。
    # 白天兜底模式不跑，避免一天多份重复体检推送。
    if (-not $Daytime) {
        Write-Log '>>> 项目自检 health_check'
        $env:PYTHONUTF8 = '1'
        & 'E:\Python\python.exe' 'E:\707\scripts\health_check.py' *>> $runLog 2>&1
        if ($LASTEXITCODE -eq 0) { Write-Log '<<< 项目自检完成（已推送体检摘要）' }
        else { Write-Log "<<< 项目自检失败 exit=$LASTEXITCODE - 详情见 auto_scheduled_run.log" }
    }

    Write-Log '==== 全部完成 ===='
} finally {
    Remove-Item $lockFile -Force -ErrorAction SilentlyContinue
    Write-Log '已释放锁文件'
}
