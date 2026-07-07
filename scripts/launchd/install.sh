#!/usr/bin/env bash
# AgentCall launchd 常驻服务安装脚本（macOS）
#
# 管理两个 LaunchAgent：
#   com.agentcall.bridge —— EC20 USB PTY 桥（scripts/ec20_usb_pty.py）
#   com.agentcall.app    —— AgentCall 主服务（app.py）
#
# 用法：
#   scripts/launchd/install.sh install     安装 plist 并启动两个服务
#   scripts/launchd/install.sh uninstall   停止服务并移除 plist
#   scripts/launchd/install.sh status      查看两个服务的运行状态
#   scripts/launchd/install.sh restart     重启两个服务（先 app 后 bridge 停，先 bridge 后 app 起）
#
# 可通过环境变量覆盖（大写下划线命名，均有默认值）：
#   AGENTCALL_LAUNCHD_DIR    plist 源目录，默认为本脚本所在目录
#   AGENTCALL_LAUNCH_AGENTS  安装目标目录，默认 ~/Library/LaunchAgents

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_SRC_DIR="${AGENTCALL_LAUNCHD_DIR:-$SCRIPT_DIR}"
LAUNCH_AGENTS_DIR="${AGENTCALL_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"

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

cmd_install() {
    mkdir -p "$LAUNCH_AGENTS_DIR"
    for label in "${LABELS[@]}"; do
        local src="$PLIST_SRC_DIR/$label.plist"
        local dst="$LAUNCH_AGENTS_DIR/$label.plist"
        [ -f "$src" ] || die "找不到 plist：$src"
        log "安装 $label -> $dst"
        cp "$src" "$dst"
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
        if launchctl print "$GUI_DOMAIN/$label" >/dev/null 2>&1; then
            local state
            state="$(launchctl print "$GUI_DOMAIN/$label" 2>/dev/null \
                | awk -F'= ' '/state =/ {print $2; exit}')"
            log "$label: 已加载 (state=${state:-unknown})"
        elif launchctl list "$label" >/dev/null 2>&1; then
            log "$label: 已加载 (launchctl list)"
        else
            log "$label: 未加载"
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

usage() {
    grep '^#' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' | sed -n '1,15p'
    exit 1
}

main() {
    local cmd="${1:-}"
    case "$cmd" in
        install)   cmd_install ;;
        uninstall) cmd_uninstall ;;
        status)    cmd_status ;;
        restart)   cmd_restart ;;
        *)         usage ;;
    esac
}

main "$@"
