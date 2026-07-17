# ADR 005: iOS incoming calls use PushKit and CallKit

Status: Accepted for phased implementation (2026-07-17)

## Decision

The iOS client will use one VoIP push to wake the app for a newly offered inbound
takeover, then immediately report that call to CallKit. The push contains only an
opaque offer id, a server-generated call UUID, and the expiry time. It contains no
number, caller identity, transcript, preference, nonce, or model output.

The app continues to learn revoke and expiry state from the authenticated offer
API after wake. Cloud does not send a second VoIP push to cancel an offer. This
keeps PushKit limited to its documented incoming-call purpose and preserves the
existing foreground polling path as a fallback.

Device tokens are accepted only from an authenticated paired device, encrypted
with AES-256-GCM before D1 storage, and deleted on unpair, device revocation, or
an APNs `BadDeviceToken`/unregistered response. APNs credentials and the token
encryption key are Worker secrets. `VOIP_PUSH_ENABLED` must equal `true`; every
other value fails closed.

CallKit owns the system incoming-call surface and audio activation. The client
must report the push promptly, deduplicate by offer id and call UUID, and retain
attempt-generation fencing. Answer claims the offer and connects LiveKit before
fulfilling the CallKit action. Decline dismisses the local offer. LiveKit automatic
audio configuration remains disabled until CallKit activates the audio session;
deactivation releases the engine again.

## Operational boundary

The first rollout is closed Beta. Migration and Worker code may be deployed with
the feature disabled. Enabling it additionally requires the APNs signing key,
token encryption key, explicit Worker flag, iOS VoIP entitlement, and a signed
device build. Turning the flag off stops registration and delivery; token removal
remains available so revocation never depends on the feature being enabled.

Push delivery is best effort and never changes the offer state. The foreground
offer API remains authoritative. No APNs token, signing key, device credential,
or push payload is logged.
