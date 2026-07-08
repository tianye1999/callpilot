# CallPilot macOS Packaging

Build the unsigned standalone installer on an Apple Silicon Mac:

```bash
bash packaging/build_installer.sh
```

Outputs:

- `dist/CallPilot.app`
- `dist/CallPilot.dmg`

The build bundles the PyInstaller Python runtime, CallPilot code, menu bar icons,
the build-machine `ffmpeg` executable, and `libusb-1.0.0.dylib`. Set
`FFMPEG_PATH` or `LIBUSB_PATH` to override discovery. The bundled `ffmpeg`
binary is redistributed under the license terms of the ffmpeg build you provide;
Homebrew builds install their license metadata under the Homebrew prefix.

Signing and notarization are optional:

```bash
CODESIGN_IDENTITY="Developer ID Application: Example (TEAMID)" \
NOTARY_PROFILE="notarytool-profile" \
bash packaging/build_installer.sh
```

If `CODESIGN_IDENTITY` is unset, the app and DMG are left unsigned.

## First Open

Unsigned builds are blocked by Gatekeeper on first launch. Install from the DMG,
then right-click `CallPilot.app` in `/Applications` and choose **Open**. Confirm
the prompt once; later launches can use the normal double-click/open flow.

On first run, the menu bar app writes per-user launchd agents to
`~/Library/LaunchAgents/` using the app's current install path. Runtime state,
logs, recordings, and `.env` live under:

```text
~/Library/Application Support/CallPilot/
```

Moving the app and opening it again regenerates the launchd plists for the new
bundle path. The menu contains **卸载常驻** to boot out and remove the background
launchd agents.
