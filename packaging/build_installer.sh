#!/usr/bin/env bash
# Build standalone CallPilot.app and CallPilot-<version>.dmg for macOS.
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
DMG_STAGE="$ROOT/build/dmg"

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
    # 含 sherpa_onnx 判定：local provider（三段式）要求它进包，否则打包版
    # AGENT_PROVIDER=local 会 ImportError（v0.5.0 发布缺陷的根因）。
    if ! "$ARM_VENV/bin/python" -c "import PyInstaller, aiohttp, rumps, sherpa_onnx, UserNotifications, ServiceManagement, dmgbuild" >/dev/null 2>&1; then
        info "installing packaging dependencies into arm64 venv（含 [local] 三段式依赖）"
        "$ARM_VENV/bin/python" -m pip install --upgrade pip >/dev/null
        # [local] 把 sherpa-onnx 装进打包 venv，供 PyInstaller 收进 bundle；
        # 默认模型由 spec 的 required_files 清单收包，缺失时构建失败。
        "$ARM_VENV/bin/python" -m pip install -e "$ROOT[local]" pyinstaller 'dmgbuild>=1.6,<2' >/dev/null
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
        xcrun stapler validate "$APP_PATH" || die "app staple validation failed"
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
[[ -z "${NOTARY_PROFILE:-}" || -n "${CODESIGN_IDENTITY:-}" ]] || die "NOTARY_PROFILE requires CODESIGN_IDENTITY"
if [[ "${RELEASE:-0}" == "1" ]]; then
    [[ -n "${CODESIGN_IDENTITY:-}" && -n "${NOTARY_PROFILE:-}" ]] || die "RELEASE=1 requires signing and notarization"
fi
PYTHON="$(ensure_build_python)"
[[ -x "$PYTHON" ]] || die "Python not found: $PYTHON"
[[ "$(python_arch "$PYTHON")" == "arm64" ]] || die "Python must be arm64 for this installer: $PYTHON ($(python_arch "$PYTHON"))"
"$PYTHON" -c "import PyInstaller" 2>/dev/null || die "missing PyInstaller: $PYTHON -m pip install pyinstaller"
"$PYTHON" -c "import ServiceManagement, dmgbuild" 2>/dev/null || die "install project dependencies and dmgbuild into $PYTHON"
VERSION="$("$PYTHON" -c 'import sys,tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "$ROOT/pyproject.toml")"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid package version"
DMG_PATH="$DIST_DIR/CallPilot-$VERSION.dmg"

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
"$PYTHON" "$ROOT/packaging/write_build_info.py"
"$PYTHON" -m PyInstaller --noconfirm --clean \
  --distpath "$DIST_DIR" --workpath "$BUILD_DIR" \
  "$ROOT/packaging/agentcall.spec"

[[ -d "$APP_PATH" ]] || die "PyInstaller did not create $APP_PATH"
chmod +x "$APP_PATH/Contents/Resources/bin/$(basename "$FFMPEG_BIN")" 2>/dev/null || true
"$PYTHON" "$ROOT/packaging/verify_bundle.py" "$APP_PATH"

# Sign inside-out, one Mach-O at a time with retries: a single failed timestamp
# request (flaky route to Apple's TSA, e.g. over VPN) aborts `codesign --deep`
# for the whole bundle, and a crashed codesign leaves `.cstemp` files behind.
sign_one() {
    local target="$1" attempt
    for attempt in 1 2 3 4 5; do
        if codesign --force --options runtime --timestamp \
             --entitlements "$ROOT/packaging/entitlements.plist" \
             --sign "$CODESIGN_IDENTITY" "$target" 2>"$DMG_STAGE/sign.err"; then
            return 0
        fi
        printf '[sign] retry %s/5 %s: %s\n' "$attempt" "${target#$APP_PATH/}" "$(tail -1 "$DMG_STAGE/sign.err")" >&2
        sleep $((attempt * 2))
    done
    cat "$DMG_STAGE/sign.err" >&2
    die "codesign failed 5 times: $target"
}

sign_inside_out() {
    local macho_list="$DMG_STAGE/macho.txt" total
    mkdir -p "$DMG_STAGE"
    find "$APP_PATH" -name "*.cstemp" -delete
    "$PYTHON" - "$APP_PATH" > "$macho_list" <<'MACHO'
import os, sys
magics = {b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xce'}
app = sys.argv[1]
main = os.path.join(app, "Contents", "MacOS")
found = []
for root, _dirs, files in os.walk(app):
    for name in files:
        path = os.path.join(root, name)
        if os.path.islink(path) or os.path.dirname(path) == main:
            continue
        try:
            with open(path, "rb") as handle:
                if handle.read(4) in magics:
                    found.append(path)
        except OSError:
            pass
for path in sorted(found, key=lambda p: (-p.count(os.sep), p)):
    print(path)
MACHO
    total=$(wc -l < "$macho_list" | tr -d ' ')
    info "signing $total nested Mach-O files inside-out"
    while IFS= read -r item; do sign_one "$item"; done < "$macho_list"
    while IFS= read -r bundle; do sign_one "$bundle"; done < <(find "$APP_PATH/Contents" -type d \( -name "*.framework" -o -name "*.bundle" -o -name "*.app" -o -name "*.xpc" \) | awk '{print gsub("/","/")" "$0}' | sort -rn | cut -d" " -f2-)
    local exe
    for exe in "$APP_PATH/Contents/MacOS/"*; do [[ -L "$exe" ]] || sign_one "$exe"; done
    sign_one "$APP_PATH"
    codesign --verify --deep --strict --verbose=2 "$APP_PATH" || die "deep verify failed after inside-out signing"
    info "inside-out signing complete"
}

# Apple's notary service intermittently drops the S3-accelerated upload and
# occasionally times out while the request is actually Accepted: retry, and
# judge by the recorded status rather than the exit code.
notarize_with_retry() {
    local target="$1" attempt
    mkdir -p "$DMG_STAGE"
    for attempt in 1 2 3; do
        xcrun notarytool submit --no-s3-acceleration "$target" --keychain-profile "$NOTARY_PROFILE" --wait >"$DMG_STAGE/notary.out" 2>&1 || true
        if grep -q "status: Accepted" "$DMG_STAGE/notary.out"; then
            return 0
        fi
        printf '[notary] retry %s/3 for %s: %s\n' "$attempt" "$(basename "$target")" "$(grep -E 'Error|status:' "$DMG_STAGE/notary.out" | tail -1)" >&2
        sleep $((attempt * 15))
    done
    cat "$DMG_STAGE/notary.out" >&2
    die "notarization failed 3 times: $target"
}

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
    info "codesigning with identity: $CODESIGN_IDENTITY"
    sign_inside_out
else
    info "CODESIGN_IDENTITY not set; leaving app unsigned"
fi

# This entry point imports and checks models only: no services, USB or login items.
info "running packaged --selftest"
"$APP_PATH/Contents/MacOS/CallPilot" --selftest

if [[ -n "${NOTARY_PROFILE:-}" ]]; then
    info "notarizing app using maintainer-provided keychain profile"
    xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null || \
      die "keychain profile unavailable; configure with notarytool store-credentials locally"
    ditto -c -k --keepParent "$APP_PATH" "$DMG_STAGE/CallPilot-notary.zip"
    notarize_with_retry "$DMG_STAGE/CallPilot-notary.zip"
    xcrun stapler staple "$APP_PATH"
    xcrun stapler validate "$APP_PATH"
fi

info "creating DMG"
sips -z 420 660 "$ROOT/packaging/dmg-background.png" --out "$DMG_STAGE/background.png" >/dev/null
export CALLPILOT_DMG_APP="$APP_PATH"
export CALLPILOT_DMG_BACKGROUND="$DMG_STAGE/background.png"
# dmgbuild owns hdiutil create/attach/detach and writes .DS_Store without Finder automation.
"$PYTHON" -m dmgbuild -s "$ROOT/packaging/dmg-settings.py" "KinyAI-SIM助手 $VERSION" "$DMG_PATH"

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
    codesign --force --timestamp --sign "$CODESIGN_IDENTITY" "$DMG_PATH"
fi

if [[ -n "${NOTARY_PROFILE:-}" ]]; then
    info "submitting DMG for notarization with keychain profile: $NOTARY_PROFILE"
    notarize_with_retry "$DMG_PATH"
    xcrun stapler staple "$DMG_PATH"
else
    info "NOTARY_PROFILE not set; skipping notarization"
fi

verify_artifacts

info "APP_PATH=$APP_PATH"
info "DMG_PATH=$DMG_PATH"
