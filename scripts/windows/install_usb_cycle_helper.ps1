# One-time admin install: scheduled task so Edge can soft-cycle USB without UAC
# prompts on every hangup.
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_usb_cycle_helper.ps1
param(
    [string]$Vid = "1E0E",
    [string]$TaskName = "CallPilotSimTechUsbCycle"
)

$ErrorActionPreference = "Stop"
$script = Join-Path $PSScriptRoot "soft_cycle_simtech_usb.ps1"
if (-not (Test-Path $script)) {
    throw "Missing $script"
}

$arg = "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Vid $Vid"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null

# A SYSTEM task is admin-only by default, so the unelevated Edge process gets
# "Access is denied" from schtasks /Run and silently falls back to a PowerShell
# attempt that cannot Disable-PnpDevice either. Grant Authenticated Users
# read+execute so the app can trigger the cycle without UAC.
$sddl = "D:(A;;GA;;;BA)(A;;GA;;;SY)(A;;GRGX;;;AU)"
$svc = New-Object -ComObject "Schedule.Service"
$svc.Connect()
$svc.GetFolder("\").GetTask($TaskName).SetSecurityDescriptor($sddl, 0)

# ProgramData\CallPilot\usb-cycle.status is written by SYSTEM; without this the
# app cannot delete or overwrite it and would keep reading a stale OK.
$statusDir = Join-Path $env:ProgramData "CallPilot"
if (-not (Test-Path $statusDir)) {
    New-Item -ItemType Directory -Path $statusDir -Force | Out-Null
}
icacls $statusDir /grant "*S-1-5-11:(OI)(CI)M" /T | Out-Null

Write-Output "Installed scheduled task '$TaskName' (SYSTEM, highest), runnable by authenticated users."
