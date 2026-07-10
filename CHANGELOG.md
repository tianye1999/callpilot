# Changelog

All notable changes to CallPilot are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/) (pre-1.0: minor bumps may break).

## [0.5.6] — 2026-07-11

Skips 0.5.5, which was only ever a local test build (packaged before the
Info.plist and setup-wizard fixes below landed); 0.5.6 is the first release to
ship them.

### Fixed

- **Menu-bar "Open console" spawned a new window on every click**: the tray now
  remembers the dashboard window and brings the existing one to the front via
  `osascript` instead of launching a duplicate pywebview process each time
  (logic extracted into the unit-tested `open_or_focus_dashboard`).
- **Setup wizard looked "ready" when hardware wasn't**: the hardware step now
  shows a red status when the EC20/EG25 module or AT port isn't detected,
  instead of a neutral one (the check still does not block setup).

### Added

- **AppleEvents usage declaration** (`NSAppleEventsUsageDescription`) in the app
  bundle, so first-launch actions that send system events (raising the console
  window, opening the browser UI) declare their purpose to macOS.
- **New-user docs**: `docs/faq.md` (install → first call — Gatekeeper, hardware /
  SIM prerequisites, UAC auto-enable and first re-plug, API-key setup, permission
  prompts, link self-test) and `docs/number-profiles.md` (task-library structure,
  match order, scenario-writing guide with a minimal template); README links both
  and notes audio needs no manual AT setup.

## [0.5.4] — 2026-07-10

### Fixed

- **New SMS not received once SIM storage fills up**: the SIM's SMS store holds
  only ~20-50 messages; once full, new messages never reach the modem at all.
  CallPilot now deletes each SMS from the SIM right after ingesting it into the
  app (both back-fill and live receive), so storage never fills and `+CMTI`
  live delivery keeps working. New `SMS_DELETE_AFTER_INGEST` setting (default
  on) can disable it.

## [0.5.3] — 2026-07-10

### Fixed

- **Incoming SMS not appearing in the app**: stored SIM messages were only
  logged at startup (never ingested), and live `+CMTI` push only covers
  messages that arrive while the service is running — anything received during
  a restart gap was lost. Now the app back-fills stored SIM messages on
  startup, with SMS de-duplication (sender + timestamp + body fingerprint,
  persisted across restarts) so nothing is ingested or email-forwarded twice.

## [0.5.2] — 2026-07-10

### Added

- **SMS-to-email forwarding** (#17, opt-in, off by default): live modem SMS
  messages enter a bounded background queue and are sent through per-deployment
  TLS-only SMTP credentials. Reliable OTPs lead the subject for notification
  previews; settings are atomically validated, secrets masked, and forwarding
  never blocks the modem callback or crashes the service.

### Fixed

- The v0.5.1 DMG was built before this feature merged, so its Settings page had
  no email fields. This build includes them; `--selftest` now also covers the
  forwarder module.

## [0.5.0] — 2026-07-10

### Added

- **Local three-stage provider** (`AGENT_PROVIDER=local`): on-device
  VAD → STT → text LLM → on-device TTS. Audio never leaves your machine —
  only the transcript goes to the cloud brain (default `qwen-plus`, reusing
  your DashScope key; text tokens cost an order of magnitude less than
  realtime audio). Powered by the sherpa-onnx family (silero-vad v5,
  paraformer-zh int8, piper zh_CN — no torch). Install with
  `pip install 'callpilot[local]'`, then fetch models (~300 MB, one-time)
  with `python -m agentcall.local_models`. Tools, live transcript, call
  summary and the preset library all work as with realtime providers.
  Real-hardware acceptance: 8/8 regression assertions, first audio in ~1.9 s,
  both directions understood by the carrier IVR.
- **Batch dialing results table**: the queue panel now shows a full
  per-number results table (dialed/failed + error detail) instead of the last
  10 chips.
- **On-call timer**: the state chip shows mm:ss while a call is active.
- **Web auth token for non-loopback deployments**: exposing `WEB_HOST` beyond
  127.0.0.1 now requires `WEB_AUTH_TOKEN` (Bearer header or `?token=`);
  the app refuses to start bare. Loopback behavior is unchanged.

### Fixed

- **Wrap-up judge now treats a definite negative answer as the result**
  ("no such plan on this account" IS the final answer): the AI says goodbye
  within a couple turns instead of re-asking the same question in different
  words. Verified end-to-end on a real call (4 s from negative answer to
  goodbye; previously 4-5 re-asks). Judge decisions are now logged to the
  call's events for observability, and a new regression assertion (WARN on
  ≥3 near-identical re-asks) guards the behavior long-term.
- **Audio main loop can no longer be stalled by a degraded realtime link**:
  dashscope's synchronous websocket send could hang for tens of seconds on a
  bad network, freezing the loop that enforces the 150 s hard call limit
  (two real calls ran 180 s+). Sends now run in a thread with a timeout
  circuit breaker (2 s audio / 5 s say) and fall back to the existing
  reconnect path.
- **Regression tool follows the running service's recordings directory**
  (`/api/meta.recordings_dir`): the packaged app and the dev checkout use
  different data directories, and the dial-and-assert tool was scanning the
  wrong one — every packaged-app run "timed out" while the call actually
  succeeded. Also waits for the recording to flush after a forced hangup.

### Engineering

- Manual-response silence window calibrated on real calls (IVR burst gap
  p90 ≈ 2.4 s → use `MANUAL_RESPONSE_SILENCE_MS=2500` when enabling; stays
  off by default).
- `.playwright-cli/` session artifacts are now git-ignored.

## [0.4.3] — 2026-07-10

### Added

- **Preset library manager**: a bilingual Presets page with create / edit /
  duplicate / enable-disable / delete / search and a global switch, backed by
  an atomic local-JSON CRUD API with stable profile IDs, field validation,
  conflict detection and legacy-file compatibility. Dialing now selects
  presets by stable ID, so renaming a task no longer breaks the match (#8).
- **Menu-bar icon is kept alive by launchd** (`com.agentcall.tray`): the icon
  appears at login and is restarted on crash. A second instance started by
  double-clicking yields quietly via a singleton lock instead of fighting the
  resident one, and the tray no longer risks killing itself while refreshing
  its own launchd unit (#11).

### Fixed

- **Cross-platform CI baseline** (#10): Windows mypy (`os.getuid`), POSIX-only
  bridge tests now skipped on Windows, launchd test assertions made
  host-neutral.
- **Manual response control race** (#10): a reply can no longer double-fire in
  the request→`response.created` wire gap; a watchdog recovers from a lost
  `created`/`done` event.

### Engineering

- The packaged app version now has a single source of truth
  (`pyproject.toml`); the PyInstaller spec reads it at build time, and a test
  keeps them from drifting.
- Quality gate hardened: releases / batch closes now require the GitHub
  Actions three-platform matrix to be green (local triad alone proved
  insufficient).

## [0.4.2] — 2026-07-09

> Version 0.4.1 was an internal packaged-app version bump only and was never
> published as a release; its fixes ship here.

### Added

- **10 public-hotline presets out of the box**: a fresh install now seeds the
  task preset library with carriers (10000 data/balance, 10086, 10010), four
  banks' card-statement lines (95588 / 95533 / 95555 / 95566), 12315 consumer
  complaints and 12345 government services — bilingual, zero private data —
  instead of 2 placeholder entries.
- **OpenAI provider upgraded to the gpt-realtime-2.1 family** (default
  `gpt-realtime-2.1-mini` for lower call latency) plus a first-audio latency
  metric per call (#3).
- launchd installer (`scripts/launchd/install.sh`) supports both the dev
  checkout and the packaged app.

### Fixed

First real-device batch on the packaged (signed DMG) app (#6):

- **Microphone permission**: `NSMicrophoneUsageDescription` +
  `com.apple.security.device.audio-input` entitlement now ship with the
  bundle — under hardened runtime macOS silently muted capture, so the uplink
  sat at −91 dB and the AI could not hear the other side. Calls work again.
- Packaged background service no longer pops a browser tab on start (the dev
  checkout keeps the auto-open).
- The native window opened from the tray now comes to the foreground.
- First launch seeds the preset library into the user data directory.
- History page: clicking a recording's play control no longer collapses the
  entry (and no longer mutes playback).
- SMS reply whitelist now includes outbound numbers that actually answered
  (CSRF guard preserved for everything else).
- Setup wizard: the test SMS can be re-run after setup completes (a fresh
  token is issued via `/api/meta`).

### Engineering

- mypy debt cleared: `ignore_errors` removed for the 7 remaining core modules;
  the whole repo is now genuinely type-checked (E1).
- Real-hardware regression calibrated over an 8-round dial run; transcript
  assertions tuned (short politeness phrases exempted) (D1).
- Community files moved into `.github/`; stray build artifacts removed from
  the repo and ignored.

## [0.4.0] — 2026-07-09

### Added

- **Task preset library** (预调教任务库): pre-tuned per-(number + task) prompt
  profiles in a local JSON file. A matched preset pins the call's scenario
  strategy and opening line — stable stance, no self-introduction loops, no
  model roundtrip. Hierarchical matching: exact number+task, then number-only
  wildcard, then dynamic generation as fallback. Fully bilingual: every field
  accepts a plain string or `{"zh": …, "en": …}`; scenario/opening follow the
  call language, labels follow the UI language.
- **Preset dropdown on the dial panel**: pick a preset to auto-fill number +
  topic (guaranteed profile hit — no more near-miss typing). The topic field
  stays editable as a per-call sub-topic ("check *last month's* data usage")
  without losing the preset's strategy.
- **Dynamic scenario prompts**: for numbers not in the preset library, a cheap
  text model drafts a per-call scenario + opening while the phone is still
  ringing (falls back to the template on timeout/failure; cached per
  number+task).
- **In-band DTMF** (default): keypad tones are synthesized into the uplink PCM
  itself. `AT+QVTS` tones never reached the far end in UAC audio mode — IVRs
  kept saying "no input detected"; in-band fixes the path and the tones are
  audible in call recordings for auditing.
- **Manual response control** (experimental, default off): when a rambling IVR
  triggers a reply per menu line, `MANUAL_RESPONSE_CONTROL=true` merges
  consecutive speech into one turn via a silence-debounce and answers once.
- **Voice settings**: voice pickers for Qwen/OpenAI with official preview
  links, plus `VOICE_STYLE` — a free-text speaking-style hint merged into the
  session instructions for both providers.
- **Real-hardware regression script** (`scripts/regression_call.py`): dials the
  carrier hotline, waits for the call to finish, and asserts transcript
  quality (no self-intro loop, no fabricated figures, no impersonating the
  callee's organization, profile hit, clean wrap-up) — PASS/FAIL exit codes
  for use in loops/CI.
- **Signed & notarized DMG**: `packaging/build_installer.sh` now signs
  (Developer ID), notarizes, staples, and self-verifies the artifacts when
  `CODESIGN_IDENTITY`/`NOTARY_PROFILE` are set — no more right-click-to-open
  on first launch.

### Fixed

- **AI fabricating results**: the model could claim "your remaining data is
  5 GB" before the callee said anything. Hardened the core prompt: never state
  figures or claim completion before the other side actually provides them.
  Verified gone on real calls.
- **AI impersonating the callee's organization** ("this is China Telecom
  customer service…"): stance rules hardened — the agent is always the caller
  acting for the owner, never the institution.
- **AI saying "I'll press 2" without pressing**: `send_dtmf` was missing from
  the always-available tool list, so the model narrated key presses instead of
  calling the tool. Now listed and mandated.

## [0.3.1] — 2026-07-09

### Fixed

- **Repeat suppression could silence the call** (regression in 0.3.0, caught on
  real hardware): dropped audio wasn't communicated to the model, so when the
  other side asked it to repeat, the repeat was dropped again — the callee
  heard dead air until they hung up. Now the second occurrence plays (repeating
  once when asked is legitimate), from the third on the audio is dropped *and*
  the model is nudged to rephrase (8s cooldown); three consecutive suppressed
  repeats mark the call as stuck and end it politely instead of leaving
  silence.

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

[0.5.4]: https://github.com/tianye1999/callpilot/releases/tag/v0.5.4
[0.5.3]: https://github.com/tianye1999/callpilot/releases/tag/v0.5.3
[0.5.2]: https://github.com/tianye1999/callpilot/releases/tag/v0.5.2
[0.5.1]: https://github.com/tianye1999/callpilot/releases/tag/v0.5.1
[0.5.0]: https://github.com/tianye1999/callpilot/releases/tag/v0.5.0
[0.4.3]: https://github.com/tianye1999/callpilot/releases/tag/v0.4.3
[0.4.2]: https://github.com/tianye1999/callpilot/releases/tag/v0.4.2
[0.4.0]: https://github.com/tianye1999/callpilot/releases/tag/v0.4.0
[0.3.1]: https://github.com/tianye1999/callpilot/releases/tag/v0.3.1
[0.3.0]: https://github.com/tianye1999/callpilot/releases/tag/v0.3.0
[0.2.0]: https://github.com/tianye1999/callpilot/releases/tag/v0.2.0
[0.1.0]: https://github.com/tianye1999/callpilot/releases/tag/v0.1.0
