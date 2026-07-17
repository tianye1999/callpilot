# App Review access runbook

This runbook creates review access without putting a static credential in the App,
repository, build settings, screenshots or App Review Notes.

## Prepare

1. Use a reviewer Edge that contains no real user messages, recordings or transcripts.
2. Keep outbound calling restricted to toll-safe test destinations for the review window.
3. Confirm the Edge is online and has fewer than five active paired devices.
4. Apply the current D1 migrations and deploy the matching Worker before enabling access.

## Enable and create codes

Enable the closed feature flag as a Worker secret. The value must be exactly `true`:

```sh
cd cloud
printf 'true' | npx wrangler secret put APP_REVIEW_PAIRING_ENABLED
```

From the enrolled reviewer Edge, create three independent one-time codes:

```sh
.venv/bin/python scripts/create_app_review_pairings.py --count 3 --ttl-hours 72
```

Put only the dialer URL, the codes, their expiry, and concise test steps in App Review
Notes. Do not include the Edge bearer, LiveKit keys, API keys or screenshots containing
credentials. Each code can pair one installation once.

## Revoke

Immediately after review, disable creation and invalidate every unclaimed review code:

```sh
cd cloud
printf 'false' | npx wrangler secret put APP_REVIEW_PAIRING_ENABLED
```

Then revoke every reviewer device from the Edge device list. Verify that old device
cookies receive `401`, that no review offers remain, and that the reviewer Edge has no
real user content before reusing it.

If the review device is lost, disable the flag first and revoke the device. An offline
phone cannot be remotely wiped; it clears its protected local cache when it reconnects
and receives the authorization failure. See `docs/content-sync-recovery.md`.
