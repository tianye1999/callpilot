$ErrorActionPreference = 'Stop'

function Run-Checked($Exe, $Arguments, $Timeout = 90000) {
    $p = Start-Process -FilePath $Exe -ArgumentList $Arguments -WorkingDirectory $root -PassThru
    if (-not $p.WaitForExit($Timeout)) { throw "Timed out: $Exe $Arguments" }
    if ($p.ExitCode -ne 0) { throw "Exit $($p.ExitCode): $Exe $Arguments" }
}

$version = python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
if ($LASTEXITCODE -ne 0) { throw 'Cannot read version' }
$setup = (Resolve-Path "dist/CallPilot-$version-setup.exe").Path
$root = Join-Path $env:RUNNER_TEMP 'CallPilot installed test'
$install = Join-Path $root 'Program Files'
New-Item -ItemType Directory -Force $root | Out-Null
# Isolated user profile for data, not the runner's real %LOCALAPPDATA%.
$env:AGENTCALL_APP_SUPPORT_DIR = Join-Path $root 'User Data'
$env:CALLPILOT_SELFTEST_REPORT = Join-Path $root 'installed-selftest.json'
$env:PYTHONPATH = ''
$exe = Join-Path $install 'CallPilot.exe'
$service = $null
try {
    Run-Checked $setup "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /TASKS=autostart /DIR=`"$install`""
    $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    if ((Get-ItemPropertyValue $runKey CallPilot) -ne "`"$exe`" --login") { throw 'Invalid autostart entry' }
    if (-not (Test-Path (Join-Path $env:APPDATA 'Microsoft/Windows/Start Menu/Programs/CallPilot/CallPilot.lnk'))) {
        throw 'Start menu shortcut missing'
    }
    Run-Checked $exe '--selftest'
    $report = Get-Content $env:CALLPILOT_SELFTEST_REPORT -Raw | ConvertFrom-Json
    if (-not $report.ok -or -not $report.frozen -or $report.platform -ne 'win32') {
        throw 'Invalid installed selftest report'
    }
    # Assert no incumbent listener; never accept someone else's /api/meta.
    if (Get-NetTCPConnection -LocalPort 47100 -State Listen -ErrorAction SilentlyContinue) {
        throw 'Port 47100 already occupied'
    }
    $service = Start-Process $exe -ArgumentList '--service --hardware-selftest' -PassThru
    $meta = $null
    for ($i = 0; $i -lt 60; $i++) {
        if ($service.HasExited) { throw "Service exited: $($service.ExitCode)" }
        try { $meta = Invoke-RestMethod 'http://127.0.0.1:47100/api/meta' -TimeoutSec 1 } catch { }
        if ($meta) { break }
        Start-Sleep -Milliseconds 500
    }
    # Never log the meta response, including on failure: it contains setup tokens.
    $versionMatches = $null -ne $meta -and $meta.version -eq $version
    Write-Output "Backend responded=$($null -ne $meta) versionMatches=$versionMatches"
    if (-not $versionMatches) { throw 'Backend version did not match the package' }
    Run-Checked $exe '--shutdown'
    if (-not $service.WaitForExit(10000) -or $service.ExitCode -ne 0) { throw 'Service did not exit cleanly' }
    $userEnv = Join-Path $env:AGENTCALL_APP_SUPPORT_DIR '.env'
    Add-Content $userEnv '# installer smoke preservation marker'
    $envHash = (Get-FileHash $userEnv).Hash
    # Upgrade while an installed backend is running: installer must stop it itself.
    $service = Start-Process $exe -ArgumentList '--service --hardware-selftest' -PassThru
    Start-Sleep -Seconds 2
    Run-Checked $setup "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /TASKS=`"`" /DIR=`"$install`""
    if (-not $service.WaitForExit(10000)) { throw 'Upgrade left old service running' }
    if (Get-ItemProperty $runKey -Name CallPilot -ErrorAction SilentlyContinue) { throw 'Autostart not disabled' }
    if ((Get-FileHash $userEnv).Hash -ne $envHash) { throw 'Upgrade changed user .env' }
    $uninstallLog = Join-Path $root 'uninstall.log'
    Run-Checked (Join-Path $install 'unins000.exe') "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG=`"$uninstallLog`""
    # Inno's temporary clone can still be deleting the original uninstaller
    # after its process handle signals exit. Do not race that final cleanup.
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ((Test-Path $install) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 200
    }
    if (Test-Path $install) {
        throw "Program directory survived uninstall; diagnostic log: $uninstallLog"
    }
    if (-not (Test-Path (Join-Path $env:AGENTCALL_APP_SUPPORT_DIR '.env'))) { throw 'User data was deleted' }
    if ((Get-FileHash $userEnv).Hash -ne $envHash) { throw 'Uninstall changed user .env' }
    Get-Content $env:CALLPILOT_SELFTEST_REPORT
    Copy-Item $env:CALLPILOT_SELFTEST_REPORT dist/installer-selftest.json
} catch {
    if (Test-Path $install) {
        Write-Output 'Installed files remaining at failure (paths only; no file contents):'
        Get-ChildItem $install -Recurse -Force | Select-Object FullName, Length, Attributes | Format-Table -AutoSize
    }
    throw
} finally {
    if (Test-Path $exe) { Run-Checked $exe '--shutdown' }
}
