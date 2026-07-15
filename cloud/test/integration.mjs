import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { generateKeyPairSync, sign } from "node:crypto";
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
    "--var", "LIVEKIT_URL:wss://integration.livekit.cloud"
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

  const untrustedOrigin = await fetch(`${base}/v1/calls`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: cookie, Origin: "https://evil.example" },
    body: JSON.stringify({ edgeId: enrollment.edgeId, idempotencyKey: "integration-call-untrusted" })
  });
  assert.equal(untrustedOrigin.status, 403);

  const socket = new WebSocket(`${base.replace("http", "ws")}/v1/edges/connect`, {
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

  const revoke = await fetch(`${base}/v1/devices/${paired.device.deviceId}`, {
    method: "DELETE",
    headers: edgeHeaders("DELETE", `/v1/devices/${paired.device.deviceId}`, enrollment)
  });
  assert.equal(revoke.status, 200);
  const afterRevoke = await fetch(`${base}/api/device`, { headers: { Cookie: cookie } });
  assert.deepEqual(await afterRevoke.json(), { ok: true, paired: false });

  socket.close();
  console.log("cloud integration: passed");
} catch (error) {
  console.error(workerOutput);
  throw error;
} finally {
  await stopWorker(worker);
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
