# Changelog

All notable changes to CallPilot are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/) (pre-1.0: minor bumps may break).

## [0.1.0] — 2026-07-08 · Developer Preview

First public release. Verified end-to-end on real hardware: a Quectel EC20
(`EC20CEFAGR08A03M4G`) on macOS, talking to China Telecom's `10000` IVR in both
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
  settings), bilingual EN/中文 (English default).
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

[0.1.0]: https://github.com/tianye1999/callpilot/releases/tag/v0.1.0
