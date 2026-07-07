# 打包 CallPilot.exe（薄前端窗口，对应 macOS 的 scripts/build_app.sh）
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts\windows\build_app.ps1
# 产物：dist\CallPilot\CallPilot.exe
# 可用环境变量 PYTHON 覆盖打包用解释器（默认项目 venv）。
#
# 注意：尚未在 Windows 真机验证（开发机为 macOS），欢迎 issue 反馈。

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
# PYTHON 覆盖允许裸命令名（如 python），经 Get-Command 解析成完整路径
$Python = if ($env:PYTHON) {
    (Get-Command $env:PYTHON -ErrorAction Stop).Source
} else { Join-Path $ProjectRoot ".venv\Scripts\python.exe" }
$DistDir     = Join-Path $ProjectRoot "dist"
$BuildDir    = Join-Path $ProjectRoot "build\pyinstaller"
$RootFileDir = Join-Path $ProjectRoot "build\app"
$RootFile    = Join-Path $RootFileDir "project_root.txt"

if (-not (Test-Path $Python)) {
    throw "未找到 $Python —— 请先创建 venv 并安装依赖"
}
# 不能用 2>$null：PS 5.1 在 EAP=Stop 下会把原生命令的 stderr 行转成
# ErrorRecord 直接抛出，探测本身反而先崩。改经 cmd 吞掉 stderr 字节流。
& $env:ComSpec /c "`"$Python`" -c `"import PyInstaller`" 2>nul"
if ($LASTEXITCODE -ne 0) {
    throw "缺 PyInstaller（.venv\Scripts\pip install pyinstaller）"
}

New-Item -ItemType Directory -Force -Path $RootFileDir | Out-Null
# project_root.txt 为单行纯路径（desktop_app 启动时以 UTF-8 回读），
# 必须写无 BOM 的 UTF-8——Windows PowerShell 的 Set-Content 默认编码不可靠
[System.IO.File]::WriteAllText($RootFile, "$ProjectRoot`n",
    (New-Object System.Text.UTF8Encoding $false))

$env:AGENTCALL_BUILD_ROOT = $ProjectRoot
$env:AGENTCALL_BUILD_ROOT_FILE = $RootFile

& $Python -m PyInstaller --noconfirm --clean `
    --distpath $DistDir --workpath $BuildDir `
    (Join-Path $ProjectRoot "packaging\agentcall.spec")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 打包失败（exit=$LASTEXITCODE）"
}

Write-Host "EXE_PATH=$(Join-Path $DistDir 'CallPilot\CallPilot.exe')"
