import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { generateKeyPairSync, sign } from "node:crypto";
import { readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

import WebSocket from "ws";

const port = 18787;
const base = `http://127.0.0.1:${port}`;
const origin = process.env.CALLPILOT_PUBLIC_ORIGIN ?? "https://dial-beta.bondings.ai";
const adminToken = "integration-admin-token-with-at-least-32-characters";
const livekitSecret = "integration-livekit-secret-with-at-least-32-characters";
const clientIp = `198.51.100.${Math.floor(Math.random() * 200) + 1}`;
const edgeKeys = generateKeyPairSync("ed25519");
const publicDer = edgeKeys.publicKey.export({ format: "der", type: "spki" });
const publicKey = publicDer.subarray(publicDer.length - 32).toString("base64url");
const cloudDir = fileURLToPath(new URL("..", import.meta.url));
const contentFixtureDir = join(cloudDir, "../docs/fixtures/content-sync/v1");
const wrangler = join(
  cloudDir,
  "node_modules",
  ".bin",
  process.platform === "win32" ? "wrangler.cmd" : "wrangler",
);

const migration = spawnSync(
  wrangler,
  ["d1", "migrations", "apply", "DB", "--local"],
  { cwd: cloudDir, encoding: "utf8" }
);
if (migration.status !== 0) throw new Error(migration.stderr || migration.stdout);

const worker = spawn(
  wrangler,
  [
    "dev", "--local", "--ip", "127.0.0.1", "--port", String(port),
    "--var", `ADMIN_TOKEN:${adminToken}`,
    "--var", "LIVEKIT_API_KEY:integration-key",
    "--var", `LIVEKIT_API_SECRET:${livekitSecret}`,
    "--var", "LIVEKIT_URL:wss://integration.livekit.cloud",
    "--var", "CONTENT_READ_ENABLED:true",
    "--var", "CONTENT_RELAY_TIMEOUT_MS:500",
    "--var", "CONTENT_READ_DEVICE_LIMIT:100",
    "--var", "CONTENT_READ_EDGE_LIMIT:200"
  ],
  { cwd: cloudDir, stdio: ["ignore", "pipe", "pipe"] }
);

let workerOutput = "";
worker.stdout.on("data", (chunk) => { workerOutput += chunk; });
worker.stderr.on("data", (chunk) => { workerOutput += chunk; });

try {
  await waitForHealth();

  const pageResponse = await fetch(`${base}/`);
  assert.equal(pageResponse.status, 200);
  const csp = pageResponse.headers.get("content-security-policy") ?? "";
  assert.match(
    csp,
    /connect-src 'self' https:\/\/integration\.livekit\.cloud wss:\/\/integration\.livekit\.cloud;/,
  );
  assert.doesNotMatch(csp, /https:\/\/\*\.livekit\.cloud/);
  assert.doesNotMatch(csp, /(?:^|\s)wss:(?:\s|;|$)/);

  const unauthorized = await fetch(`${base}/v1/admin/enrollment-invites`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ttlSeconds: 600 })
  });
  assert.equal(unauthorized.status, 401);

  const invite = await post("/v1/admin/enrollment-invites", { ttlSeconds: 600 }, {
    Authorization: `Bearer ${adminToken}`
  });
  assert.match(invite.code, /^[A-Za-z0-9_-]{40,}$/);

  const enrollment = await post("/v1/edge-enrollments/claim", {
    code: invite.code,
    displayName: "Integration Edge",
    publicKey
  }, { "CF-Connecting-IP": clientIp });
  assert.match(enrollment.edgeId, /^edge_/);
  assert.doesNotMatch(JSON.stringify(enrollment), new RegExp(livekitSecret));

  const reusedEnrollment = await fetch(`${base}/v1/edge-enrollments/claim`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "CF-Connecting-IP": clientIp },
    body: JSON.stringify({
      code: invite.code,
      displayName: "Replay",
      publicKey
    })
  });
  assert.equal(reusedEnrollment.status, 401);

  const pairing = await post(`/v1/edges/${enrollment.edgeId}/pairing-sessions`, { ttlSeconds: 300 }, {
    ...edgeHeaders("POST", `/v1/edges/${enrollment.edgeId}/pairing-sessions`, enrollment)
  });
  assert.match(pairing.code, /^[23456789A-HJ-NP-Z]{4}-[23456789A-HJ-NP-Z]{4}$/);

  const pairResponse = await fetch(`${base}/v1/pairing-sessions/claim`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: origin, "CF-Connecting-IP": clientIp },
    body: JSON.stringify({ code: pairing.code, displayName: "Integration Phone" })
  });
  assert.equal(pairResponse.status, 201);
  const cookie = pairResponse.headers.get("set-cookie")?.split(";", 1)[0];
  assert.ok(cookie?.startsWith("__Host-callpilot-device="));
  const paired = await pairResponse.json();

  // Pairing alone grants no content capability. The closed-Beta allowlist is
  // server-managed D1 authorization metadata, not a phone-controlled field.
  const deniedBeforeGrant = await fetch(`${base}/v1/messages`, { headers: { Cookie: cookie } });
  assert.equal(deniedBeforeGrant.status, 403);
  assert.equal((await deniedBeforeGrant.json()).error.code, "FORBIDDEN");
  const grant = spawnSync(
    wrangler,
    [
      "d1", "execute", "DB", "--local",
      "--command", `INSERT OR REPLACE INTO content_read_edges(edge_id, created_at) VALUES ('${enrollment.edgeId}', ${Date.now()})`
    ],
    { cwd: cloudDir, encoding: "utf8" }
  );
  if (grant.status !== 0) throw new Error(grant.stderr || grant.stdout);

  const deviceWithCapabilities = await fetch(`${base}/v1/device`, { headers: { Cookie: cookie } });
  assert.equal(deviceWithCapabilities.status, 200);
  assert.deepEqual((await deviceWithCapabilities.json()).capabilities, [
    "messages:read", "call_records:read"
  ]);

  const untrustedOrigin = await fetch(`${base}/v1/calls`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: cookie, Origin: "https://evil.example" },
    body: JSON.stringify({ edgeId: enrollment.edgeId, idempotencyKey: "integration-call-untrusted" })
  });
  assert.equal(untrustedOrigin.status, 403);

  let socket = new WebSocket(`${base.replace("http", "ws")}/v1/edges/connect`, {
    headers: edgeHeaders("GET", "/v1/edges/connect", enrollment)
  });
  await new Promise((resolve, reject) => {
    socket.once("open", resolve);
    socket.once("error", reject);
  });
  socket.send(JSON.stringify({
    v: 1,
    type: "heartbeat",
    occurredAt: new Date().toISOString(),
    status: { modemOnline: true, lineBusy: false, version: "integration" }
  }));
  await delay(100);

  const idempotencyKey = "integration-call-idempotency-0001";
  const commandPromise = nextMessage(socket);
  const call = await post("/v1/calls", { edgeId: enrollment.edgeId, idempotencyKey }, {
    Cookie: cookie,
    Origin: origin
  }, 202);
  const command = await commandPromise;
  assert.equal(command.type, "session.start");
  assert.equal(command.callId, call.callId);
  assert.match(command.session.token, /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);

  socket.send(JSON.stringify({
    v: 1,
    type: "command.ack",
    commandId: command.commandId,
    callId: command.callId,
    status: "accepted"
  }));
  await delay(100);

  const readyResponse = await fetch(`${base}/v1/calls/${call.callId}`, { headers: { Cookie: cookie } });
  assert.equal(readyResponse.status, 200);
  const ready = await readyResponse.json();
  assert.equal(ready.status, "ready");
  assert.equal(ready.session.livekitUrl, "wss://integration.livekit.cloud");
  assert.match(ready.session.token, /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);

  const duplicate = await post("/v1/calls", { edgeId: enrollment.edgeId, idempotencyKey }, {
    Cookie: cookie,
    Origin: origin
  });
  assert.equal(duplicate.callId, call.callId);

  const rejectedCommandPromise = nextMessage(socket);
  const rejectedCall = await post("/v1/calls", {
    edgeId: enrollment.edgeId,
    idempotencyKey: "integration-call-rejected-0002"
  }, {
    Cookie: cookie,
    Origin: origin
  }, 202);
  const rejectedCommand = await rejectedCommandPromise;
  socket.send(JSON.stringify({
    v: 1,
    type: "command.ack",
    commandId: rejectedCommand.commandId,
    callId: rejectedCommand.callId,
    status: "rejected",
    errorCode: "SIM_NOT_REGISTERED"
  }));
  await delay(100);

  const rejectedResponse = await fetch(`${base}/v1/calls/${rejectedCall.callId}`, {
    headers: { Cookie: cookie }
  });
  assert.equal(rejectedResponse.status, 200);
  const rejected = await rejectedResponse.json();
  assert.equal(rejected.status, "failed");
  assert.equal(rejected.errorCode, "SIM_NOT_REGISTERED");

  // ---- Inbound takeover offers (#95) ----
  // D1 local persists across runs and offers are insert-once by design, so the
  // ids must be unique per run or a previous run's row swallows the INSERT.
  const runTag = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  const offerId = `offer_${runTag}a001`;
  socket.send(JSON.stringify({
    v: 1,
    type: "inbound.offer",
    offerId,
    callId: `call_${runTag}c001`,
    generation: 3,
    nonce: "integration-nonce-0001",
    expiresAtUnixMs: Date.now() + 60_000
  }));
  const offers = await eventuallyOffers(
    cookie,
    (payload) => payload.offers.length === 1 && payload.offers[0].offerId === offerId,
    "offer did not appear",
  );
  // Privacy: polling exposes only opaque offer ids — never nonce or call id.
  assert.equal(JSON.stringify(offers).includes("nonce"), false);
  assert.equal(JSON.stringify(offers).includes(`call_${runTag}`), false);

  const claimCommandPromise = nextMessage(socket);
  const claim = await post("/v1/inbound-offers/claim", { offerId }, {
    Cookie: cookie,
    Origin: origin
  }, 202);
  assert.match(claim.claimId, /^claim_/);
  assert.match(claim.token, /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);
  const claimCommand = await claimCommandPromise;
  assert.equal(claimCommand.type, "inbound.claim");
  assert.equal(claimCommand.offerId, offerId);
  assert.equal(claimCommand.claimId, claim.claimId);
  assert.equal(claimCommand.generation, 3);
  assert.equal(claimCommand.nonce, "integration-nonce-0001");
  assert.match(claimCommand.session.browserIdentity, /^web_/);
  assert.match(claimCommand.session.token, /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);

  // First-claim-wins: the second claim must lose deterministically.
  const doubleClaim = await fetch(`${base}/v1/inbound-offers/claim`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: cookie, Origin: origin },
    body: JSON.stringify({ offerId })
  });
  assert.equal(doubleClaim.status, 409);

  socket.send(JSON.stringify({
    v: 1,
    type: "command.ack",
    commandId: claimCommand.commandId,
    callId: claimCommand.callId,
    offerId,
    status: "accepted"
  }));
  await delay(100);

  // Revoke path: a second offer withdrawn by the Edge disappears from polling.
  const offerId2 = `offer_${runTag}a002`;
  socket.send(JSON.stringify({
    v: 1,
    type: "inbound.offer",
    offerId: offerId2,
    callId: `call_${runTag}c002`,
    generation: 4,
    nonce: "integration-nonce-0002",
    expiresAtUnixMs: Date.now() + 60_000
  }));
  await eventuallyOffers(
    cookie,
    (payload) => payload.offers.some((o) => o.offerId === offerId2),
    "second offer did not appear",
  );
  socket.send(JSON.stringify({
    v: 1,
    type: "inbound.offer.revoke",
    offerId: offerId2,
    callId: `call_${runTag}c002`,
    reason: "CALL_ENDED"
  }));
  await eventuallyOffers(
    cookie,
    (payload) => payload.offers.length === 0,
    "revoked offer still listed",
  );

  // ---- Read-only content relay (#99) ----
  const messages = contentFixture("messages-page.json");
  const messagesResponse = await contentRead(
    socket,
    cookie,
    "/v1/messages?limit=3&cursor=cursor_messages_fixture_0001",
    "messages.list",
    messages,
    { limit: 3, cursor: "cursor_messages_fixture_0001" }
  );
  assert.deepEqual(messagesResponse, messages);

  const calls = contentFixture("call-records-page.json");
  assert.deepEqual(await contentRead(
    socket, cookie, "/v1/call-records", "call_records.list", calls,
    { limit: 25, cursor: null }
  ), calls);

  const detail = contentFixture("call-record-detail-ready.json");
  assert.deepEqual(await contentRead(
    socket,
    cookie,
    "/v1/call-records/call_fixture_pending_0001",
    "call_records.get",
    detail,
    { callId: "call_fixture_pending_0001" }
  ), detail);

  const timelinePage = contentFixture("call-timeline-page.json");
  assert.deepEqual(await contentRead(
    socket,
    cookie,
    "/v1/call-records/call_fixture_agent_0001/timeline",
    "call_timeline.list",
    timelinePage,
    { callId: "call_fixture_agent_0001", limit: 50, cursor: null }
  ), timelinePage);

  const exactLimitCommandPromise = nextMessage(socket);
  const exactLimitFetch = fetch(`${base}/v1/messages`, { headers: { Cookie: cookie } });
  const exactLimitCommand = await exactLimitCommandPromise;
  const exactLimitBody = contentFixture("edge-data-response.json").body;
  exactLimitBody.items[0].text = "";
  const exactLimitEnvelope = {
    v: 1,
    type: "data.response",
    requestId: exactLimitCommand.requestId,
    resource: exactLimitCommand.resource,
    status: "ok",
    body: exactLimitBody
  };
  const exactOverhead = Buffer.byteLength(JSON.stringify(exactLimitEnvelope), "utf8");
  exactLimitBody.items[0].text = "x".repeat(16_384 - exactOverhead);
  const exactLimitWire = JSON.stringify(exactLimitEnvelope);
  assert.equal(Buffer.byteLength(exactLimitWire, "utf8"), 16_384);
  socket.send(exactLimitWire);
  const exactLimitResponse = await exactLimitFetch;
  assert.equal(exactLimitResponse.status, 200);
  assert.equal((await exactLimitResponse.json()).items[0].text.length, 16_384 - exactOverhead);

  const invalidIdentityOverride = await fetch(
    `${base}/v1/messages?edgeId=edge_abcdefghijkl`,
    { headers: { Cookie: cookie } }
  );
  assert.equal(invalidIdentityOverride.status, 400);
  const invalidIdentityBody = await invalidIdentityOverride.json();
  assert.equal(invalidIdentityBody.error.code, "INVALID_REQUEST");
  assert.match(invalidIdentityBody.error.requestId, /^request_/);

  // A response with the right requestId but wrong resource cannot satisfy the
  // pending read. The later matching response still completes it.
  const fencedCommandPromise = nextMessage(socket);
  let fencedSettled = false;
  const fencedFetch = fetch(`${base}/v1/messages`, { headers: { Cookie: cookie } })
    .finally(() => { fencedSettled = true; });
  const fencedCommand = await fencedCommandPromise;
  socket.send(JSON.stringify({
    v: 1,
    type: "data.response",
    requestId: fencedCommand.requestId,
    resource: "call_records.list",
    status: "ok",
    body: calls
  }));
  await delay(100);
  assert.equal(fencedSettled, false);
  const correctFencedResponse = {
    v: 1,
    type: "data.response",
    requestId: fencedCommand.requestId,
    resource: fencedCommand.resource,
    status: "ok",
    body: messages
  };
  socket.send(JSON.stringify(correctFencedResponse));
  const fencedResponse = await fencedFetch;
  assert.equal(fencedResponse.status, 200);
  // Duplicate/unsolicited response is discarded and cannot poison the next read.
  socket.send(JSON.stringify(correctFencedResponse));

  const cursorCommandPromise = nextMessage(socket);
  const cursorFetch = fetch(`${base}/v1/messages?cursor=cursor_messages_fixture_bad1`, {
    headers: { Cookie: cookie }
  });
  const cursorCommand = await cursorCommandPromise;
  socket.send(JSON.stringify({
    v: 1,
    type: "data.response",
    requestId: cursorCommand.requestId,
    resource: cursorCommand.resource,
    status: "error",
    error: { code: "CURSOR_INVALID" }
  }));
  const cursorResponse = await cursorFetch;
  assert.equal(cursorResponse.status, 400);
  assert.equal((await cursorResponse.json()).error.code, "CURSOR_INVALID");

  const oversizeCommandPromise = nextMessage(socket);
  const oversizeFetch = fetch(`${base}/v1/messages`, { headers: { Cookie: cookie } });
  const oversizeCommand = await oversizeCommandPromise;
  socket.send(JSON.stringify({
    v: 1,
    type: "data.response",
    requestId: oversizeCommand.requestId,
    resource: oversizeCommand.resource,
    status: "error",
    error: { code: "PAYLOAD_TOO_LARGE" }
  }));
  const oversizeResponse = await oversizeFetch;
  assert.equal(oversizeResponse.status, 413);
  assert.equal((await oversizeResponse.json()).error.code, "PAYLOAD_TOO_LARGE");

  const actualOversizeCommandPromise = nextMessage(socket);
  const actualOversizeFetch = fetch(`${base}/v1/messages`, { headers: { Cookie: cookie } });
  const actualOversizeCommand = await actualOversizeCommandPromise;
  const actualOversizeBody = contentFixture("edge-data-response.json").body;
  actualOversizeBody.items[0].text = "x".repeat(16_384);
  const actualOversizeClosed = new Promise((resolve) => socket.once("close", resolve));
  socket.send(JSON.stringify({
    v: 1,
    type: "data.response",
    requestId: actualOversizeCommand.requestId,
    resource: actualOversizeCommand.resource,
    status: "ok",
    body: actualOversizeBody
  }));
  const actualOversizeResponse = await actualOversizeFetch;
  assert.equal(actualOversizeResponse.status, 413);
  assert.equal((await actualOversizeResponse.json()).error.code, "PAYLOAD_TOO_LARGE");
  await actualOversizeClosed;
  socket = new WebSocket(`${base.replace("http", "ws")}/v1/edges/connect`, {
    headers: edgeHeaders("GET", "/v1/edges/connect", enrollment)
  });
  await new Promise((resolve, reject) => {
    socket.once("open", resolve);
    socket.once("error", reject);
  });

  const binaryCommandPromise = nextMessage(socket);
  const binaryFetch = fetch(`${base}/v1/messages`, { headers: { Cookie: cookie } });
  await binaryCommandPromise;
  const binaryClosed = new Promise((resolve) => socket.once("close", resolve));
  socket.send(Buffer.from([0x01]));
  const binaryResponse = await binaryFetch;
  assert.equal(binaryResponse.status, 503);
  assert.equal((await binaryResponse.json()).error.code, "EDGE_OFFLINE");
  await binaryClosed;
  socket = new WebSocket(`${base.replace("http", "ws")}/v1/edges/connect`, {
    headers: edgeHeaders("GET", "/v1/edges/connect", enrollment)
  });
  await new Promise((resolve, reject) => {
    socket.once("open", resolve);
    socket.once("error", reject);
  });

  const timeoutCommandPromise = nextMessage(socket);
  const timeoutFetch = fetch(`${base}/v1/messages`, { headers: { Cookie: cookie } });
  const timeoutCommand = await timeoutCommandPromise;
  const timeoutResponse = await timeoutFetch;
  assert.equal(timeoutResponse.status, 504);
  assert.equal((await timeoutResponse.json()).error.code, "TIMEOUT");
  socket.send(JSON.stringify({
    v: 1,
    type: "data.response",
    requestId: timeoutCommand.requestId,
    resource: timeoutCommand.resource,
    status: "ok",
    body: messages
  }));
  const afterLate = await contentRead(
    socket, cookie, "/v1/messages", "messages.list", messages,
    { limit: 25, cursor: null }
  );
  assert.deepEqual(afterLate, messages);

  // A new authenticated Edge connection fences the socket that received a
  // pending request; an old-socket response can never complete it.
  const replacementCommandPromise = nextMessage(socket);
  const replacementFetch = fetch(`${base}/v1/messages`, { headers: { Cookie: cookie } });
  await replacementCommandPromise;
  const replacementSocket = new WebSocket(`${base.replace("http", "ws")}/v1/edges/connect`, {
    headers: edgeHeaders("GET", "/v1/edges/connect", enrollment)
  });
  await new Promise((resolve, reject) => {
    replacementSocket.once("open", resolve);
    replacementSocket.once("error", reject);
  });
  const replacedResponse = await replacementFetch;
  assert.equal(replacedResponse.status, 503);
  assert.equal((await replacedResponse.json()).error.code, "EDGE_OFFLINE");
  socket = replacementSocket;

  // No connected socket produces the content-specific 503 contract. Reconnect
  // afterwards so the in-flight revocation race can be exercised independently.
  const socketClosed = new Promise((resolve) => socket.once("close", resolve));
  socket.close();
  await socketClosed;
  const offlineResponse = await fetch(`${base}/v1/messages`, { headers: { Cookie: cookie } });
  assert.equal(offlineResponse.status, 503);
  assert.equal((await offlineResponse.json()).error.code, "EDGE_OFFLINE");

  socket = new WebSocket(`${base.replace("http", "ws")}/v1/edges/connect`, {
    headers: edgeHeaders("GET", "/v1/edges/connect", enrollment)
  });
  await new Promise((resolve, reject) => {
    socket.once("open", resolve);
    socket.once("error", reject);
  });

  // Revocation after dispatch but before delivery must discard the accepted
  // plaintext body and return authorization failure.
  const revokeRaceCommandPromise = nextMessage(socket);
  const revokeRaceFetch = fetch(`${base}/v1/messages`, { headers: { Cookie: cookie } });
  const revokeRaceCommand = await revokeRaceCommandPromise;

  const revoke = await fetch(`${base}/v1/devices/${paired.device.deviceId}`, {
    method: "DELETE",
    headers: edgeHeaders("DELETE", `/v1/devices/${paired.device.deviceId}`, enrollment)
  });
  assert.equal(revoke.status, 200);
  socket.send(JSON.stringify({
    v: 1,
    type: "data.response",
    requestId: revokeRaceCommand.requestId,
    resource: revokeRaceCommand.resource,
    status: "ok",
    body: messages
  }));
  const revokedInFlight = await revokeRaceFetch;
  assert.equal(revokedInFlight.status, 401);
  assert.equal((await revokedInFlight.json()).error.code, "UNAUTHORIZED");
  const afterRevoke = await fetch(`${base}/api/device`, { headers: { Cookie: cookie } });
  assert.deepEqual(await afterRevoke.json(), { ok: true, paired: false });

  const revokedContentRead = await fetch(`${base}/v1/messages`, {
    headers: { Cookie: cookie }
  });
  assert.equal(revokedContentRead.status, 401);
  assert.equal((await revokedContentRead.json()).error.code, "UNAUTHORIZED");

  // Losing a phone must not strand the owner's Edge. The Edge creates a fresh
  // pairing session, a replacement phone receives a different credential, and
  // the Edge-level content grant remains effective. The revoked credential stays
  // invalid throughout and is never revived by the replacement pairing.
  const recoveryPairing = await post(
    `/v1/edges/${enrollment.edgeId}/pairing-sessions`,
    { ttlSeconds: 300 },
    edgeHeaders(
      "POST",
      `/v1/edges/${enrollment.edgeId}/pairing-sessions`,
      enrollment
    )
  );
  assert.match(recoveryPairing.code, /^[23456789A-HJ-NP-Z]{4}-[23456789A-HJ-NP-Z]{4}$/);

  const recoveryPairResponse = await fetch(`${base}/v1/pairing-sessions/claim`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: origin,
      "CF-Connecting-IP": clientIp
    },
    body: JSON.stringify({ code: recoveryPairing.code, displayName: "Replacement Phone" })
  });
  assert.equal(recoveryPairResponse.status, 201);
  const recoveryCookie = recoveryPairResponse.headers.get("set-cookie")?.split(";", 1)[0];
  assert.ok(recoveryCookie?.startsWith("__Host-callpilot-device="));
  const recoveryPaired = await recoveryPairResponse.json();
  assert.notEqual(recoveryPaired.device.deviceId, paired.device.deviceId);

  const recoveredDevice = await fetch(`${base}/v1/device`, {
    headers: { Cookie: recoveryCookie }
  });
  assert.equal(recoveredDevice.status, 200);
  const recoveredDeviceBody = await recoveredDevice.json();
  assert.equal(recoveredDeviceBody.paired, true);
  assert.equal(recoveredDeviceBody.device.edgeId, enrollment.edgeId);
  assert.deepEqual(recoveredDeviceBody.capabilities, [
    "messages:read", "call_records:read"
  ]);

  const recoveredMessages = await contentRead(
    socket,
    recoveryCookie,
    "/v1/messages",
    "messages.list",
    messages,
    { limit: 25, cursor: null }
  );
  assert.deepEqual(recoveredMessages, messages);

  const stillRevoked = await fetch(`${base}/v1/messages`, {
    headers: { Cookie: cookie }
  });
  assert.equal(stillRevoked.status, 401);
  assert.equal((await stillRevoked.json()).error.code, "UNAUTHORIZED");

  // Application output and every local D1 row remain free of relayed content.
  // Audit rows hold only opaque actor ids plus the closed resource discriminator.
  assert.doesNotMatch(workerOutput, /Synthetic notice fragment|verification value redacted/);
  const dumpPath = join(tmpdir(), `callpilot-cloud-content-${process.pid}-${Date.now()}.sql`);
  const exported = spawnSync(
    wrangler,
    ["d1", "export", "DB", "--local", "--output", dumpPath],
    { cwd: cloudDir, encoding: "utf8" }
  );
  if (exported.status !== 0) throw new Error(exported.stderr || exported.stdout);
  const durableDump = readFileSync(dumpPath, "utf8");
  rmSync(dumpPath, { force: true });
  assert.doesNotMatch(
    durableDump,
    /Synthetic notice fragment|verification value redacted|Hello\. Who are you calling for|cursor_messages_fixture_0001/
  );

  socket.close();
  console.log("cloud integration: passed");
} catch (error) {
  console.error(workerOutput);
  throw error;
} finally {
  await stopWorker(worker);
}

async function eventuallyOffers(cookie, predicate, label) {
  const deadline = Date.now() + 2_000;
  let last;
  while (Date.now() < deadline) {
    const response = await fetch(`${base}/v1/inbound-offers`, { headers: { Cookie: cookie } });
    assert.equal(response.status, 200);
    last = await response.json();
    if (predicate(last)) return last;
    await delay(100);
  }
  throw new Error(`${label}: ${JSON.stringify(last)}`);
}

function contentFixture(name) {
  return JSON.parse(readFileSync(join(contentFixtureDir, name), "utf8"));
}

async function contentRead(socket, cookie, path, resource, body, expectedParams) {
  const commandPromise = nextMessage(socket);
  const responsePromise = fetch(`${base}${path}`, { headers: { Cookie: cookie } });
  const command = await commandPromise;
  assert.equal(command.v, 1);
  assert.equal(command.type, "data.request");
  assert.match(command.requestId, /^request_/);
  assert.equal(command.deviceId.startsWith("device_"), true);
  assert.equal(command.resource, resource);
  assert.deepEqual(command.params, expectedParams);
  assert.equal(command.expiresAtUnixMs > command.issuedAtUnixMs, true);
  assert.equal(command.expiresAtUnixMs - command.issuedAtUnixMs <= 10_000, true);
  socket.send(JSON.stringify({
    v: 1,
    type: "data.response",
    requestId: command.requestId,
    resource,
    status: "ok",
    body
  }));
  const response = await responsePromise;
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("cache-control"), "no-store");
  return response.json();
}

async function waitForHealth() {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const response = await fetch(`${base}/healthz`);
      if (response.ok) return;
    } catch {}
    await delay(100);
  }
  throw new Error("wrangler dev did not become ready");
}

async function post(path, body, extraHeaders = {}, expectedStatus = undefined) {
  const response = await fetch(`${base}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...extraHeaders },
    body: JSON.stringify(body)
  });
  if (expectedStatus !== undefined) assert.equal(response.status, expectedStatus);
  const payload = await response.json();
  if (!response.ok) throw new Error(`${path}: ${response.status} ${JSON.stringify(payload)}`);
  return payload;
}

function nextMessage(socket) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("WebSocket command timeout")), 3000);
    socket.once("message", (data) => {
      clearTimeout(timeout);
      resolve(JSON.parse(data.toString()));
    });
  });
}

function edgeHeaders(method, path, enrollment) {
  const timestamp = String(Date.now());
  const message = `${enrollment.edgeId}\n${timestamp}\n${method}\n${path}`;
  const signature = sign(null, Buffer.from(message), edgeKeys.privateKey).toString("base64url");
  return {
    Authorization: `Bearer ${enrollment.credential}`,
    "X-CallPilot-Timestamp": timestamp,
    "X-CallPilot-Signature": signature,
  };
}

async function stopWorker(process) {
  if (process.exitCode !== null) return;
  const closed = new Promise((resolve) => process.once("close", resolve));
  process.kill("SIGTERM");
  await Promise.race([closed, delay(3000)]);
  if (process.exitCode === null) {
    process.kill("SIGKILL");
    await closed;
  }
}
