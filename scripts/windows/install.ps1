# AgentCall Windows 常驻服务安装脚本（对应 macOS 的 scripts/launchd/install.sh）
#
# Windows 有 Quectel 官方驱动，AT 串口直接以 COMx 暴露，不需要 macOS 那样的
# USB→PTY 桥——因此只注册一个计划任务：
#   AgentCallApp —— AgentCall 主服务（app.py），登录自启 + 失败自动重启
#
# 用法（若被执行策略拦截，加 -ExecutionPolicy Bypass 即可，无需全局改策略）：
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 install
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 uninstall
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 status
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 restart
#
# 注意：本脚本尚未在 Windows 真机验证（开发机为 macOS），欢迎 issue 反馈。

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "uninstall", "status", "restart")]
    [string]$Command
)

$ErrorActionPreference = "Stop"
# 控制台输出统一 UTF-8，避免中文在 GBK 代码页下乱码
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# 路径全部从脚本位置推导（scripts\windows\ → 项目根），不硬编码
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$VenvPython  = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$AppScript   = Join-Path $ProjectRoot "app.py"
$LogFile     = Join-Path $ProjectRoot "data\app_console.log"
$TaskName    = "AgentCallApp"

function Write-Log([string]$Message) {
    Write-Host "[install.ps1] $Message"
}

function Get-AppTask {
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Install-App {
    if (-not (Test-Path $VenvPython)) {
        throw "找不到 $VenvPython —— 请先创建 venv 并安装依赖（见 scripts\windows\README.md）"
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null

    # 经 cmd /c 包一层做字节直通重定向：PowerShell 的 *>> 在 PS 5.1 下会把
    # 输出重编码成 UTF-16 并按 OEM 代码页转译中文（乱码），cmd 的 >> 不碰字节。
    # PYTHONUTF8=1 让 python 侧 stdout/日志一律 UTF-8，与 app.py 的 _force_utf8 呼应。
    $cmdArg = "/c set PYTHONUTF8=1 && `"$VenvPython`" `"$AppScript`" >> `"$LogFile`" 2>&1"
    $action = New-ScheduledTaskAction -Execute "$env:ComSpec" `
        -Argument $cmdArg -WorkingDirectory $ProjectRoot
    # 域机/AzureAD 帐户下裸用户名可能解析失败，按惯例带域前缀
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    # ExecutionTimeLimit=0 关掉计划任务默认 72 小时强杀；失败后每分钟重启
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew `
        -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)

    if (Get-AppTask) {
        Write-Log "任务已存在，先移除旧的 $TaskName"
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    Register-ScheduledTask -TaskName $TaskName -Action $action `
        -Trigger $trigger -Settings $settings | Out-Null
    Write-Log "已注册计划任务 $TaskName（登录自启），正在启动…"
    Start-ScheduledTask -TaskName $TaskName
    Write-Log "安装完成。用 'install.ps1 status' 查看状态。"
}

function Uninstall-App {
    $task = Get-AppTask
    if (-not $task) {
        Write-Log "任务 $TaskName 未安装，无需卸载"
        return
    }
    if ($task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Log "卸载完成。"
}

function Show-Status {
    $task = Get-AppTask
    if (-not $task) {
        Write-Log "${TaskName}: 未安装"
        return
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Log ("{0}: {1}（上次运行 {2}，上次结果 0x{3:X}）" -f `
        $TaskName, $task.State, $info.LastRunTime, $info.LastTaskResult)
    Write-Log "服务日志：$LogFile"
}

function Restart-App {
    $task = Get-AppTask
    if (-not $task) {
        throw "尚未安装（缺任务 $TaskName），请先执行 'install.ps1 install'"
    }
    if ($task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName
        # Stop-ScheduledTask 异步返回；MultipleInstances=IgnoreNew 下若实例仍在
        # Running 就 Start，会被静默丢弃（只停不启）。轮询等实例真正退出。
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-ScheduledTask -TaskName $TaskName).State -eq "Running") {
            if ((Get-Date) -gt $deadline) { throw "停止任务超时（30s），请手动检查" }
            Start-Sleep -Milliseconds 500
        }
    }
    Start-ScheduledTask -TaskName $TaskName
    Write-Log "重启完成。"
}

function Show-Usage {
    Write-Host "用法: powershell -ExecutionPolicy Bypass -File install.ps1 <install|uninstall|status|restart>"
    exit 1
}

switch ($Command) {
    "install"   { Install-App }
    "uninstall" { Uninstall-App }
    "status"    { Show-Status }
    "restart"   { Restart-App }
    default     { Show-Usage }
}
