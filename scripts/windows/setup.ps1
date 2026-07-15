# CallPilot 一键环境准备（Windows，对应 macOS/Linux 的 scripts/setup.sh）
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1
#
# 职责：检查 Python ≥3.12 与 ffmpeg → 建 .venv 并安装依赖 → 生成 .env → 打印下一步。
# Windows 无需 USB→PTY 桥：装 Quectel 官方驱动后 AT 口即原生 COM 口（MODEM_PORT=auto 自动扫描）。
# 幂等可重跑；输出英文（面向全球开发者）。可用环境变量 PYTHON 覆盖解释器。
#
# 注意：尚未在 Windows 真机验证（开发机为 macOS），欢迎 issue 反馈。

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $ProjectRoot

function Write-Info([string]$Message) { Write-Host "[setup] $Message" }
function Write-Warn([string]$Message) {
    Write-Host "[setup] WARNING: $Message" -ForegroundColor Yellow
}

# 经 cmd 探测命令：PS 5.1 在 EAP=Stop 下会把原生命令重定向后的 stderr 行转成
# ErrorRecord 直接抛出（同 build_app.ps1 的注释），cmd 的 2>nul 不碰字节。
function Test-CommandExit([string]$CommandLine) {
    & $env:ComSpec /c "$CommandLine >nul 2>nul"
    return ($LASTEXITCODE -eq 0)
}

# ---- 1/4 Python >= 3.12（候选：PYTHON 环境变量 → python → py 启动器）----
$probe = 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'
$candidates = @()
if ($env:PYTHON) { $candidates += "`"$env:PYTHON`"" }
$candidates += @("python", "py -3.14", "py -3.13", "py -3.12", "py -3")

$PyCmd = $null
foreach ($cand in $candidates) {
    if (Test-CommandExit "$cand -c `"$probe`"") { $PyCmd = $cand; break }
}
if (-not $PyCmd) {
    Write-Warn "Python 3.12+ not found (CallPilot requires Python >= 3.12)."
    Write-Host "  Install it:   winget install Python.Python.3.12"
    Write-Host "  Or download:  https://www.python.org/downloads/  (tick 'Add python.exe to PATH')"
    exit 1
}
Write-Info "Python: $PyCmd"

# ---- 2/4 ffmpeg（通话音频必需；缺失不阻塞安装，结尾再次提醒）----
$FfmpegMissing = $false
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ffmpeg) {
    Write-Info "ffmpeg: $($ffmpeg.Source)"
} else {
    $FfmpegMissing = $true
    Write-Warn "ffmpeg not found on PATH (required for call audio)."
    Write-Host "  Install it:   winget install Gyan.FFmpeg"
    Write-Host "  Or download:  https://www.gyan.dev/ffmpeg/builds/  (then add its bin\ to PATH)"
}

# ---- 3/4 venv + 依赖 ----
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    Write-Info "Reusing existing .venv"
} else {
    Write-Info "Creating virtualenv .venv ..."
    & $env:ComSpec /c "$PyCmd -m venv .venv"
    if ($LASTEXITCODE -ne 0) { throw "failed to create .venv (exit=$LASTEXITCODE)" }
}

Write-Info 'Installing dependencies: pip install -e ".[dev]" ...'
# 不硬编码 pip 镜像源（面向全球）；网络慢/超时导致失败时给一行镜像重试建议。
& $VenvPython -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) {
    Write-Warn "pip install failed. If the download was slow or timed out, retry with a PyPI mirror near you, e.g.:"
    Write-Host '  .venv\Scripts\python -m pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple'
    exit 1
}

# ---- 4/4 .env ----
if (Test-Path ".env") {
    Write-Info ".env already exists — keeping it"
} else {
    Copy-Item ".env.example" ".env"
    Write-Info "Created .env from .env.example"
}

Write-Host ""
Write-Info "Setup complete. Next steps:"
Write-Host "  1. Edit .env — default Qwen: set DASHSCOPE_API_KEY;"
Write-Host "     to use OpenAI, set AGENT_PROVIDER=openai and OPENAI_API_KEY;"
Write-Host "     on Windows also set MODEM_PORT=auto and MODEM_AUDIO_MODE=uac"
Write-Host "  2. Install the official Quectel EC20 Windows driver, then plug in the modem"
Write-Host "     (it shows up as native COM ports — no USB bridge needed; see scripts\windows\README.md)"
Write-Host "  3. Start the service:  .venv\Scripts\python app.py   -> http://127.0.0.1:47100"
if ($FfmpegMissing) {
    Write-Warn "Don't forget to install ffmpeg before making calls (see above)."
}
