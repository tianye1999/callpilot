# ADR-002: Company-hosted control plane for remote handsets

Status: Accepted for issue #42 Beta; extended for issue #99 content-read MVP

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
carries media directly between the phone and Edge. Audio and recordings are not
stored or relayed by the control plane.

Issue #99 extends the control plane with an explicitly granted, read-only content
capability for SMS metadata/bodies and normalized call records/transcripts. Edge
remains the only source of truth. The control plane authenticates the paired
device, relays one bounded request over the existing Edge WebSocket, returns the
bounded response, and then discards it. SMS bodies and call content are never
written to Durable Object storage, D1, R2, KV, Cache API, logs, audit details, or
metric labels. They do pass through Worker and Durable Object memory as plaintext
inside TLS while the request is active; this MVP is therefore transient relay,
not content end-to-end encryption.

The Beta implementation uses Cloudflare Workers, Durable Objects, and D1. An
Edge's Durable Object owns its live WebSocket and serial command stream. D1 owns
durable device and authorization metadata. LiveKit API credentials are Worker
secrets and are never distributed to Edge or browser clients.

The existing loopback gateway and Tunnel flow remain an explicitly enabled
diagnostic fallback during migration.

## Security boundaries

- Beta enrollment and phone pairing proofs are one-time and expire quickly.
- Long-lived credentials are random bearer secrets; only SHA-256 hashes are
  stored server-side. Each Edge request also proves possession of its Ed25519
  device key. Both secrets stay in the operating-system keychain.
- Every call and command has an opaque ID, expiry, and idempotency key.
- Cloud authorization never bypasses local Edge policy, modem readiness, line
  ownership, or rate limiting.
- A phone credential authorizes one paired Edge only. By default it cannot reach
  SMS, call records, recordings, settings, or arbitrary modem operations.
- Issue #99 may add `messages:read` and `call_records:read` to an active paired
  device. These capabilities grant only the versioned content endpoints and
  normalized DTOs in `docs/content-sync-protocol.md`; they never grant a generic
  Edge proxy, local admin API access, SMS sending, deletion, recordings, settings,
  or arbitrary modem operations.
- Content read requires both the Cloud and the Edge feature gate. Both default to
  off. Cloud checks device ownership, capability, revocation, expiry and rate
  limit before dispatch; Edge independently checks its local gate, resource
  allowlist, request expiry and size before reading local data.
- Revocation is effective for new reads immediately. A response from an in-flight
  request is re-authorized before delivery and is discarded if the device was
  revoked meanwhile. On the next authorization failure, native clients erase the
  protected content cache and local unread watermark.
- LiveKit credentials are room- and identity-scoped and expire after five minutes.
- Real-person remote calls are not recorded unless the owner explicitly enables
  recording on the Edge.

## Consequences

Users no longer configure Cloudflare Tunnel or LiveKit secrets. CallPilot must
operate a small public service and a data store, and must monitor its availability.
The browser/Android protocol becomes a versioned external contract. Native inbound
ringing and full account/billing systems remain outside this Beta.

Content reads require the Edge to be online. Native clients may retain a bounded,
platform-protected cache for offline display and must label it stale rather than
present it as current. Cloud caching, cross-device read state, content push,
recording transfer, writes, and content E2EE remain separate decisions. The
company must accurately disclose that transient Cloud processing can see content
until an end-to-end encrypted relay is designed.

References: #42, #99, #56, #27,
[`content-sync-protocol.md`](../content-sync-protocol.md).
