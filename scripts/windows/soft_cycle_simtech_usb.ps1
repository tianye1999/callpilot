# Soft-cycle SimTech/SIMCom USB composite (VID_1E0E) so Audio COM recovers
# closer to a cold power-on. Requires elevation (or the CallPilot scheduled task).
param(
    [string]$Vid = "1E0E",
    # SYSTEM 任务与用户进程的 %TEMP% 不同；默认落到 ProgramData 供双方可见。
    [string]$StatusFile = "$env:ProgramData\CallPilot\usb-cycle.status",
    [int]$DisableSeconds = 3,
    [int]$EnableWaitSeconds = 6
)

$ErrorActionPreference = "Stop"
$Vid = $Vid.ToUpperInvariant()

function Write-Status([string]$Line) {
    $dir = Split-Path -Parent $StatusFile
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    Set-Content -Path $StatusFile -Value $Line -Encoding UTF8
}

try {
    # Prefer the USB composite root (no MI_xx function). Never cycle WWAN/Audio
    # child interfaces alone — that leaves AT half-alive and does not reset PCM.
    $all = @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
        Where-Object {
            $_.InstanceId -like "USB\VID_$Vid&PID_*" -and
            $_.InstanceId -notlike '*&MI_*'
        })
    $dev = $all |
        Where-Object { $_.Class -eq 'USB' -and $_.FriendlyName -match 'Composite' } |
        Select-Object -First 1
    if (-not $dev) {
        $dev = $all | Where-Object { $_.Class -eq 'USB' } | Select-Object -First 1
    }

    if (-not $dev) {
        Write-Status "FAIL:no_device vid=$Vid"
        exit 2
    }

    Write-Output "Cycling: $($dev.FriendlyName) [$($dev.InstanceId)]"
    Disable-PnpDevice -InstanceId $dev.InstanceId -Confirm:$false
    Start-Sleep -Seconds $DisableSeconds
    Enable-PnpDevice -InstanceId $dev.InstanceId -Confirm:$false
    Start-Sleep -Seconds $EnableWaitSeconds
    Write-Status "OK:$($dev.InstanceId)"
    exit 0
}
catch {
    Write-Status "FAIL:$($_.Exception.Message)"
    exit 1
}
