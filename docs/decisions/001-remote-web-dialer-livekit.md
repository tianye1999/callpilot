# ADR-001: Use a static Web Dialer and LiveKit room for remote SIM calls

## Status

Accepted for issue #31; amended for #31.1 fixed entry and pairing

## Date

2026-07-10

## Context

The first remote-handset proof needs to answer one question before native mobile
work starts: can a user away from the Dongle speak through a phone browser while
the Dongle SIM places the real PSTN call?

The existing aiohttp server is a privileged local admin surface. Publishing that
port through a tunnel would expose dial, SMS, settings, and recording APIs and is
not an acceptable control path. A direct browser-to-Edge WebRTC peer also needs a
signalling service and TURN deployment before it can work reliably across mobile
and home NATs.

## Decision

Use a short-lived, one-room LiveKit session with two participants:

- `web-*`: the phone browser. It may publish one microphone source, subscribe to
  Edge audio, and publish room data.
- `edge-*`: AgentCall on the Dongle computer. It publishes PSTN downlink audio,
  consumes browser audio, and accepts scoped control messages.

The dialer is three static files and can be hosted on any HTTPS static host. A
locally authenticated dashboard request creates room-scoped browser and Edge JWTs.
Only the browser token is encoded in the invitation URL fragment. The browser
erases that fragment before decoding it; the static host does not receive it in an
HTTP request.

Dial, DTMF, hangup, and status messages use reliable LiveKit data packets. The Edge
joins LiveKit outbound, so no inbound connection to the local aiohttp admin server
is required. The Edge repeats all authority checks and does not send modem `ATD`
until the expected browser participant has published an audio track.

The Dongle side stays at 8 kHz, signed 16-bit, mono PCM. LiveKit performs WebRTC
resampling at the programmatic participant boundary. Both directions use bounded
queues; congestion discards the oldest media instead of accumulating latency.

## Security Boundary

- Feature default: `REMOTE_WEB_DIALER_ENABLED=false`.
- Invitation TTL: 30-900 seconds, default 300.
- One invitation permits one browser identity and at most one call attempt.
- Tokens are room-scoped; browser publishing is restricted to microphone audio.
- Control packets are topic-, identity-, size-, schema-, and state-checked.
- Every call request has an idempotency key and an hourly Edge-side rate limit.
- Media loss beyond the configured grace period hangs up the physical modem.
- Logs and EventHub messages never contain LiveKit tokens, API credentials, or the
  dialled number. The existing local call record retains the number for audit.
- Never tunnel or publish the CallPilot admin port (`WEB_PORT`, default 47100).

This POC does not configure LiveKit E2EE. It relies on LiveKit's normal WebRTC
transport and must not be described as end-to-end encrypted.

## Configuration

```dotenv
REMOTE_WEB_DIALER_ENABLED=true
REMOTE_MEDIA_PROVIDER=livekit
REMOTE_DTMF_MODE=qvts
REMOTE_CONTROL_URL=https://dial.example.com/
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
REMOTE_DISCONNECT_GRACE_SECONDS=5
REMOTE_OUTBOUND_MAX_SECONDS=1800
REMOTE_DIAL_LIMIT_PER_HOUR=10
REMOTE_GATEWAY_PORT=47445
```

`REMOTE_DTMF_MODE` intentionally defaults to `qvts`. A real EC20/EG25 UAC call
showed that the carrier IVR ignored in-band-only tones, while QVTS triggered the
menu and its resulting service SMS. `both` remains available for hardware that
needs both paths.

Route the HTTPS hostname to the dedicated loopback gateway port. The gateway
serves the browser files with the required CSP, referrer, permissions, and content
type headers. Then enable the feature, restart CallPilot, and use **Pair phone /
配对手机** on the local dial panel. **Temporary link / 临时链接** retains the
original fragment-only invitation flow for diagnostics. The LiveKit API secret
stays on Edge and is never sent to the browser.

## Alternatives Considered

### Native iOS/Android first

Rejected for the first proof. PushKit, CallKit, provisioning, background audio, and
store distribution do not help prove the core Dongle-to-public-WebRTC media path.

### Expose the existing aiohttp server through a tunnel

Rejected. It makes a privileged local management surface part of the public attack
surface and gives the browser more authority than one call needs.

### Raw WebSocket PCM

Rejected for the public mobile path. It would require hand-built jitter handling,
codec/resampling behavior, echo handling, and NAT/public ingress that WebRTC already
solves.

### Direct browser-to-Edge WebRTC

Deferred. It still needs signalling, STUN/TURN, reconnect semantics, and operational
work. A managed or self-hosted LiveKit deployment gives the POC those pieces without
putting the admin server online.

## Consequences

- `livekit` and `livekit-api` become runtime and packaging dependencies.
- A LiveKit project and an HTTPS host for three static files are required for a real
  off-LAN test.
- The page must stay foreground for dependable mobile-browser audio. Locked-screen
  incoming-call behavior still belongs to parent issue #30 and a native app.
- The Edge media/control contracts can later be reused by the native handset bridge.

## #31.1 Amendment: Fixed Entry and Device Pairing

The successful public POC exposed one usability gap: the user had to create and
copy a new URL before every call. The browser iteration keeps the same LiveKit
call scope while adding a narrow Edge HTTP gateway:

- The phone reuses one fixed HTTPS origin and pairs once with a five-minute,
  one-time code carried in a URL fragment.
- The public gateway binds only to `127.0.0.1:47445` and is the sole Cloudflare
  Tunnel origin. It does not mount the admin application's routes.
- Successful pairing sets a host-only HttpOnly, Secure, SameSite=Strict cookie.
  Edge stores only the SHA-256 hash of its random secret in a mode-0600 local
  file; the local dashboard can list and revoke devices.
- A paired phone calls `POST /api/session` for each call. The response contains a
  new short-lived, room-scoped invitation; the durable cookie is never shared
  with LiveKit or exposed to JavaScript.
- While any remote worker is alive, every other paired or legacy session request
  is rejected instead of reusing its room/token; one phone therefore cannot join
  or interrupt another phone's in-flight media session.
- Pairing and session mutations require an exact same-origin `Origin` header.
  Pair attempts are rate-limited, codes expire and are one-time, and the active
  paired-device cap defaults to five.
- The previous URL-fragment invitation remains compatible as a diagnostic
  fallback.

Example Cloudflare Tunnel ingress:

```yaml
ingress:
  - hostname: dial.example.com
    service: http://127.0.0.1:47445
  - service: http_status:404
```

The browser shell includes a manifest and service worker for home-screen use,
but foreground browser limitations remain unchanged. No inbound ringing,
background wake, PushKit, CallKit, or Android Telecom behavior is claimed.

## References

- [LiveKit raw media tracks](https://docs.livekit.io/transport/media/raw-tracks/)
- [LiveKit tokens and grants](https://docs.livekit.io/frontends/reference/tokens-grants/)
- [LiveKit JavaScript client SDK](https://docs.livekit.io/reference/client-sdk-js/)
- [MDN getUserMedia security requirements](https://developer.mozilla.org/docs/Web/API/MediaDevices/getUserMedia)
