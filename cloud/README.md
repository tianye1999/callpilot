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

## Closed-Beta iOS incoming-call rollout

VoIP push delivery defaults off. Apply the D1 migrations and deploy the Worker
before setting any APNs secret. The feature requires six Worker settings:

```bash
npx wrangler secret put APNS_TEAM_ID
npx wrangler secret put APNS_KEY_ID
npx wrangler secret put APNS_PRIVATE_KEY
npx wrangler secret put APNS_BUNDLE_ID
npx wrangler secret put PUSH_TOKEN_ENCRYPTION_KEY
npx wrangler secret put VOIP_PUSH_ENABLED
```

`PUSH_TOKEN_ENCRYPTION_KEY` is a base64url-encoded 32-byte random key. Enter the
exact value `true` for `VOIP_PUSH_ENABLED`; missing values and all other spellings
fail closed. Keep the flag off until the signed iOS build has the VoIP entitlement
and the dedicated reviewer Edge is isolated from owner data and toll-risk calls.
Turning the flag off stops new registration and delivery. Authenticated token
removal, device unpairing, and Edge-side device revocation still delete stored
tokens while delivery is disabled.

## Physical-SIM app releases

`GET /v1/releases/latest?product=physical-sim&platform=macos` is public and
read-only. It does not read identity headers, authenticate a device, or write
audit/content records. There is no release write endpoint.

Both parameters are required, must occur exactly once, and accept 1-64 lowercase
ASCII letters/digits with single internal hyphens (for example `physical-sim`
and `macos`). Unknown parameters are rejected. Valid combinations without a row
return 404. A successful response is:

```json
{
  "product": "physical-sim",
  "platform": "macos",
  "version": "26.10.1",
  "publishedAt": 1793232000000,
  "notes": { "zh": "REPLACE_WITH_ZH_NOTES", "en": "REPLACE_WITH_EN_NOTES" },
  "downloadUrl": "https://downloads.example.com/REPLACE_WITH_ARTIFACT.dmg"
}
```

The latest row is selected by `published_at` (epoch milliseconds), not semantic
version order; equal timestamps use descending lexical `version` order as a
stable tie-breaker. Insert only published releases, not scheduled future rows.
Successful responses include `Cache-Control: public, max-age=300`. Errors retain
`Cache-Control: no-store` and the existing
`{"error":{"code":"...","message":"...","requestId":"req_..."}}` shape:
400 `VALIDATION_ERROR`, 404 `NOT_FOUND`, 405 `METHOD_NOT_ALLOWED` with `Allow: GET`,
or 500 `INTERNAL_ERROR` for storage failures or an invalid stored download URL.
Only HTTPS download URLs are returned. The migration rejects non-`https://`
prefixes; the reader additionally validates the full URL and fails closed.

### Owner-operated deployment

This is the shared US-line Worker, not a separate Physical-SIM backend. The
following are future operator steps, not part of local verification. The owner
must authorize deployment and log in with `npx wrangler login` if required.

1. From `cloud/`, run `npx wrangler deployments list` **before migrations or
   deployment**. Record the active deployment/version IDs and match them to the
   source commit in the deployment record. Confirm the local candidate is that
   deployed source plus only the approved release API changes. A deployment list
   alone does not prove source equality: stop if the commit mapping is missing,
   there is unexplained drift, or unrelated local cloud changes would be shipped.
   Preserve the previous version ID for rollback.
2. Run `npm run check` and `npx wrangler deploy --dry-run`. Inspect
   `npx wrangler d1 migrations list callpilot-cloud-beta --remote`; confirm
   migrations 0001-0008 are already applied and only the reviewed 0009 is pending.
   Stop on unexpected pending migrations. Apply the additive table/index first:
   `npx wrangler d1 migrations apply callpilot-cloud-beta --remote`.
3. Run `npx wrangler deploy` only after migration success. Verify the deployment
   ID, `/healthz`, the new endpoint (404 is expected before seeding), and the
   existing US-line enrollment/pairing/call flow using its approved acceptance
   procedure. Local tests do not replace that production acceptance gate.
4. Insert the approved release row using the example below, then GET the exact
   product/platform combination and check every field and the cache header.

Maintainers write release data directly to D1. Replace every `REPLACE_WITH_*`
value; timestamps must be unquoted epoch-millisecond integers. Use an approved,
public HTTPS artifact URL without credentials or expiring access tokens. Notes
are public, must not contain personal data, and must be SQL-escaped (a single
quote becomes two single quotes); also quote shell metacharacters safely. Do not
paste untrusted notes directly into a shell command.

```bash
npx wrangler d1 execute callpilot-cloud-beta --remote --command "INSERT INTO app_releases (product, platform, version, published_at, notes_zh, notes_en, download_url, created_at) VALUES ('physical-sim', 'macos', 'REPLACE_WITH_VERSION', REPLACE_WITH_PUBLISHED_AT_MS, 'REPLACE_WITH_ZH_NOTES', 'REPLACE_WITH_EN_NOTES', 'https://downloads.example.com/REPLACE_WITH_ARTIFACT.dmg', REPLACE_WITH_CREATED_AT_MS)"
```

The `(product, platform, version)` primary key rejects duplicate versions; this
example deliberately does not overwrite an existing row. A corrected/new row
can take up to 300 seconds to appear to clients with cached responses. HTTPS is
not an artifact authenticity check: the maintainer must verify the host, file,
and signing before publishing. If the shared Worker regresses, the owner should
roll back to the recorded previous Worker version using the normal deployment
procedure; leave the additive `app_releases` table in place. No existing tables,
credentials, feature flags, routes, or observability settings need changing.

## Rollback

The hosted mode is independently gated by `REMOTE_CLOUD_ENABLED=false`. Turning
it off and restarting restores the existing local gateway/Tunnel diagnostic path.
Do not repoint `dial.bondings.ai` away from the currently accepted path until the
staging Edge and phone flow has passed end-to-end hardware acceptance.
