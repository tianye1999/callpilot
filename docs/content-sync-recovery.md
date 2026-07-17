# Content Sync Device Revocation and Recovery

This runbook covers a lost or replaced phone in the closed Beta content-sync
deployment. It applies to hosted mode, where the Edge remains the source of truth
and Cloud only relays bounded reads.

## What revocation does

Revoking a phone invalidates that phone's Cloud credential. New content reads are
rejected, and a response already in flight is re-authorized and discarded before
delivery. Revocation does not delete SMS or call records from the Edge and does
not disable other paired phones.

Revocation is not remote device wipe. A lost phone that is offline cannot receive
the authorization failure. Its local protected cache is erased only when the App
runs again, reaches Cloud, and observes that its credential is unauthorized. Use
the operating system's lost-device controls when remote device erasure is needed.

## Revoke the lost phone

1. Open the CallPilot dashboard on the computer that owns the Edge.
2. In the paired-device list, identify the lost phone by its display name and last
   activity time. Do not revoke a device when its identity is uncertain.
3. Select **Revoke** and confirm.
4. Refresh the device list and confirm that the revoked phone is no longer active.

After revocation, the old phone must show the pairing screen the next time it can
contact Cloud. Its Messages and Call Records caches, details, and local unread
watermark must be cleared. The Edge's local source records remain intact.

## Pair a replacement phone

1. From the same dashboard, create a new short-lived pairing code.
2. Install or open CallPilot on the replacement phone and claim that code before
   it expires. Never send a pairing code through a public channel.
3. Confirm that Settings shows the expected Edge and that content sync is
   available. The closed-Beta Edge allowlist grant is attached to the Edge, so a
   replacement phone paired to the same active Edge receives the approved read
   capabilities; pairing to another Edge does not inherit them.
4. Open Messages and Call Records and pull to refresh. Confirm that both pages show
   live data rather than stale cache or an offline state.
5. Confirm once more that the old credential remains rejected. Re-pairing a new
   phone never revives the revoked device record.

## Expected failure states

| Observation | Meaning | Action |
| --- | --- | --- |
| Pairing screen after the old phone reconnects | Revocation took effect | No action on that phone unless it is recovered and intentionally paired again |
| `FORBIDDEN` after replacement pairing | The Edge is not on the closed-Beta content allowlist, or a content gate is off | Check the approved Edge allowlist and both content feature gates; do not broaden the grant to another Edge |
| `EDGE_OFFLINE` | Cloud cannot reach the owner's Edge | Restore the Edge service or network, then refresh |
| `TIMEOUT` | Edge did not answer the bounded relay request | Retry once after checking Edge health; do not repeatedly refresh |
| Messages or calls remain stale | The App has cache but no successful fresh read | Check the displayed error and Edge connectivity; stale data must not be presented as live |

## Verification boundary

The automated Cloud integration test exercises revocation while plaintext is in
flight, verifies the old credential remains unauthorized, pairs a replacement
device to the same Edge, confirms both read capabilities, and completes a fresh
message relay. It also verifies that relayed content is absent from Worker output
and the local D1 export.

Do not perform destructive revocation against the production Beta owner device as
a routine test. Use the local Worker integration test for the destructive matrix
and retain the existing non-destructive real-device content-read evidence for the
Beta deployment.
