#!/usr/bin/env bash
# Run as the service user. Only udev/systemd/group installation uses sudo.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LINUX="$ROOT/scripts/linux"
SERVICE=callpilot-edge.service
START_NOW=false

info() { printf '[linux-install] %s\n' "$*"; }
die() { printf '[linux-install] ERROR: %s\n' "$*" >&2; exit 1; }

for arg in "$@"; do
    case "$arg" in
        --now) START_NOW=true ;;
        -h|--help)
            printf 'Usage: bash scripts/linux/install.sh [--now]\n'
            printf 'Install and enable on boot; --now also starts the service. Run without sudo.\n'
            exit 0 ;;
        *) die "Unknown argument: $arg (use --help)" ;;
    esac
done

[ "$(uname -s)" = Linux ] || die "This installer is Linux-only."
[ "$(id -u)" -ne 0 ] || die "Run as an ordinary user, not with sudo; privileged steps request sudo separately."
RUN_USER="$(id -un)"
# Systemd path settings support spaces. Reject expansion/escape characters before sed.
case "$ROOT" in
    *[!a-zA-Z0-9_./\ -]*) die "Repo path must use ASCII letters, digits, spaces, or _ . / - only." ;;
esac
case "$RUN_USER" in
    *[!a-zA-Z0-9_.-]*|'') die "Unsupported service username." ;;
esac

UV_BIN="$(command -v uv || true)"
if [ -z "$UV_BIN" ] && [ -x "$HOME/.local/bin/uv" ]; then
    UV_BIN="$HOME/.local/bin/uv"
fi
if [ -z "$UV_BIN" ]; then
    printf '%s\n' \
        '[linux-install] uv is required to install isolated Python >=3.12 (Bookworm ships 3.11).' \
        'Install uv explicitly as this user, following:' \
        '  https://docs.astral.sh/uv/getting-started/installation/' \
        'For example, after installing pipx: pipx install uv && pipx ensurepath' \
        'For SSH/non-interactive shells: export PATH="$HOME/.local/bin:$PATH"' \
        'Open a new shell, verify uv --version, then re-run this installer.' >&2
    exit 1
fi
info "Using uv: $UV_BIN"
# Sudo-only commands are resolved by sudo's PATH; let sudo report failures.
for cmd in sudo getent sed mktemp ffmpeg; do
    command -v "$cmd" >/dev/null 2>&1 || die "Missing $cmd; see docs/raspberry-pi.md prerequisites."
done
getent group dialout >/dev/null || die "The dialout group is required."

info "Installing user-managed Python 3.13 with uv; system Python is not modified."
"$UV_BIN" python install 3.13
PYTHON_BIN="$("$UV_BIN" python find 3.13)"
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
    || die "Selected interpreter must be Python >=3.12."
cd "$ROOT"
if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
    die "Existing .venv is older than 3.12. Move it aside manually, then re-run (no files were removed)."
fi
info "Reusing scripts/setup.sh (keeps any existing .env)."
PYTHON="$PYTHON_BIN" bash scripts/setup.sh

UNIT_TMP="$(mktemp)"
trap 'rm -f "$UNIT_TMP"' EXIT
sed -e "s|@USER@|$RUN_USER|g" -e "s|@REPO@|$ROOT|g" \
    "$LINUX/systemd/$SERVICE.in" > "$UNIT_TMP"

info "sudo is required now: install udev rules, grant dialout access, and install/enable $SERVICE."
info "Rules are re-triggered for USB serial devices; do this only outside an active call."
# Check command rules, not sudo -v's verifypw policy for mixed password/NOPASSWD entries.
sudo true || die "sudo command authentication failed. Run from an interactive terminal to enter a password, or check this user's sudo command permissions."
for rule in "$LINUX"/udev/*.rules; do
    sudo install -D -m 0644 "$rule" "/etc/udev/rules.d/$(basename "$rule")"
done
sudo usermod -aG dialout "$RUN_USER"
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add --subsystem-match=usb --attr-match=idVendor=1e0e --attr-match=idProduct=9001
sudo udevadm trigger --action=add --subsystem-match=tty --sysname-match='ttyUSB*'
sudo udevadm settle --timeout=10
sudo install -D -m 0644 "$UNIT_TMP" "/etc/systemd/system/$SERVICE"
sudo systemctl daemon-reload
if [ "$START_NOW" = true ]; then
    sudo systemctl enable --now "$SERVICE"
else
    sudo systemctl enable "$SERVICE"
    info "Not starting the service now. Check .env before starting it or rebooting."
fi

info "Installation complete. Merge these Linux-only values into .env (no automatic overwrite):"
sed -n '/^[A-Z]/p' "$LINUX/.env.example"
info "Also configure provider credentials (for example DASHSCOPE_API_KEY). Never publish .env."
info "Log in again for dialout access in your shell; systemd receives the group at next start."
info "An already running service was not restarted; schedule a restart after config changes."
info "Verify: systemctl status $SERVICE; journalctl -u $SERVICE -b"
