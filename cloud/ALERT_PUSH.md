# Ordinary APNs alerts for the physical-SIM relay

## Changed files

- Edge: `.env.example`, `src/agentcall/remote_alert_push.py`,
  `src/agentcall/config.py`, `src/agentcall/cloud_control.py`,
  `src/agentcall/notification_events.py`, `src/agentcall/call_log.py`,
  `src/agentcall/call_agent.py`; tests `tests/unit/test_remote_alert_push.py`,
  `tests/unit/test_cloud_control.py`.
- Relay: `cloud/src/alert-push.ts`, `cloud/src/voip-push.ts`,
  `cloud/src/edge-room.ts`, `cloud/src/index.ts`, `cloud/src/schemas.ts`,
  `cloud/src/types.ts`, `cloud/migrations/0009_alert_push.sql`,
  `cloud/wrangler.sim-relay.jsonc`.
- Relay tests: `cloud/test/alert-push.test.ts`, `cloud/test/voip-push.test.ts`,
  `cloud/test/integration.mjs`, `cloud/test/sim-api-integration.mjs`,
  `cloud/test/empty-vars`; runbook `cloud/ALERT_PUSH.md`.
- iOS lifecycle/navigation: `ios/CallPilot/CallPilotApp.swift`,
  `ios/CallPilot/AppModel.swift`, `ios/CallPilot/UI/MainTabShell.swift`,
  `ios/CallPilot/Support/AlertPushDelegate.swift`,
  `ios/CallPilot/Protocol/AlertPushRoute.swift`.
- iOS token/content handling: `ios/CallPilot/Support/PushEnvironment.swift`,
  `ios/CallPilot/Call/CallKitCoordinator.swift`,
  `ios/CallPilot/Protocol/HostedCloudClient.swift`,
  `ios/CallPilot/Content/MessageInboxModel.swift`,
  `ios/CallPilot/Content/CallHistoryModel.swift`.
- iOS settings/resources: `ios/project.yml`, `ios/CallPilot/Info.plist`,
  `ios/CallPilot/Resources/en.lproj/Localizable.strings`,
  `ios/CallPilot/Resources/zh-Hans.lproj/Localizable.strings`.
- iOS tests: `ios/run-core-tests.py`, `ios/CallPilotTests/AlertPushTests.swift`,
  `ios/CallPilotTests/CallKitConfigurationTests.swift`,
  `ios/CallPilotTests/HostedCloudClientTests.swift`,
  `ios/CallPilotTests/MessageInboxModelTests.swift`,
  `ios/CallPilotTests/CallHistoryModelTests.swift`.

## Scope and defaults

This is a code-only change. The shared `callpilot-cloud-beta` Worker must not be
deployed for this feature. No production migration, secret update, device test,
modem operation, or call is part of local validation.

All new behavior is opt-in:

| Layer | Setting | Default |
| --- | --- | --- |
| Edge | `REMOTE_ALERT_PUSH_ENABLED` | `false` |
| sim-relay | `ALERT_PUSH_ENABLED` | absent/`false`; only literal `true` enables |
| iOS build | `ALERT_PUSH_ENABLED` -> Info.plist `AlertPushEnabled` | `NO` |

The provisioning-profile environment fix is unconditional, including existing
PushKit registration. Both token types read `Entitlements.aps-environment` from
the embedded profile: `development` means sandbox, `production` means production.
Missing/malformed profiles fall back to production in Release and sandbox in Debug.
No profile contents or token values are logged.

## Contract and decisions

- `PUT /v1/device/alert-push-token` accepts the same strict `{token, environment}`
  body as the VoIP endpoint, with existing device authentication and origin checks.
  `DELETE` removes only the ordinary token. A separate endpoint and table prevent
  an ordinary token from overwriting or invalidating a PushKit token.
- Migration `0009_alert_push.sql` only adds `device_alert_push_tokens` and
  `alert_push_events`; it does not reinterpret any existing table. Tokens use
  the existing AES-GCM encryption key and random nonces.
- Edge sends `{v:1,type:"device.notify",kind,eventId}` on its authenticated
  control socket. Kinds are `sms`, `call_digest`, `missed_call`. Event IDs are
  opaque `event_` plus 64 lowercase hex characters. Extra fields are rejected;
  Edge ownership is taken from the socket attachment, never the message.
- New SMS alerts occur after existing ingestion/deduplication, and SIM history
  backfill is suppressed. Incoming-call alerts occur after record persistence.
  A successful nonempty summary produces `call_digest` only for an answered,
  completed incoming call; otherwise an uncompleted/unanswered incoming call
  produces `missed_call`. Outgoing and `remote_web_dialer`/`REMOTE_HANDSET` calls
  produce neither. Repeated summary writes do not notify again.
- A bounded 128-entry Edge queue expires after five minutes, does not replay
  history at startup, and is drained by the existing cloud thread. Failures log
  generic messages without exception text and cannot affect call/SMS processing.
- D1 atomically reserves `(edge_id,event_id)`, keeps seven-day replay protection,
  and allows at most 20 accepted events per Edge in a rolling minute. This also
  bounds each paired device to 20 alerts per minute. It survives Worker restart.
- Only non-revoked paired devices with an ordinary token are selected. APNs uses
  `apns-push-type: alert`, priority 10, a one-hour expiration, and `APNS_BUNDLE_ID`
  without `.voip`. JWT signing, encryption and sandbox/production routing are
  shared with VoIP. A 410 or 400/BadDeviceToken deletes only the token version
  that failed, not a concurrently refreshed token. Other errors retain it.
- Payloads contain only version, type, kind and `aps` with a localization key
  (`push.sms`, `push.call_digest`, `push.missed_call`) and default sound. There are
  no numbers, names, message bodies, summaries, transcripts or event IDs in APNs.
- iOS asks notification permission only when paired and enabled. Denial is not
  an error; foreground activation checks permission again and removes the token
  if permission was withdrawn. Token delivery, pairing, and activation retry
  registration. Foreground notifications use banner/list/sound; background
  presentation is performed by iOS. A tap selects and resets the relevant tab,
  then refreshes through existing authenticated content APIs. Cold-launch taps
  wait for pairing. A refresh already in flight is allowed to complete before
  fetching again, and unpairing invalidates a waiting refresh.

Apple references:
[APS environment entitlement](https://developer.apple.com/documentation/bundleresources/entitlements/aps-environment),
[remote notification payloads](https://developer.apple.com/documentation/usernotifications/generating-a-remote-notification).

## Local verification

Run Python pytest, Ruff and mypy (native and win32); `npm run check` in `cloud`;
`python3 ios/run-core-tests.py` in Debug and with `CORE_TEST_CONFIGURATION=Release`;
and a Release iphoneos build with `CODE_SIGNING_ALLOWED=NO DEVELOPMENT_TEAM=`.
The iOS flag can be enabled for a validation build with `ALERT_PUSH_ENABLED=YES`.

Wrangler validation must explicitly avoid developer secret files:

```sh
cd cloud
npx wrangler deploy --dry-run -c wrangler.sim-relay.jsonc --env-file test/empty-vars
```

Local integration scripts also pass `test/empty-vars` and use fresh temporary
local D1/DO storage. APNs transport tests use generated in-memory keys and a fake
fetcher, never Apple's servers. Integration exercises actual local Worker HTTP,
authenticated WebSocket, encryption, migrations, replay protection, rolling
rate limits and restart persistence. It is not proof of real-device delivery.

## Future rollout, requiring separate authorization

1. Confirm the deployment target is `callpilot-sim-relay`, configuration is
   `wrangler.sim-relay.jsonc`, and D1 is `callpilot-sim-relay`. Keep all alert flags
   off. Do not use bare Wrangler deployment commands or target the shared Worker.
2. Back up/check D1 and apply the additive migration through `0009` to that D1
   only. Migration must precede enabling the relay feature.
3. An authorized operator must provision/verify `APNS_TEAM_ID`, `APNS_KEY_ID`,
   `APNS_PRIVATE_KEY`, `APNS_BUNDLE_ID=ai.bondings.callpilot.sim` and
   `PUSH_TOKEN_ENCRYPTION_KEY` in the sim-relay environment. Reuse the existing
   VoIP credentials and 32-byte base64url encryption key; do not rotate the key
   casually, because it also decrypts existing VoIP tokens. This task neither
   reads nor changes these values.
4. Deploy only sim-relay with alerts still off; distribute Edge code with its flag
   off. Existing VoIP must continue to work independently.
5. Enable the relay alert flag. Build/distribute the authorized iOS build with
   `ALERT_PUSH_ENABLED=YES` and the Push Notifications entitlement. Open it and
   pair/grant notification permission so the ordinary token is registered.
6. Confirm the existing content-read prerequisites: relay `CONTENT_READ_ENABLED`,
   the Edge's content-read authorization/allowlist, and
   `REMOTE_CONTENT_READ_ENABLED`. Otherwise the alert can arrive but its target
   page cannot fetch content. Enable `REMOTE_ALERT_PUSH_ENABLED` last, on the
   intended Edge with an already working cloud control connection.
7. In a separately authorized device test, verify Chinese and English, foreground,
   locked/background and cold-launch taps, denied/re-enabled permission, unpair,
   token rotation, and real development-signed Release versus TestFlight delivery.
   Verify all three event kinds, owner-answered suppression, and VoIP coexistence.
8. Roll back by disabling relay `ALERT_PUSH_ENABLED` first, then the Edge flag;
   leave additive tables intact. Never disable/rotate shared VoIP credentials as
   an alert rollback. Already accepted APNs alerts may still be delivered until
   their one-hour expiration; an iOS-only flag rollback cannot withdraw them.

## Remaining delivery limits

Delivery is best effort, not a durable message bus: process exit, queue overflow,
five-minute offline expiry, network/APNs failure or rate limits can drop an alert.
Reservations are made before delivery and there is no retry/ack protocol, avoiding
duplicate alerts after ambiguous sends. Seven days is the replay-protection bound.
APNs acceptance is not evidence that iOS displayed a notification. Notification
permission, Focus settings and OS delivery policy remain outside the service's
control. Existing call-record creation and summary generation are prerequisites;
this change does not invent records for failures the existing content feed lacks.

Real APNs transport, signed-profile extraction on an installed device, system
permission UI, lock-screen delivery and cold-launch navigation require the
separate device acceptance gate. Windows mypy is static verification, not a
Windows runtime test. No Android files are changed.
