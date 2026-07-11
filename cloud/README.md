# CallPilot Cloud Beta

Company-hosted control plane for issue #42. It serves the fixed Web Dialer,
registers Edge devices, pairs phones, routes one live Edge WebSocket through a
Durable Object, and signs room-scoped LiveKit credentials. It does not store or
relay call audio, SMS bodies, transcripts, or recordings.

## Local verification

```bash
cd cloud
npm ci
npm run check
npx wrangler deploy --dry-run
```

`npm run check` includes TypeScript, ESLint, unit tests, and a local Wrangler
integration flow covering one-time enrollment, pairing, Origin enforcement,
Edge WSS presence, idempotent call creation, LiveKit token brokering, and device
revocation.

## First staging deployment

Never place values in `wrangler.jsonc` or `.dev.vars.example`.

```bash
cd cloud
npx wrangler login
npx wrangler d1 create callpilot-cloud-beta
# Replace only database_id in wrangler.jsonc with the returned UUID.
npx wrangler d1 migrations apply callpilot-cloud-beta --remote
npx wrangler secret put ADMIN_TOKEN
npx wrangler secret put LIVEKIT_API_KEY
npx wrangler secret put LIVEKIT_API_SECRET
npx wrangler deploy
```

Attach both `dial.bondings.ai` and `api.bondings.ai` as Worker custom domains.
The same Worker serves static assets on the dial host and versioned API/WSS
routes on both hosts. Keep `PUBLIC_ORIGIN=https://dial.bondings.ai`; mutation
requests from any other browser Origin are rejected.

Create a Beta enrollment code through the admin endpoint without logging it:

```bash
curl --fail-with-body -X POST https://api.bondings.ai/v1/admin/enrollment-invites \
  -H "Authorization: Bearer $CALLPILOT_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"ttlSeconds":3600}'
```

The code is shown once and expires after one use. Edge credentials and device
private keys are stored by the desktop app in Keychain/Credential Manager.

## Rollback

The hosted mode is independently gated by `REMOTE_CLOUD_ENABLED=false`. Turning
it off and restarting restores the existing local gateway/Tunnel diagnostic path.
Do not repoint `dial.bondings.ai` away from the currently accepted path until the
staging Edge and phone flow has passed end-to-end hardware acceptance.

