#!/usr/bin/env bash
# Explicit opt-in, run as the Edge service user, only outside every phone call.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
die() { printf '[bluetooth-setup] ERROR: %s\n' "$*" >&2; exit 1; }
[ "$(uname -s)" = Linux ] || die "Linux only."
[ "$(id -u)" -ne 0 ] || die "Run as the Edge user, not root."
[ "${1:-}" = --idle-confirmed ] || die "Confirm the phone has no calls: pass --idle-confirmed."
command -v wireplumber >/dev/null || die "Install WirePlumber 0.4.x first."
case "$(wireplumber --version)" in
    *0.4.*) ;;
    *) die "This configuration is for WirePlumber 0.4.x only; 0.5 needs a different format." ;;
esac
RUN_USER="$(id -un)"
case "$RUN_USER" in *[!a-zA-Z0-9_.-]*|'') die "Unsupported service username." ;; esac
UV_BIN="$(command -v uv || true)"
[ -n "$UV_BIN" ] || UV_BIN="$HOME/.local/bin/uv"
[ -x "$UV_BIN" ] || die "uv is required."
[ -x "$ROOT/.venv/bin/python" ] || die "Run scripts/setup.sh first."
printf '[bluetooth-setup] Installing optional venv dependency and oFono; .env is unchanged.\n'
"$UV_BIN" pip install --python "$ROOT/.venv/bin/python" -e "$ROOT[bluetooth]"
sudo apt-get install -y ofono
install -d -m 0755 "$HOME/.config/wireplumber/bluetooth.lua.d"
install -m 0644 "$ROOT/scripts/linux/wireplumber/51-callpilot-bluetooth.lua" \
    "$HOME/.config/wireplumber/bluetooth.lua.d/51-callpilot-bluetooth.lua"
# oFono's distribution policy may otherwise allow only root. Grant this user,
# not all local users. This file contains no phone identity or credentials.
POLICY="$(mktemp)"
trap 'rm -f "$POLICY"' EXIT
printf '%s\n' '<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"' \
    ' "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">' \
    "<busconfig><policy user=\"$RUN_USER\">" \
    '<allow send_destination="org.ofono"/>' \
    '<allow send_destination="org.bluez"/>' \
    '</policy></busconfig>' > "$POLICY"
sudo install -m 0644 "$POLICY" /etc/dbus-1/system.d/callpilot-ofono.conf
sudo systemctl reload dbus
sudo systemctl enable --now bluetooth ofono
systemctl --user restart wireplumber || die "User WirePlumber restart failed; log in as the Edge user and retry."
printf '[bluetooth-setup] Pair/trust the phone, configure local .env, then restart Edge. See docs/raspberry-pi.md.\n'
