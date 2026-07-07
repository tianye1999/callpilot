#!/usr/bin/env bash
# 打包 AgentCall.app（薄前端窗口）。用法：bash scripts/build_app.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-"$ROOT/.venv/bin/python"}"
DIST_DIR="$ROOT/dist"
BUILD_DIR="$ROOT/build/pyinstaller"
ROOT_FILE_DIR="$ROOT/build/app"
ROOT_FILE="$ROOT_FILE_DIR/project_root.txt"
APP_PATH="$DIST_DIR/CallPilot.app"

[[ "$(uname -s)" == "Darwin" ]] || { echo "error: 需要 macOS" >&2; exit 2; }
[[ -x "$PYTHON" ]] || { echo "error: 未找到 $PYTHON" >&2; exit 2; }
"$PYTHON" -c "import PyInstaller" 2>/dev/null || {
  echo "error: 缺 PyInstaller（.venv/bin/pip install pyinstaller）" >&2; exit 2; }

mkdir -p "$ROOT_FILE_DIR"
printf '%s\n' "$ROOT" > "$ROOT_FILE"

export AGENTCALL_BUILD_ROOT="$ROOT"
export AGENTCALL_BUILD_ROOT_FILE="$ROOT_FILE"

"$PYTHON" -m PyInstaller --noconfirm --clean \
  --distpath "$DIST_DIR" --workpath "$BUILD_DIR" \
  "$ROOT/packaging/agentcall.spec"

codesign --force --deep --sign - "$APP_PATH"
codesign --verify --deep --strict "$APP_PATH"

echo "APP_PATH=$APP_PATH"
