# CallPilot Cloud Beta

Company-hosted control plane for issue #42. It serves the fixed Web Dialer,
registers Edge devices, pairs phones, routes one live Edge WebSocket through a
Durable Object, and signs room-scoped LiveKit credentials. When the #99 content
gate and closed-Beta allowlist are both enabled, it can relay one bounded SMS or
normalized call-history response in memory. It never stores those bodies,
transcripts, summaries, recordings, or audio in D1 or Durable Object storage.

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
npx wrangler secret put LIVEKIT_URL
npx wrangler deploy
```

The Beta staging Worker is attached to `dial-beta.bondings.ai` and
`api-beta.bondings.ai`. Attach `dial.bondings.ai` and `api.bondings.ai` only
when promoting the accepted build to production.
The same Worker serves static assets on the dial host and versioned API/WSS
routes on both hosts. Keep `PUBLIC_ORIGIN` equal to the active dialer origin
(`https://dial-beta.bondings.ai` in staging); mutation requests from any other
browser Origin are rejected.

Create a Beta enrollment code through the admin endpoint without logging it:

```bash
curl --fail-with-body -X POST https://api-beta.bondings.ai/v1/admin/enrollment-invites \
  -H "Authorization: Bearer $CALLPILOT_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"ttlSeconds":3600}'
```

The code is shown once and expires after one use. Edge credentials and device
private keys are stored by the desktop app in Keychain/Credential Manager.

## Closed-Beta content-read rollout

Content reads default off. Apply migrations first, add only an owner-approved
Edge to the server-managed allowlist, and then set the Worker flag:

```bash
npx wrangler d1 execute callpilot-cloud-beta --remote \
  --command "INSERT INTO content_read_edges(edge_id, created_at) VALUES ('edge_REPLACE_WITH_OPAQUE_ID', CAST(strftime('%s','now') AS INTEGER) * 1000)"
npx wrangler secret put CONTENT_READ_ENABLED
```

Enter the exact value `true` for the flag. Missing values and every other spelling
fail closed. Removing the allowlist row revokes both `messages:read` and
`call_records:read` for all devices paired to that Edge. The Edge independently
requires `REMOTE_CONTENT_READ_ENABLED=true`; either gate being off blocks reads.
Worker invocation observability remains explicitly disabled because request URLs
carry opaque cursors that must not enter durable platform logs or traces.

## Rollback

The hosted mode is independently gated by `REMOTE_CLOUD_ENABLED=false`. Turning
it off and restarting restores the existing local gateway/Tunnel diagnostic path.
Do not repoint `dial.bondings.ai` away from the currently accepted path until the
staging Edge and phone flow has passed end-to-end hardware acceptance.
