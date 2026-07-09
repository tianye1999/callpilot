#!/usr/bin/env bash
# AgentCall launchd 常驻服务安装脚本（macOS）
#
# 管理两个 LaunchAgent：
#   com.agentcall.bridge —— EC20 USB PTY 桥（scripts/ec20_usb_pty.py）
#   com.agentcall.app    —— AgentCall 主服务（app.py）
#
# 用法：
#   scripts/launchd/install.sh install [dev|app]  安装 plist 并启动两个服务
#     dev（默认）：从当前仓库动态生成 python app.py / EC20 bridge 开发版 plist
#     app：复制本目录静态 plist，供打包版 CallPilot.app 使用
#   scripts/launchd/install.sh uninstall          停止服务并移除 plist
#   scripts/launchd/install.sh status             查看两个服务的运行状态与当前 plist 形态
#   scripts/launchd/install.sh restart            重启两个服务（先 app 后 bridge 停，先 bridge 后 app 起）
#
# 可通过环境变量覆盖（大写下划线命名，均有默认值）：
#   AGENTCALL_LAUNCHD_DIR    plist 源目录，默认为本脚本所在目录
#   AGENTCALL_LAUNCH_AGENTS  安装目标目录，默认 ~/Library/LaunchAgents

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_SRC_DIR="${AGENTCALL_LAUNCHD_DIR:-$SCRIPT_DIR}"
LAUNCH_AGENTS_DIR="${AGENTCALL_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"
DEV_PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"

resolve_repo_root() {
    local path="$SCRIPT_DIR/../.."
    if command -v realpath >/dev/null 2>&1; then
        realpath "$path"
    else
        (cd "$path" && pwd -P)
    fi
}

REPO_ROOT="$(resolve_repo_root)"

# 启动顺序：桥优先；app 晚于桥启动没关系（modem 串口重连机制已兜底）
LABELS=("com.agentcall.bridge" "com.agentcall.app")
GUI_DOMAIN="gui/$(id -u)"

log() { printf '[install.sh] %s\n' "$*"; }
die() { printf '[install.sh] ERROR: %s\n' "$*" >&2; exit 1; }

# launchctl bootstrap 是新接口（macOS 10.11+），老系统回退 load -w/unload
has_bootstrap() {
    launchctl help 2>&1 | grep -q "bootstrap"
}

load_agent() {
    local plist="$1"
    if has_bootstrap; then
        launchctl bootstrap "$GUI_DOMAIN" "$plist" \
            || log "bootstrap 失败（可能已加载）：$plist"
    else
        launchctl load -w "$plist" \
            || log "load 失败（可能已加载）：$plist"
    fi
}

unload_agent() {
    local label="$1" plist="$2"
    if has_bootstrap; then
        launchctl bootout "$GUI_DOMAIN/$label" 2>/dev/null \
            || log "bootout 跳过（未加载）：$label"
    else
        launchctl unload "$plist" 2>/dev/null \
            || log "unload 跳过（未加载）：$label"
    fi
}

ensure_dev_prereqs() {
    [ -d "$REPO_ROOT/.venv" ] \
        || die "找不到开发版 venv：$REPO_ROOT/.venv；请先创建 .venv 后再 install dev"
    [ -x "$REPO_ROOT/.venv/bin/python" ] \
        || die "找不到可执行 Python：$REPO_ROOT/.venv/bin/python；请先修复 .venv"
    [ -f "$REPO_ROOT/app.py" ] \
        || die "找不到开发版入口：$REPO_ROOT/app.py"
    [ -f "$REPO_ROOT/scripts/ec20_usb_pty.py" ] \
        || die "找不到 EC20 bridge：$REPO_ROOT/scripts/ec20_usb_pty.py"
    mkdir -p "$REPO_ROOT/data"
}

ensure_app_prereqs() {
    local label src
    for label in "${LABELS[@]}"; do
        src="$PLIST_SRC_DIR/$label.plist"
        [ -f "$src" ] || die "找不到 plist：$src"
    done
}

xml_escape() {
    local value="$1"
    value="${value//&/&amp;}"
    value="${value//</&lt;}"
    value="${value//>/&gt;}"
    printf '%s' "$value"
}

write_dev_app_plist() {
    local dst="$1"
    local repo python app_py env_file stdout stderr path_value
    repo="$(xml_escape "$REPO_ROOT")"
    python="$(xml_escape "$REPO_ROOT/.venv/bin/python")"
    app_py="$(xml_escape "$REPO_ROOT/app.py")"
    env_file="$(xml_escape "$REPO_ROOT/.env")"
    stdout="$(xml_escape "$REPO_ROOT/data/launchd-app.out.log")"
    stderr="$(xml_escape "$REPO_ROOT/data/launchd-app.err.log")"
    path_value="$(xml_escape "$DEV_PATH")"
    cat > "$dst" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.agentcall.app</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/caffeinate</string>
        <string>-s</string>
        <string>$python</string>
        <string>$app_py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$path_value</string>
        <key>AGENTCALL_ENV_FILE</key>
        <string>$env_file</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>$repo</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$stdout</string>
    <key>StandardErrorPath</key>
    <string>$stderr</string>
    <key>LimitLoadToSessionType</key>
    <string>Aqua</string>
</dict>
</plist>
EOF
}

write_dev_bridge_plist() {
    local dst="$1"
    local repo python bridge_py env_file stdout stderr path_value
    repo="$(xml_escape "$REPO_ROOT")"
    python="$(xml_escape "$REPO_ROOT/.venv/bin/python")"
    bridge_py="$(xml_escape "$REPO_ROOT/scripts/ec20_usb_pty.py")"
    env_file="$(xml_escape "$REPO_ROOT/.env")"
    stdout="$(xml_escape "$REPO_ROOT/data/launchd-bridge.out.log")"
    stderr="$(xml_escape "$REPO_ROOT/data/launchd-bridge.err.log")"
    path_value="$(xml_escape "$DEV_PATH")"
    cat > "$dst" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.agentcall.bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/caffeinate</string>
        <string>-s</string>
        <string>$python</string>
        <string>$bridge_py</string>
        <string>--map</string>
        <string>2:/tmp/ec20-at</string>
        <string>--map</string>
        <string>1:/tmp/ec20-nmea</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$path_value</string>
        <key>AGENTCALL_ENV_FILE</key>
        <string>$env_file</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>$repo</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$stdout</string>
    <key>StandardErrorPath</key>
    <string>$stderr</string>
    <key>LimitLoadToSessionType</key>
    <string>Aqua</string>
</dict>
</plist>
EOF
}

write_dev_plist() {
    local label="$1" dst="$2"
    case "$label" in
        com.agentcall.app)    write_dev_app_plist "$dst" ;;
        com.agentcall.bridge) write_dev_bridge_plist "$dst" ;;
        *)                   die "未知 label：$label" ;;
    esac
}

install_one_plist() {
    local shape="$1" label="$2" dst="$3"
    case "$shape" in
        dev)
            write_dev_plist "$label" "$dst"
            ;;
        app)
            cp "$PLIST_SRC_DIR/$label.plist" "$dst"
            ;;
        *)
            die "未知安装形态：$shape（应为 dev 或 app）"
            ;;
    esac
}

cmd_install() {
    local shape="${1:-dev}"
    case "$shape" in
        dev) ensure_dev_prereqs ;;
        app) ensure_app_prereqs ;;
        *)   die "未知安装形态：$shape（应为 dev 或 app）" ;;
    esac

    mkdir -p "$LAUNCH_AGENTS_DIR"
    for label in "${LABELS[@]}"; do
        local dst="$LAUNCH_AGENTS_DIR/$label.plist"
        log "安装 $label ($shape) -> $dst"
        unload_agent "$label" "$dst"
        install_one_plist "$shape" "$label" "$dst"
        load_agent "$dst"
    done
    log "安装完成。用 '$0 status' 查看状态。"
}

cmd_uninstall() {
    # 卸载顺序与启动相反：先 app 后 bridge
    local i
    for (( i=${#LABELS[@]}-1; i>=0; i-- )); do
        local label="${LABELS[$i]}"
        local dst="$LAUNCH_AGENTS_DIR/$label.plist"
        log "卸载 $label"
        unload_agent "$label" "$dst"
        if [ -f "$dst" ]; then
            rm "$dst"
            log "已移除 $dst"
        fi
    done
    log "卸载完成。"
}

cmd_status() {
    for label in "${LABELS[@]}"; do
        local dst="$LAUNCH_AGENTS_DIR/$label.plist"
        local summary
        summary="$(plist_summary "$dst")"
        if launchctl print "$GUI_DOMAIN/$label" >/dev/null 2>&1; then
            local state
            state="$(launchctl print "$GUI_DOMAIN/$label" 2>/dev/null \
                | awk -F'= ' '/state =/ {print $2; exit}')"
            log "$label: 已加载 (state=${state:-unknown}); $summary"
        elif launchctl list "$label" >/dev/null 2>&1; then
            log "$label: 已加载 (launchctl list); $summary"
        else
            log "$label: 未加载; $summary"
        fi
    done
}

cmd_restart() {
    for label in "${LABELS[@]}"; do
        local dst="$LAUNCH_AGENTS_DIR/$label.plist"
        [ -f "$dst" ] || die "尚未安装（缺 $dst），请先执行 '$0 install'"
    done
    cmd_uninstall_keep_plist
    for label in "${LABELS[@]}"; do
        load_agent "$LAUNCH_AGENTS_DIR/$label.plist"
    done
    log "重启完成。"
}

# restart 专用：只卸载不删 plist
cmd_uninstall_keep_plist() {
    local i
    for (( i=${#LABELS[@]}-1; i>=0; i-- )); do
        local label="${LABELS[$i]}"
        unload_agent "$label" "$LAUNCH_AGENTS_DIR/$label.plist"
    done
}

plist_shape() {
    local args="$1"
    if [[ "$args" == *"/.venv/bin/python"* ]] \
        && [[ "$args" == *"/app.py"* || "$args" == *"/scripts/ec20_usb_pty.py"* ]]; then
        printf 'dev'
    elif [[ "$args" == *".app/Contents/MacOS/CallPilot"* ]]; then
        printf 'app'
    elif [[ "$args" == *"--service"* || "$args" == *"--bridge"* ]]; then
        printf 'app'
    else
        printf 'unknown'
    fi
}

program_arguments() {
    local plist="$1"
    awk '
        /<key>ProgramArguments<\/key>/ { in_args=1; next }
        in_args && /<\/array>/ { exit }
        in_args && /<string>/ {
            line=$0
            sub(/^[[:space:]]*<string>/, "", line)
            sub(/<\/string>[[:space:]]*$/, "", line)
            printf "%s%s", sep, line
            sep=" "
        }
    ' "$plist"
}

plist_summary() {
    local plist="$1"
    if [ ! -f "$plist" ]; then
        printf 'plist=missing'
        return
    fi

    local args shape
    args="$(program_arguments "$plist")"
    shape="$(plist_shape "$args")"
    if [ -z "$args" ]; then
        args="(missing ProgramArguments)"
    fi
    printf 'plist=%s ProgramArguments=%s' "$shape" "$args"
}

usage() {
    sed -n '2,18s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"
    exit 1
}

main() {
    local cmd="${1:-}"
    case "$cmd" in
        install)
            [ "$#" -le 2 ] || usage
            cmd_install "${2:-dev}"
            ;;
        uninstall) cmd_uninstall ;;
        status)    cmd_status ;;
        restart)   cmd_restart ;;
        *)         usage ;;
    esac
}

main "$@"
