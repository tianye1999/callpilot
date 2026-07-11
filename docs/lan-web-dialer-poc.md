# LAN Web Dialer POC

> **⚠️ ARCHIVED / SUPERSEDED** — 本 POC 已被 #31（Web Dialer）/ #42（云控制面）的远程
> 拨号路线取代，仅作实验留证归档，不再维护。自签 CA + URL query token + `0.0.0.0`
> 监听 + modem 直控仅适用于 LAN 实验，**不得据此构建产品**；未来若做局域网/离线自托管，
> 基于正式协议重做，而非复活本 POC。

This POC verifies one narrow claim: a phone browser on the same LAN can provide
the user's microphone/speaker while the Mac with the Dongle places the real SIM
call.

It is intentionally separate from the normal AgentCall AI pipeline:

- no LiveKit or cloud relay
- no production iOS app
- no website deployment
- no persistent account system
- one active call at a time

## Run

```bash
PYTHONPATH=src ~/AgentCall/.venv/bin/python scripts/lan_web_dialer.py
```

The script prints:

- `iPhone URL`, opened on the phone browser
- `TLS cert`, installed once on the phone and trusted for local HTTPS

The phone and the Mac must be on the same LAN.  Browser microphone access needs
HTTPS; plain `http://192.168.x.x` is not enough for `getUserMedia`.

## Acceptance

1. Plug the Dongle into the Mac and confirm the SIM can place calls.
2. Start the POC script.
3. Install/trust the printed certificate on the phone once.
4. Open the printed `https://<Mac-LAN-IP>:47443/?token=...` URL.
5. Enter B's number, tap dial, wait for B to answer.
6. Verify 60 seconds of two-way speech.
7. Tap hangup and confirm the cellular call ends.

## Safety

The server requires a random per-run token in the URL.  Do not expose this POC
outside the LAN.  It can place real SIM calls.

## 2026-07-11 Hardware Result

Environment: Mac + EC20/EG25 Dongle + real SIM, iPhone browser on the same LAN,
calling the public `10000` service line.

Observed result:

- The browser dial action reached Edge and issued `ATD10000;`.
- The call connected and the phone browser heard the remote IVR audio.
- Browser DTMF buttons sent real DTMF over the Dongle call.
- The carrier returned service SMS after IVR interaction, confirming the call
  reached the real network service.
- The phone microphone path produced non-silent uplink audio:
  `phone_mic_non_silent` increased from `2046` to `117304` bytes during the
  test, while `modem_to_phone` also increased continuously.

Conclusion: the minimum hardware/media assumption is valid.  A same-LAN phone
browser can act as the handset for a real Dongle SIM call, with audio flowing in
both directions through the Mac Edge process.

Important implementation notes from the run:

- Stop the normal CallPilot app service while running this POC; otherwise both
  processes can compete for the same `/tmp/ec20-at` serial bridge.
- If the USB PTY bridge collapses, software restart may not recover it; a
  physical Dongle replug restored `/tmp/ec20-at`, `/tmp/ec20-nmea`, and
  `/tmp/ec20-modem` in this test.
- Keep modem commands off the aiohttp event loop and use command timeouts, so a
  dead serial bridge does not leave the browser with a silent "no response"
  button state.
