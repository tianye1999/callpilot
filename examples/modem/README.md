# Modem atomic-capability examples

Minimal, standalone demos of each **modem communication primitive**, built directly
on `agentcall.modem.Eg25Modem`. Each script does exactly one thing so you can learn
and verify a single building block in isolation — before wiring them into your own
app or debugging the full CallPilot service.

These are the atoms; the full application (`app.py`) is what you get when you compose
them (auto-answer + AI agent + audio bridge + web UI).

## Capabilities

| Script | Capability | Needs a live call? |
|--------|------------|--------------------|
| `at_console.py` | Send any raw AT command, print the reply (the lowest-level atom) | no |
| `probe_device.py` | Self-check: SIM, signal, network, voice channel, firmware | no |
| `dial_call.py` | Place an outbound call, wait for answer, hold, hang up | — |
| `answer_call.py` | Wait for `RING`, auto-answer (`ATA`), hold until the other side hangs up | — |
| `send_sms.py` | Send one SMS (Chinese auto-encoded as UCS2) | no |
| `receive_sms.py` | Listen for and print incoming SMS (`+CMTI` → read → decode) | no |
| `send_dtmf.py` | Send DTMF key tones for IVR navigation (`AT+QVTS`) | **yes** |

## Prerequisites

1. **Install the package** (editable), from the repo root:
   ```bash
   python -m venv .venv
   .venv/bin/pip install -e ".[dev]"
   ```
2. **Hardware**: a Quectel EC20/EG25 modem with an active SIM (voice + SMS).
3. **Config**: copy `.env.example` to `.env`. The examples read `MODEM_PORT` /
   `MODEM_BAUD` from it (same source of truth as the app).
4. **macOS only** — the AT port is not a native device; start the USB→PTY bridge
   first, in a separate terminal, and leave it running:
   ```bash
   .venv/bin/python scripts/ec20_usb_pty.py --map 2:/tmp/ec20-at
   ```
   Then set `MODEM_PORT=/tmp/ec20-at` in `.env` (this is the macOS default).
5. **Do not run these while the main app owns the serial port.** Stop the service
   first (e.g. `launchctl stop com.agentcall.app`, or just don't run `app.py`) —
   two processes cannot share the AT port. The USB bridge can stay running.

## Running

From the repo root:

```bash
.venv/bin/python examples/modem/probe_device.py
.venv/bin/python examples/modem/at_console.py "AT+CSQ"
.venv/bin/python examples/modem/dial_call.py 10000 20
.venv/bin/python examples/modem/send_sms.py 10086 CXLL
```

`answer_call.py` and `receive_sms.py` run until you press `Ctrl-C`.

## Safety

- `dial_call.py` and `send_sms.py` perform **real** telephony and may incur carrier
  charges. Only dial/text numbers you are authorized to contact (your own phone, a
  carrier service hotline such as `10000`/`10086`, etc.).
- `send_dtmf.py` only does something useful during an active call — pair it with
  `dial_call.py`.

## Related tools (already in `scripts/`)

- `scripts/eg25_probe.py` — broader device probe (scans all serial ports).
- `scripts/uac_check.py` — verify the modem's UAC sound card works both directions.
- `scripts/ec20_usb_pty.py` — the macOS USB→PTY bridge these examples depend on.
