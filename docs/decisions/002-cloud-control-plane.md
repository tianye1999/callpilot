# ADR-002: Company-hosted control plane for remote handsets

Status: Accepted for issue #42 Beta

## Context

The #31/#31.1 browser dialer proves that a phone can act as a remote handset for
a Dongle SIM. Its fixed URL currently reaches one Edge through a Cloudflare
Tunnel installed on that computer. Requiring every customer to own and operate
a tunnel does not provide stable routing for multiple Edges and distributes
infrastructure credentials to end-user machines.

## Decision

CallPilot will operate a small control plane at `api.bondings.ai`. The phone and
Edge both initiate outbound TLS connections to it; no customer computer accepts
public inbound traffic. `dial.bondings.ai` is a company-hosted PWA.

The control plane owns enrollment, device pairing, Edge presence, command relay,
rate limits, redacted audit, and short-lived LiveKit token issuance. LiveKit
carries media directly between the phone and Edge. Audio, recordings, SMS bodies,
and transcripts are not stored by the control plane.

The Beta implementation uses Cloudflare Workers, Durable Objects, and D1. An
Edge's Durable Object owns its live WebSocket and serial command stream. D1 owns
durable device and authorization metadata. LiveKit API credentials are Worker
secrets and are never distributed to Edge or browser clients.

The existing loopback gateway and Tunnel flow remain an explicitly enabled
diagnostic fallback during migration.

## Security boundaries

- Beta enrollment and phone pairing proofs are one-time and expire quickly.
- Long-lived credentials are random bearer secrets; only SHA-256 hashes are
  stored server-side. Edge secrets are stored in the operating-system keychain.
- Every call and command has an opaque ID, expiry, and idempotency key.
- Cloud authorization never bypasses local Edge policy, modem readiness, line
  ownership, or rate limiting.
- Phone credentials authorize one paired Edge only and cannot reach SMS,
  recordings, settings, or arbitrary modem operations.
- LiveKit credentials are room- and identity-scoped and expire after five minutes.
- Real-person remote calls are not recorded unless the owner explicitly enables
  recording on the Edge.

## Consequences

Users no longer configure Cloudflare Tunnel or LiveKit secrets. CallPilot must
operate a small public service and a data store, and must monitor its availability.
The browser/Android protocol becomes a versioned external contract. Native inbound
ringing and full account/billing systems remain outside this Beta.

