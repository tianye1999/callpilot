$ErrorActionPreference = 'Stop'

function Run-Checked($Exe, $Arguments, $Timeout = 90000) {
    $p = Start-Process -FilePath $Exe -ArgumentList $Arguments -PassThru
    if (-not $p.WaitForExit($Timeout)) { throw "Timed out: $Exe $Arguments" }
    if ($p.ExitCode -ne 0) { throw "Exit $($p.ExitCode): $Exe $Arguments" }
}

function Wait-ForMeta($Exe, $Service, $Version) {
    $meta = $null
    for ($i = 0; $i -lt 60; $i++) {
        if ($Service.HasExited) { throw "Service exited: $($Service.ExitCode)" }
        try { $meta = Invoke-RestMethod 'http://127.0.0.1:47100/api/meta' -TimeoutSec 1 } catch { }
        if ($meta) { break }
        Start-Sleep -Milliseconds 500
    }
    # Never log the meta response, including on failure: it contains setup tokens.
    return ($null -ne $meta -and $meta.version -eq $Version), ($null -ne $meta)
}

$version = python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
if ($LASTEXITCODE -ne 0) { throw 'Cannot read version' }
$zip = (Resolve-Path 'dist/CallPilot-windows-x64.zip').Path
# Path contains spaces to mirror a real user extraction location.
$root = Join-Path $env:RUNNER_TEMP 'CallPilot portable smoke test'
New-Item -ItemType Directory -Force $root | Out-Null
$env:PYTHONPATH = ''
Remove-Item Env:\AGENTCALL_APP_SUPPORT_DIR -ErrorAction SilentlyContinue
$exe = $null
$service = $null
$appDir = $null

try {
    Expand-Archive -LiteralPath $zip -DestinationPath $root -Force
    $exeItem = Get-ChildItem $root -Recurse -Filter CallPilot.exe | Select-Object -First 1
    if (-not $exeItem) { throw 'CallPilot.exe missing from portable zip' }
    $appDir = $exeItem.DirectoryName
    $exe = $exeItem.FullName
    $dataRoot = Join-Path $appDir 'data-root'
    $localAppData = Join-Path $env:LOCALAPPDATA 'CallPilot'
    if (Test-Path $localAppData) { throw 'Pre-existing %LOCALAPPDATA%\CallPilot; run on a clean runner' }
    # Assert no incumbent listener; never accept someone else's /api/meta.
    if (Get-NetTCPConnection -LocalPort 47100 -State Listen -ErrorAction SilentlyContinue) {
        throw 'Port 47100 already occupied'
    }

    $service = Start-Process $exe -ArgumentList '--service --hardware-selftest' -PassThru
    $versionMatches, $responded = Wait-ForMeta $exe $service $version
    Write-Output "BackendResponded=$responded VersionMatches=$versionMatches"
    if (-not $versionMatches) { throw 'Backend version did not match the package' }
    Run-Checked $exe '--shutdown'
    if (-not $service.WaitForExit(10000) -or $service.ExitCode -ne 0) { throw 'Service did not exit cleanly' }
    # --shutdown exit 0 means all three role locks are acquirable again.

    $envWritten = Test-Path (Join-Path $dataRoot '.env')
    $logsWritten = Test-Path (Join-Path $dataRoot 'logs')
    $localCreated = Test-Path $localAppData
    Write-Output "PortableEnvWritten=$envWritten PortableLogsWritten=$logsWritten LocalAppDataCreated=$localCreated"
    if (-not $envWritten) { throw 'portable .env missing under data-root' }
    if (-not $logsWritten) { throw 'portable logs missing under data-root' }
    if ($localCreated) { throw '%LOCALAPPDATA%\CallPilot was created; portable mode leaked data' }

    $envPath = Join-Path $dataRoot '.env'
    $envHash = (Get-FileHash $envPath).Hash
    # Simulate an upgrade: overwrite program files with the same version, keep data-root.
    Expand-Archive -LiteralPath $zip -DestinationPath $root -Force
    $service = Start-Process $exe -ArgumentList '--service --hardware-selftest' -PassThru
    $upgradeVersionMatches, $upgradeResponded = Wait-ForMeta $exe $service $version
    Run-Checked $exe '--shutdown'
    if (-not $service.WaitForExit(10000) -or $service.ExitCode -ne 0) { throw 'Upgraded service did not exit cleanly' }
    $envPreserved = (Get-FileHash $envPath).Hash -eq $envHash
    Write-Output "UpgradeBackendResponded=$upgradeResponded UpgradeVersionMatches=$upgradeVersionMatches EnvPreserved=$envPreserved"
    if (-not $upgradeVersionMatches) { throw 'Upgraded backend version did not match the package' }
    if (-not $envPreserved) { throw 'Upgrade changed data-root .env' }
} catch {
    if ($appDir) {
        Write-Output 'Portable files remaining at failure (paths only; no file contents):'
        Get-ChildItem $appDir -Recurse -Force | Select-Object FullName, Length, Attributes | Format-Table -AutoSize
    }
    throw
} finally {
    if ($exe -and (Test-Path $exe)) { Run-Checked $exe '--shutdown' }
}
