# WP6 client updates (#140)

## Version and scope

- `pyproject.toml` is the sole version source: `26.10.1`. Intended release tag: `sim-v26.10.1`; this work does not create a tag, build, install, commit, or push.
- Bundle identifier and app name are unchanged. PyInstaller includes the source version file and package release notes. Each process captures its version at startup; a running old process does not reread the replaced bundle and claim to be new.
- `release_notes.json` is the current package's bilingual About text, not downloaded release notes.
- Non-macOS update checking is disabled entirely. The release contract currently exposes only a macOS artifact; offering it on Windows/Linux would be misleading. Version/About remain visible on all platforms.

## Update checks

- Python owns HTTPS requests, with a total timeout of five seconds and redirects disabled. Default: `https://api-beta.bondings.ai/v1/releases/latest?product=physical-sim&platform=macos`.
- `UPDATE_CHECK_ENABLED=true` by default; `UPDATE_CHECK_URL` is configurable. Invalid/non-HTTPS URLs, HTTP errors including the currently expected 404, malformed data and network errors produce no alert.
- Checks run at service startup and every 24 hours. A local shell-open request can wake the checker; repeated startup requests within 60 seconds coalesce. Neither the web page nor tray contacts the release host.
- `/api/meta` exposes the process version, package notes and validated `update`. Version comparison uses numeric segments, with trailing zero segments normalized.
- `data/updates.json` stores dismissed and notified versions atomically, outside `.env`. A different release is not suppressed by a previous dismissal.
- Update events use the WP5 ordinary-tier `feed.info` entry point. Tray first-poll replay applies to update events only, not historical SMS/summary events. A stable `callpilot.update.<version>` native identifier is used. The sender checks busy state and asks the local service to persist the claim after permission checking, immediately before native submission. OS permission/Focus settings still control delivery. A crash or OS error after the durable claim may prevent delivery; claims are at-most-once, not a guarantee of OS presentation.
- Download opens the default browser. The desktop bridge reads the validated URL from local meta and accepts no renderer-supplied URL. Web links allow HTTPS only. Notes use text rendering, not HTML.

## Replacement lifecycle

- On every bundled launch, including a second instance, compare service version/executable, bridge version/actual lock-holder PID, and both launchd plist definitions. Plists include a version environment marker, so replacing at the same path is detectable.
- A bridge writes its runtime marker only after successfully acquiring its existing instance lock. A marker without that live lock is not evidence of a running new bridge.
- Busy calls, queued dialing and unknown call state defer reconciliation. A separate upgrade lock serializes competing launch attempts.
- Once launchd settings are locked, acquire a 60-second idle-maintenance lease through the existing `run_idle_hardware` API. This excludes incoming/outgoing calls without editing shared call files. Failure, normal service cleanup or lease timeout releases the reservation.
- Stop service before bridge; wait for both to unload. Start bridge and verify its version/instance-lock ownership before starting service. Bridge failure prevents starting a potentially mixed service generation. A failed step is reported, not called successful.
- WP6-capable trays hand off by normal UI exit, not by stopping the new services. The previous window closes normally. A stale tray detects that its on-disk bundle changed and starts the new executable instead of reinstalling its cached old generation.

### Legacy migration acceptance gap

A running pre-WP6 service (including 0.6) has no idle-lease endpoint, and its tray has no normal handoff protocol. Checking `/api/meta` alone cannot prevent a call from starting immediately before a restart. This implementation therefore **refuses automatic replacement of a running legacy service** and displays a one-time quit/reopen instruction after the call ends. It does not silently risk a live call or send `open` back to a legacy tray that could reinstall old plists.

This conservative first-migration exception needs owner/Claude acceptance; it is not the same as proving automatic 0.6 -> 26.10.1 replacement. Automatic future replacement is implemented for WP6-capable versions. No real installation or migration was performed during development.

## Claude device acceptance

1. Confirm the legacy first-migration exception above before release. On an authorized test machine, quit the legacy app after its call ends, install, reopen, and confirm all three version displays are `26.10.1`.
2. Build two distinct WP6-capable versions. Replace at the same path and at a moved path, with and without login autostart; verify service, bridge, tray and window converge to the new generation and do not restart repeatedly on reopening.
3. Replace during an active call and with pending queued dialing. Verify no call is ended, no update popup appears, and switching occurs only after idle. Also test an incoming-call attempt during the maintenance transition.
4. Force a bridge lock conflict/bootstrap failure in an isolated environment. Confirm the service is not started over a stale bridge, the UI reports failure, and retry recovers without touching unrelated processes.
5. Use controlled HTTPS release responses: 404, timeout, malformed response, older/equal/newer versions, and HTTP download URLs. Until deployment, the real endpoint's 404 must remain silent.
6. Verify one ordinary system notification per version across tray/service restarts, deferral during calls, disabled notifications, and notification permission denial. Confirm default-browser download and pure-text notes in the actual pywebview runtime.
7. Close the banner, restart the app and service, and confirm the same version stays dismissed while another version appears. Check Chinese/English About and narrow-window layout.

## Quality gates

Run `.venv/bin/pytest -q`, `.venv/bin/ruff check .`, `.venv/bin/mypy`, `.venv/bin/mypy --platform win32`, and `.venv/bin/mypy --platform linux`. Unit/API tests use fake launchctl/native notification senders, temporary settings, and mocked hardware. Browser validation uses a fully intercepted fixture origin, never the running service or release host.

Development verification: full pytest `3060 passed, 3 skipped`; targeted final shell/update/notification/window regression `144 passed`. Ruff and all three mypy platform checks passed (73 source files each); `git diff --check` passed. Playwright fixture checks passed at 1100x780 and 390x844: version/About, text-only release notes, HTTPS link, dismissal after reload, different-release reappearance and call-time suppression. Screenshots: `/tmp/wp6-desktop.png`, `/tmp/wp6-mobile.png`. No live service, device, launchd, cloud deployment or packaged-app acceptance was performed.
