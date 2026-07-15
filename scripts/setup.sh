#!/usr/bin/env bash
# CallPilot 一键环境准备（macOS / Linux）。用法：bash scripts/setup.sh
#
# 职责：检查 Python ≥3.12 与 ffmpeg → 建 .venv 并安装依赖 → 生成 .env → 打印下一步。
# 幂等可重跑：已完成的步骤自动跳过。输出英文（面向全球开发者）。
# 可用环境变量 PYTHON 指定解释器（如 PYTHON=python3.12 bash scripts/setup.sh）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OS="$(uname -s)"

info() { printf '[setup] %s\n' "$*"; }
warn() { printf '[setup] WARNING: %s\n' "$*" >&2; }

# ---- 1/4 Python >= 3.12 ----
version_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
        >/dev/null 2>&1
}

PY=""
for cand in "${PYTHON:-}" python3 python3.14 python3.13 python3.12; do
    [ -n "$cand" ] || continue
    command -v "$cand" >/dev/null 2>&1 || continue
    if version_ok "$cand"; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
    warn "Python 3.12+ not found (CallPilot requires Python >= 3.12)."
    if [ "$OS" = "Darwin" ]; then
        echo "  Install it:   brew install python@3.12" >&2
    else
        echo "  Install it:   sudo apt install python3.12 python3.12-venv   (or your distro's equivalent)" >&2
    fi
    echo "  Or download:  https://www.python.org/downloads/" >&2
    echo "  Then re-run:  PYTHON=python3.12 bash scripts/setup.sh" >&2
    exit 1
fi
info "Python: $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"

# ---- 2/4 ffmpeg（通话音频必需；缺失不阻塞安装，但结尾再次提醒）----
FFMPEG_MISSING=""
if command -v ffmpeg >/dev/null 2>&1; then
    info "ffmpeg: $(command -v ffmpeg)"
else
    FFMPEG_MISSING=1
    warn "ffmpeg not found on PATH (required for call audio)."
    if [ "$OS" = "Darwin" ]; then
        echo "  Install it:  brew install ffmpeg" >&2
    else
        echo "  Install it:  sudo apt install ffmpeg   (or your distro's equivalent)" >&2
    fi
fi

# ---- 2.5 libusb（macOS 的 USB→PTY 桥必需；pyusb 只是绑定，系统库要单装）----
if [ "$OS" = "Darwin" ]; then
    if ! { [ -e /usr/local/lib/libusb-1.0.dylib ] || [ -e /opt/homebrew/lib/libusb-1.0.dylib ]; }; then
        warn "libusb not found (the USB bridge needs it)."
        echo "  Install it:  brew install libusb" >&2
    fi
fi

# ---- 3/4 venv + 依赖 ----
if [ -x .venv/bin/python ]; then
    # 复用前复查解释器版本：陈旧的 3.11- venv 会让 pip 报难懂的
    # "requires a different Python"，不如在这里直接指路重建。
    if ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
        err "Existing .venv uses Python < 3.12. Remove it and re-run:  rm -rf .venv && bash scripts/setup.sh"
        exit 1
    fi
    info "Reusing existing .venv"
else
    info "Creating virtualenv .venv ..."
    "$PY" -m venv .venv
fi

info 'Installing dependencies: pip install -e ".[dev]" ...'
# 不硬编码 pip 镜像源（面向全球）；网络慢/超时导致失败时给一行镜像重试建议。
if ! .venv/bin/pip install -e ".[dev]"; then
    warn 'pip install failed. If the download was slow or timed out, retry with a PyPI mirror near you, e.g.:'
    echo '  .venv/bin/pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple' >&2
    exit 1
fi

# ---- 4/4 .env ----
if [ -f .env ]; then
    info ".env already exists — keeping it"
else
    cp .env.example .env
    info "Created .env from .env.example"
fi

echo ""
info "Setup complete. Next steps:"
echo "  1. Edit .env — default OpenAI: set OPENAI_API_KEY; to use Qwen, set AGENT_PROVIDER=qwen and DASHSCOPE_API_KEY"
if [ "$OS" = "Darwin" ]; then
    echo "  2. Plug in the EC20, then start the USB bridge:  .venv/bin/python scripts/ec20_usb_pty.py --map 2:/tmp/ec20-at"
else
    echo "  2. Plug in the EC20, then set MODEM_PORT in .env to its AT serial port (e.g. /dev/ttyUSB2)"
fi
echo "  3. Start the service:  .venv/bin/python app.py   -> http://127.0.0.1:47100"
if [ -n "$FFMPEG_MISSING" ]; then
    warn "Don't forget to install ffmpeg before making calls (see above)."
fi
