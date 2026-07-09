#!/usr/bin/env bash
# Build standalone CallPilot.app and CallPilot.dmg for macOS.
# Usage:
#   Unsigned local package:
#     ./packaging/build_installer.sh
#   Signed and notarized release package:
#     CODESIGN_IDENTITY="Developer ID Application: <Name> (<TEAMID>)" \
#       NOTARY_PROFILE=<keychain-profile> ./packaging/build_installer.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-}"
DIST_DIR="$ROOT/dist"
BUILD_DIR="$ROOT/build/pyinstaller"
ARM_VENV="$ROOT/build/packaging-venv-arm64"
APP_PATH="$DIST_DIR/CallPilot.app"
DMG_PATH="$DIST_DIR/CallPilot.dmg"
DMG_STAGE="$ROOT/build/dmg/CallPilot"

die() { printf 'error: %s\n' "$*" >&2; exit 2; }
info() { printf '[build_installer] %s\n' "$*" >&2; }

python_arch() {
    "$1" -c 'import platform; print(platform.machine())' 2>/dev/null || true
}

ensure_build_python() {
    if [[ -n "$PYTHON" ]]; then
        printf '%s\n' "$PYTHON"
        return
    fi
    local repo_python="$ROOT/.venv/bin/python"
    if [[ -x "$repo_python" && "$(python_arch "$repo_python")" == "arm64" ]]; then
        printf '%s\n' "$repo_python"
        return
    fi
    local arm_python="/opt/homebrew/bin/python3"
    [[ -x "$arm_python" ]] || die "arm64 Python not found; install Homebrew Python or set PYTHON to an arm64 interpreter"
    mkdir -p "$ROOT/build"
    if [[ ! -x "$ARM_VENV/bin/python" ]]; then
        info "creating arm64 packaging venv at $ARM_VENV"
        arch -arm64 "$arm_python" -m venv "$ARM_VENV"
    fi
    if ! "$ARM_VENV/bin/python" -c "import PyInstaller, aiohttp, rumps" >/dev/null 2>&1; then
        info "installing packaging dependencies into arm64 venv"
        "$ARM_VENV/bin/python" -m pip install --upgrade pip >/dev/null
        "$ARM_VENV/bin/python" -m pip install -e "$ROOT" pyinstaller >/dev/null
    fi
    printf '%s\n' "$ARM_VENV/bin/python"
}

resolve_ffmpeg() {
    if [[ -n "${FFMPEG_PATH:-}" ]]; then
        printf '%s\n' "$FFMPEG_PATH"
        return
    fi
    local prefix candidate
    if command -v brew >/dev/null 2>&1; then
        prefix="$(arch -arm64 brew --prefix ffmpeg 2>/dev/null || brew --prefix ffmpeg 2>/dev/null || true)"
        candidate="$prefix/bin/ffmpeg"
        [[ -n "$prefix" && -x "$candidate" ]] && { printf '%s\n' "$candidate"; return; }
    fi
    for candidate in \
        /opt/homebrew/opt/ffmpeg/bin/ffmpeg \
        /opt/homebrew/bin/ffmpeg \
        /usr/local/bin/ffmpeg
    do
        [[ -x "$candidate" ]] && { printf '%s\n' "$candidate"; return; }
    done
    command -v ffmpeg 2>/dev/null || true
}

resolve_libusb() {
    if [[ -n "${LIBUSB_PATH:-}" ]]; then
        printf '%s\n' "$LIBUSB_PATH"
        return
    fi
    local prefix candidate
    if command -v brew >/dev/null 2>&1; then
        prefix="$(arch -arm64 brew --prefix libusb 2>/dev/null || brew --prefix libusb 2>/dev/null || true)"
        candidate="$prefix/lib/libusb-1.0.0.dylib"
        [[ -n "$prefix" && -f "$candidate" ]] && { printf '%s\n' "$candidate"; return; }
    fi
    for candidate in \
        /opt/homebrew/opt/libusb/lib/libusb-1.0.0.dylib \
        /usr/local/opt/libusb/lib/libusb-1.0.0.dylib
    do
        [[ -f "$candidate" ]] && { printf '%s\n' "$candidate"; return; }
    done
}

check_arm64() {
    local path="$1" name="$2" archs=""
    if command -v lipo >/dev/null 2>&1; then
        archs="$(lipo -archs "$path" 2>/dev/null || true)"
    fi
    [[ "$archs" == *arm64* ]] || die "$name must contain arm64 slice: $path (${archs:-unknown arch})"
}

verify_artifacts() {
    local dmg_size spctl_output

    [[ -d "$APP_PATH" ]] || die "app missing before verification: $APP_PATH"
    [[ -f "$DMG_PATH" ]] || die "DMG missing before verification: $DMG_PATH"

    info "DMG SHA256:"
    if ! shasum -a 256 "$DMG_PATH"; then
        die "failed to compute DMG SHA256: $DMG_PATH"
    fi

    if ! dmg_size="$(stat -f '%z' "$DMG_PATH")"; then
        die "failed to read DMG size: $DMG_PATH"
    fi
    info "DMG_SIZE_BYTES=$dmg_size"

    if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
        if ! codesign --verify --deep --strict --verbose=2 "$APP_PATH"; then
            die "app codesign verification failed: $APP_PATH"
        fi
        if ! codesign --verify "$DMG_PATH"; then
            die "DMG codesign verification failed: $DMG_PATH"
        fi
    fi

    if [[ -n "${NOTARY_PROFILE:-}" ]]; then
        if ! xcrun stapler validate "$DMG_PATH"; then
            die "DMG staple validation failed: $DMG_PATH"
        fi
        if ! spctl_output="$(spctl -a -vvv -t open --context context:primary-signature "$DMG_PATH" 2>&1)"; then
            [[ -n "$spctl_output" ]] && printf '%s\n' "$spctl_output" >&2
            die "Gatekeeper notarization validation failed: $DMG_PATH"
        fi
        [[ -n "$spctl_output" ]] && printf '%s\n' "$spctl_output" >&2
        if [[ "$spctl_output" != *accepted* || "$spctl_output" != *"Notarized Developer ID"* ]]; then
            die "Gatekeeper validation did not report accepted Notarized Developer ID: $DMG_PATH"
        fi
    fi

    if [[ -n "${NOTARY_PROFILE:-}" ]]; then
        info "verification passed: notarized package (app and DMG signatures verified; staple and Gatekeeper notarization accepted)"
    elif [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
        info "verification passed: signed package (app and DMG signatures verified; notarization skipped)"
    else
        info "verification passed: unsigned package (SHA256 and file size recorded; signing and notarization skipped)"
    fi
}

[[ "$(uname -s)" == "Darwin" ]] || die "macOS is required"
PYTHON="$(ensure_build_python)"
[[ -x "$PYTHON" ]] || die "Python not found: $PYTHON"
[[ "$(python_arch "$PYTHON")" == "arm64" ]] || die "Python must be arm64 for this installer: $PYTHON ($(python_arch "$PYTHON"))"
"$PYTHON" -c "import PyInstaller" 2>/dev/null || die "missing PyInstaller: $PYTHON -m pip install pyinstaller"

FFMPEG_BIN="$(resolve_ffmpeg)"
[[ -n "$FFMPEG_BIN" && -x "$FFMPEG_BIN" ]] || die "ffmpeg not found; set FFMPEG_PATH or install ffmpeg for the build machine"
LIBUSB_DYLIB="$(resolve_libusb)"
[[ -n "$LIBUSB_DYLIB" && -f "$LIBUSB_DYLIB" ]] || die "libusb dylib not found; set LIBUSB_PATH or install libusb for the build machine"
check_arm64 "$FFMPEG_BIN" "ffmpeg"
check_arm64 "$LIBUSB_DYLIB" "libusb"

rm -rf "$APP_PATH" "$DMG_PATH" "$DMG_STAGE"
mkdir -p "$DIST_DIR" "$DMG_STAGE"

export AGENTCALL_BUILD_ROOT="$ROOT"
export AGENTCALL_FFMPEG_PATH="$FFMPEG_BIN"
export AGENTCALL_LIBUSB_PATH="$LIBUSB_DYLIB"

info "building PyInstaller app"
"$PYTHON" -m PyInstaller --noconfirm --clean \
  --distpath "$DIST_DIR" --workpath "$BUILD_DIR" \
  "$ROOT/packaging/agentcall.spec"

[[ -d "$APP_PATH" ]] || die "PyInstaller did not create $APP_PATH"
chmod +x "$APP_PATH/Contents/Resources/bin/$(basename "$FFMPEG_BIN")" 2>/dev/null || true

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
    info "codesigning with identity: $CODESIGN_IDENTITY"
    codesign --force --deep --options runtime --timestamp \
      --entitlements "$ROOT/packaging/entitlements.plist" \
      --sign "$CODESIGN_IDENTITY" "$APP_PATH"
else
    info "CODESIGN_IDENTITY not set; leaving app unsigned"
fi

info "creating DMG"
cp -R "$APP_PATH" "$DMG_STAGE/CallPilot.app"
ln -s /Applications "$DMG_STAGE/Applications"
hdiutil create -volname "CallPilot" -srcfolder "$DMG_STAGE" -ov -format UDZO "$DMG_PATH" >/dev/null

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
    codesign --force --timestamp --sign "$CODESIGN_IDENTITY" "$DMG_PATH"
fi

if [[ -n "${NOTARY_PROFILE:-}" ]]; then
    [[ -n "${CODESIGN_IDENTITY:-}" ]] || die "NOTARY_PROFILE requires CODESIGN_IDENTITY"
    info "submitting DMG for notarization with keychain profile: $NOTARY_PROFILE"
    xcrun notarytool submit "$DMG_PATH" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG_PATH"
else
    info "NOTARY_PROFILE not set; skipping notarization"
fi

verify_artifacts

info "APP_PATH=$APP_PATH"
info "DMG_PATH=$DMG_PATH"
