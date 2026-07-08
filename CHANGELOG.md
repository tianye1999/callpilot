# Changelog

All notable changes to CallPilot are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/) (pre-1.0: minor bumps may break).

## [0.3.0] — 2026-07-09

### Added

- **One-click macOS installer**: `packaging/build_installer.sh` produces a
  self-contained `CallPilot.dmg` — bundled Python runtime, ffmpeg and libusb,
  no Python/venv/Homebrew needed. First launch installs the launchd background
  services automatically (and re-points them if the app is moved); the tray
  menu gains an "Uninstall background services" item. Runtime data moves to
  `~/Library/Application Support/CallPilot/`. Unsigned for now (right-click →
  Open on first launch); codesign/notarization hooks are in place.
- **First-run setup wizard**: detects the modem (PyUSB/system_profiler on
  macOS, serial VID scan elsewhere), validates your API key online
  (distinguishes "invalid key" from "network unreachable"), sets owner name /
  persona / language / voice, and can send a test SMS — no manual `.env`
  editing. Missing credentials no longer crash the service: the web UI comes
  up and guides you instead.
- **Live listen in the browser**: hear both call directions (AI + caller) in
  real time via WebSocket + Web Audio — works even where native audio is
  broken. Call recordings are also playable per-call from History (caller
  track auto-amplified).
- **LLM wrap-up judge**: a cheap text model watches the transcript and decides
  "keep going vs wrap up" — ends calls that are stuck in circles, keeps
  waiting when the other side is still looking something up, and only counts
  the goal as reached when the substantive result was actually given. Replaces
  keyword heuristics entirely.
- **Repeat suppression**: when an IVR broadcast forces the model to respond
  over and over, near-identical replies are detected by text similarity and
  dropped before they reach the line (`REPEAT_SUPPRESS_SIMILARITY`, 0 to
  disable).
- **Tool safety**: shared SMS rate limit across the AI tool and the web API
  (`SMS_RATE_LIMIT_PER_HOUR`), an off switch for the OTP-reading tool
  (`TOOL_QUERY_CODE_ENABLED`), and desensitized audit logging for every tool
  call (message lengths, not contents; hit/miss, not the code).
- **Clear recordings**: delete a single call or all call records from History
  (in-progress calls are protected); SMS sending restricted to numbers you've
  actually interacted with.
- **Modem primitives examples**: `examples/modem/` — minimal standalone demos
  for raw AT, device probe, dial, answer, SMS send/receive and DTMF.

### Changed

- **Prompts rewritten scenario-style** (describe the situation, don't
  enumerate rules): shorter opening line, introduce yourself once, speak
  short menu keywords to voice menus, say a complete goodbye *before* calling
  the hang-up tool, and politely steer back to the task until the substantive
  result is in hand.
- Doubao provider is now labeled **experimental** in Settings and docs
  (outbound calls may be silent).
- Frontend hardened against XSS: no HTML-injection APIs; all user-controlled
  content rendered via `textContent` (guarded by a static test).

### Fixed

- **Incoming-SMS race**: a `+CMTI` notification arriving while any AT command
  response was being read was silently discarded; all command responses now
  scan for URCs.
- Installer first-run race: bootstrapping launchd agents right after removing
  old ones could silently fail; now waits for unload and retries with backoff,
  reporting failures via tray notification.

### Engineering

- `ruff` (E/F/W/I) and `mypy` gates wired into CI, warning-clean at
  introduction; zero behavior change.

## [0.2.0] — 2026-07-08

### Added

- **Windows 10/11 support** (code-complete, awaiting hardware reports).
  Windows uses the official Quectel driver's native COM port — no USB bridge
  needed. Ships with automatic port detection (`MODEM_PORT=auto` scans for the
  Quectel vendor ID), a PortAudio/WASAPI audio path, a scheduled-task
  installer (`scripts/windows/install.ps1`), and `CallPilot.exe` packaging.
- **OpenAI Realtime provider** — new in this release: use OpenAI's realtime
  speech-to-speech API as the AI brain, alongside Alibaba Qwen Omni (default)
  and ByteDance Doubao.
- **Three-platform CI**: every change now runs the full zero-hardware test
  suite on Ubuntu, macOS, and Windows.
- **Language menu**: UI language switching moved to a globe-icon dropdown,
  making room for more languages beyond English and Chinese.

### Fixed

- **Five P0 correctness bugs** caught in a three-way code review, including:
  dashboard occasionally missing live events (broadcast tasks could be
  garbage-collected mid-flight), a hangup race with call-status polling, a
  delayed hangup scheduled during one call cutting off the *next* call (calls
  now carry a generation number), and `RECORDING_ENABLED=on` being interpreted
  differently in two places.
- **Zombie sessions**: if the modem's serial link dies mid-call, the session
  now ends itself within seconds instead of rejecting all new calls until a
  manual hangup.
- **launchd PATH**: the launchd units now set `PATH` explicitly, so `ffmpeg`
  is found and call audio works when CallPilot starts at login.
- **Dial input validation**: invalid phone numbers are rejected up front
  instead of tying up the session for a 45-second timeout, and dialing with no
  modem connected reports a clear error instead of pretending a call was
  placed.

### Changed

- **Call-session core refactored** for contributors: prompt building and
  in-call AI tools split into their own modules, outbound call tasks passed
  explicitly instead of through the environment (batch dialing no longer
  rewrites `.env` on every call), and web API error handling unified. No
  intended behavior changes, backed by the full offline test suite.
- **Configuration consolidated into a single registry**: every setting's
  default value lives in one place, `.env.example` documents all editable
  settings, and a regression test keeps the two from drifting apart.
- **Platform differences centralized** in one module — per-OS defaults and
  paths are no longer scattered through the code.
- **macOS app is now a menu-bar tray** (phone-handset icon — green when the
  service is running, grey when stopped — with an open-console / restart-service
  / quit menu) instead of a standalone desktop window.
- **Outbound wind-down polish**: on reaching the goal, the AI now speaks a full
  goodbye *before* it invokes hang-up, and stays silent during the hang-up delay
  (no stray "call ended" line played to the other side).

## [0.1.0] — 2026-07-08 · Developer Preview

First public release. Verified end-to-end on real hardware: a Quectel EC20
(`EC20CEFAGR08A03M4G`) on macOS, talking to a carrier's `10000` IVR in both
directions and exchanging SMS.

### Telephony (Quectel EC20/EG25)

- Auto-answer incoming calls (`RING → ATA`), hangup detection via CLCC polling.
- Outbound dialing: single call with an optional per-call task/topic (blank
  reuses the last one), and batch dialing with a queue.
- Optional dial whitelist (`DIAL_WHITELIST`).
- SMS send/receive with Chinese UCS2 encoding.
- DTMF keypad: AI tool (`send_dtmf`, used autonomously on IVR menus) and a
  manual web keypad (`AT+QVTS`, `AT+VTS` fallback).
- macOS USB→PTY bridge (`scripts/ec20_usb_pty.py`) — macOS has no native
  Quectel serial driver.

### AI brain (cloud realtime speech-to-speech)

- Alibaba Qwen Omni realtime by default (16 kHz in / 24 kHz out, server VAD,
  function calling); ByteDance Doubao scaffolding included (not yet at parity).
- AI tools: send SMS, hang up, read latest OTP/SMS, press DTMF keys.
- Per-call LLM summary; live transcript streaming to the UI.
- Half-duplex echo suppression; voice channel re-armed per call (fixes
  silent second call).

### Desktop app & UI

- CallPilot desktop app (PyInstaller `.app`, thin shell over local checkout)
  with phosphor-green dark UI, dock navigation (phone / live / SMS / history /
  settings), bilingual English/Chinese (English default).
- Local speaker monitoring of both call directions with adjustable gain.
- Call recordings (`events.jsonl` + uplink/downlink WAV + metadata), latency
  metrics, call history, live settings editing.

### Reliability

- Resilient startup: the app and web UI come up even with no modem attached; a
  supervisor connects in the background with exponential backoff.
- Modem serial auto-reconnect after USB replug.
- USB stability hardening: root cause of drop-off storms identified as system
  sleep → USB re-enumeration → endpoint stall. Mitigations: `caffeinate -s` in
  launchd plists, `dev.reset()` before bridge reconnect, exponential backoff
  with fail-threshold exit for cold restart, single-instance lock.
- launchd units for the bridge and the app (RunAtLoad / KeepAlive).

### Known limitations

- macOS only in practice; `uac_ffmpeg` is the only verified audio mode
  (`nmea` crashes the EC20's USB on macOS — do not use).
- Windows/Linux serial paths exist but are unverified.
- No barge-in (half-duplex); no self-contained installer yet.
- Requires your own DashScope API key and carrier SIM with voice + SMS.

[0.3.0]: https://github.com/tianye1999/callpilot/releases/tag/v0.3.0
[0.2.0]: https://github.com/tianye1999/callpilot/releases/tag/v0.2.0
[0.1.0]: https://github.com/tianye1999/callpilot/releases/tag/v0.1.0
