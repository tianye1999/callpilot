import { authenticateDevice, authenticateEdge } from "./auth";
import {
  contentReadEnabled,
  contentRelayTimeoutMs,
  enforceContentRateLimit
} from "./content-access";
import {
  CONTENT_CAPABILITIES,
  CONTENT_WIRE_LIMIT_BYTES,
  contentCursorSchema,
  dataResponseSchema,
  responseMatchesRequest,
  serializedByteLength,
  type ContentErrorCode,
  type ContentResource,
  type DataRequest
} from "./content-sync";
import { liveKitConnectSources } from "./csp";
import { EdgeRoom } from "./edge-room";
import { error, HttpError, json, readJson, requireSameOrigin } from "./http";
import { issueParticipantToken } from "./livekit";
import {
  claimEnrollmentSchema,
  claimInboundOfferSchema,
  claimPairingSchema,
  createCallSchema,
  createEnrollmentInviteSchema,
  createPairingSchema
} from "./schemas";
import { constantTimeEqual, randomId, randomPairingCode, randomSecret, sha256 } from "./security";
import type { CallRecord, DeviceRecord, Env, InboundOfferRecord } from "./types";
import { ZodError } from "zod";

export { EdgeRoom };

const COOKIE_MAX_AGE = 180 * 24 * 60 * 60;

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const path = new URL(request.url).pathname;
    const requestId = randomId(
      path.startsWith("/v1/messages") || path.startsWith("/v1/call-records") ? "request" : "req",
      12
    );
    try {
      return await route(request, env, requestId);
    } catch (caught) {
      if (caught instanceof HttpError) {
        return error(caught.code, caught.message, caught.status, requestId, caught.headers);
      }
      if (caught instanceof ZodError) return error("VALIDATION_ERROR", "Request fields are invalid", 400, requestId);
      console.error(JSON.stringify({ requestId, errorType: caught instanceof Error ? caught.name : "unknown" }));
      return error("INTERNAL_ERROR", "The request could not be completed", 500, requestId);
    }
  }
};

async function route(request: Request, env: Env, requestId: string): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/$/, "") || "/";

  if (path === "/healthz" && request.method === "GET") return json({ ok: true });
  if (path === "/v1/admin/enrollment-invites" && request.method === "POST") {
    return createEnrollmentInvite(request, env);
  }
  if (path === "/v1/edge-enrollments/claim" && request.method === "POST") {
    return claimEnrollment(request, env);
  }
  if (path === "/v1/edges/connect" && request.method === "GET") return connectEdge(request, env);
  if (path === "/v1/pairing-sessions/claim" && request.method === "POST") {
    return claimPairing(request, env);
  }
  if ((path === "/api/device" || path === "/v1/device") && request.method === "GET") {
    return getDevice(request, env);
  }
  if (path === "/v1/device" && request.method === "DELETE") return unpairCurrentDevice(request, env);
  if (path === "/v1/calls" && request.method === "POST") return createCall(request, env);
  if (path === "/v1/inbound-offers" && request.method === "GET") {
    return listInboundOffers(request, env);
  }
  if (path === "/v1/inbound-offers/claim" && request.method === "POST") {
    return claimInboundOffer(request, env);
  }
  if (path === "/v1/messages" && request.method === "GET") {
    return relayContentRead(request, env, "messages.list", undefined);
  }
  if (path === "/v1/call-records" && request.method === "GET") {
    return relayContentRead(request, env, "call_records.list", undefined);
  }

  const timeline = path.match(
    /^\/v1\/call-records\/(call_[A-Za-z0-9_-]{12,80})\/timeline$/
  );
  if (timeline && request.method === "GET") {
    return relayContentRead(request, env, "call_timeline.list", timeline[1]);
  }
  const callRecord = path.match(/^\/v1\/call-records\/(call_[A-Za-z0-9_-]{12,80})$/);
  if (callRecord && request.method === "GET") {
    return relayContentRead(request, env, "call_records.get", callRecord[1]);
  }
  if (path.startsWith("/v1/call-records/") && request.method === "GET") {
    throw new HttpError("INVALID_REQUEST", "The request fields are invalid", 400);
  }

  const presence = path.match(/^\/v1\/edges\/(edge_[A-Za-z0-9_-]+)\/presence$/);
  if (presence && request.method === "GET") return getPresence(request, env, presence[1] ?? "");
  const pairing = path.match(/^\/v1\/edges\/(edge_[A-Za-z0-9_-]+)\/pairing-sessions$/);
  if (pairing && request.method === "POST") return createPairing(request, env, pairing[1] ?? "");
  const devices = path.match(/^\/v1\/edges\/(edge_[A-Za-z0-9_-]+)\/devices$/);
  if (devices && request.method === "GET") return listDevices(request, env, devices[1] ?? "");
  const device = path.match(/^\/v1\/devices\/(device_[A-Za-z0-9_-]+)$/);
  if (device && request.method === "DELETE") return revokeDevice(request, env, device[1] ?? "");
  const call = path.match(/^\/v1\/calls\/(call_[A-Za-z0-9_-]+)$/);
  if (call && request.method === "GET") return getCall(request, env, call[1] ?? "");

  if (path.startsWith("/v1/") || path.startsWith("/api/")) {
    return error("NOT_FOUND", "Resource not found", 404, requestId);
  }
  return secureAsset(await env.ASSETS.fetch(request), env);
}

async function createEnrollmentInvite(request: Request, env: Env): Promise<Response> {
  const header = request.headers.get("Authorization") ?? "";
  if (!env.ADMIN_TOKEN || !await constantTimeEqual(header, `Bearer ${env.ADMIN_TOKEN}`)) {
    throw new HttpError("UNAUTHORIZED", "Administrator credential is invalid", 401);
  }
  const input = createEnrollmentInviteSchema.parse(await readJson(request));
  const code = randomSecret(32);
  const now = Date.now();
  await env.DB.prepare(
    "INSERT INTO enrollment_invites(invite_hash, expires_at, created_at) VALUES (?1, ?2, ?3)"
  ).bind(await sha256(code), now + input.ttlSeconds * 1000, now).run();
  return json({ code, expiresAt: now + input.ttlSeconds * 1000 }, 201);
}

async function claimEnrollment(request: Request, env: Env): Promise<Response> {
  await enforceRateLimit(env, request, "enrollment", 10, 300_000);
  const input = claimEnrollmentSchema.parse(await readJson(request));
  const now = Date.now();
  const inviteHash = await sha256(input.code);
  const claimed = await env.DB.prepare(
    "UPDATE enrollment_invites SET used_at = ?1 WHERE invite_hash = ?2 AND used_at IS NULL AND expires_at > ?1 RETURNING invite_hash"
  ).bind(now, inviteHash).first<{ invite_hash: string }>();
  if (!claimed) throw new HttpError("INVALID_ENROLLMENT", "Enrollment code is invalid or expired", 401);

  const edgeId = randomId("edge");
  const secret = randomSecret();
  await env.DB.prepare(
    "INSERT INTO edges(edge_id, display_name, public_key, secret_hash, created_at) VALUES (?1, ?2, ?3, ?4, ?5)"
  ).bind(edgeId, input.displayName, input.publicKey, await sha256(secret), now).run();
  await audit(env, "beta_invite", "enrollment", "edge.enrolled", edgeId, "allowed");
  return json({ edgeId, credential: `${edgeId}.${secret}` }, 201);
}

async function connectEdge(request: Request, env: Env): Promise<Response> {
  const edge = await authenticateEdge(request, env);
  const stub = edgeRoom(env, edge.edge_id);
  const headers = new Headers(request.headers);
  headers.set("X-CallPilot-Edge-Id", edge.edge_id);
  return stub.fetch(new Request("https://edge-room/connect", { method: "GET", headers }));
}

async function getPresence(request: Request, env: Env, edgeId: string): Promise<Response> {
  const actor = await authenticateActorForEdge(request, env, edgeId);
  const response = await edgeRoom(env, edgeId).fetch("https://edge-room/presence");
  const presence = await response.json();
  return json({ edgeId, presence, actor: actor.kind });
}

async function createPairing(request: Request, env: Env, edgeId: string): Promise<Response> {
  const edge = await authenticateEdge(request, env);
  if (edge.edge_id !== edgeId) throw new HttpError("FORBIDDEN", "Edge does not own this resource", 403);
  const input = createPairingSchema.parse(await readJson(request));
  const active = await env.DB.prepare(
    "SELECT COUNT(*) AS count FROM devices WHERE edge_id = ?1 AND revoked_at IS NULL"
  ).bind(edgeId).first<{ count: number }>();
  if ((active?.count ?? 0) >= 5) throw new HttpError("DEVICE_LIMIT", "Paired device limit reached", 409);

  const pairingId = randomId("pairing");
  const code = randomPairingCode();
  const now = Date.now();
  await env.DB.prepare(
    "INSERT INTO pairing_sessions(pairing_id, edge_id, code_hash, expires_at, created_at) VALUES (?1, ?2, ?3, ?4, ?5)"
  ).bind(pairingId, edgeId, await sha256(normalizePairingCode(code)), now + input.ttlSeconds * 1000, now).run();
  return json({ pairingId, code, expiresAt: now + input.ttlSeconds * 1000 }, 201);
}

async function claimPairing(request: Request, env: Env): Promise<Response> {
  requireSameOrigin(request, env.PUBLIC_ORIGIN);
  await enforceRateLimit(env, request, "pairing", 10, 300_000);
  const input = claimPairingSchema.parse(await readJson(request));
  const now = Date.now();
  const codeHash = await sha256(normalizePairingCode(input.code));
  const pairing = await env.DB.prepare(
    "UPDATE pairing_sessions SET claimed_at = ?1 WHERE code_hash = ?2 AND claimed_at IS NULL AND expires_at > ?1 RETURNING pairing_id, edge_id"
  ).bind(now, codeHash).first<{ pairing_id: string; edge_id: string }>();
  if (!pairing) throw new HttpError("INVALID_PAIRING", "Pairing code is invalid or expired", 401);

  const active = await env.DB.prepare(
    "SELECT COUNT(*) AS count FROM devices WHERE edge_id = ?1 AND revoked_at IS NULL"
  ).bind(pairing.edge_id).first<{ count: number }>();
  if ((active?.count ?? 0) >= 5) throw new HttpError("DEVICE_LIMIT", "Paired device limit reached", 409);

  const deviceId = randomId("device");
  const secret = randomSecret();
  const inserted = await env.DB.prepare(
    "INSERT INTO devices(device_id, edge_id, display_name, secret_hash, created_at, last_used_at) SELECT ?1, ?2, ?3, ?4, ?5, ?5 WHERE (SELECT COUNT(*) FROM devices WHERE edge_id = ?2 AND revoked_at IS NULL) < 5"
  ).bind(deviceId, pairing.edge_id, input.displayName, await sha256(secret), now).run();
  if (!inserted.meta.changes) throw new HttpError("DEVICE_LIMIT", "Paired device limit reached", 409);
  await audit(env, "phone", deviceId, "device.paired", pairing.edge_id, "allowed");
  return json(
    { paired: true, device: { deviceId, edgeId: pairing.edge_id, displayName: input.displayName } },
    201,
    { "Set-Cookie": deviceCookie(`${deviceId}.${secret}`, COOKIE_MAX_AGE) }
  );
}

async function getDevice(request: Request, env: Env): Promise<Response> {
  try {
    const device = await authenticateDevice(request, env);
    const response = await edgeRoom(env, device.edge_id).fetch("https://edge-room/presence");
    return json({
      ok: true,
      paired: true,
      capabilities: await contentCapabilities(env, device.edge_id),
      device: { deviceId: device.device_id, edgeId: device.edge_id, displayName: device.display_name },
      edge: await response.json()
    });
  } catch (caught) {
    if (caught instanceof HttpError && caught.status === 401) return json({ ok: true, paired: false });
    throw caught;
  }
}

async function listDevices(request: Request, env: Env, edgeId: string): Promise<Response> {
  const edge = await authenticateEdge(request, env);
  if (edge.edge_id !== edgeId) throw new HttpError("FORBIDDEN", "Edge does not own this resource", 403);
  const result = await env.DB.prepare(
    "SELECT device_id, display_name, created_at, last_used_at FROM devices WHERE edge_id = ?1 AND revoked_at IS NULL ORDER BY created_at DESC"
  ).bind(edgeId).all();
  return json({ devices: result.results });
}

async function unpairCurrentDevice(request: Request, env: Env): Promise<Response> {
  requireSameOrigin(request, env.PUBLIC_ORIGIN);
  const device = await authenticateDevice(request, env);
  await env.DB.prepare("UPDATE devices SET revoked_at = ?1 WHERE device_id = ?2 AND revoked_at IS NULL")
    .bind(Date.now(), device.device_id).run();
  await audit(env, "phone", device.device_id, "device.unpaired", device.edge_id, "allowed");
  return json(
    { paired: false },
    200,
    { "Set-Cookie": deviceCookie("", 0) }
  );
}

async function revokeDevice(request: Request, env: Env, deviceId: string): Promise<Response> {
  const edge = await authenticateEdge(request, env);
  const now = Date.now();
  const result = await env.DB.prepare(
    "UPDATE devices SET revoked_at = ?1 WHERE device_id = ?2 AND edge_id = ?3 AND revoked_at IS NULL"
  ).bind(now, deviceId, edge.edge_id).run();
  if (!result.meta.changes) throw new HttpError("NOT_FOUND", "Device not found", 404);
  await audit(env, "edge", edge.edge_id, "device.revoked", deviceId, "allowed");
  return json({ revoked: true });
}

async function createCall(request: Request, env: Env): Promise<Response> {
  requireSameOrigin(request, env.PUBLIC_ORIGIN);
  const device = await authenticateDevice(request, env);
  const input = createCallSchema.parse(await readJson(request));
  if (device.edge_id !== input.edgeId) throw new HttpError("FORBIDDEN", "Device is not paired to this Edge", 403);

  const duplicate = await env.DB.prepare(
    "SELECT * FROM calls WHERE device_id = ?1 AND idempotency_key = ?2"
  ).bind(device.device_id, input.idempotencyKey).first<CallRecord>();
  if (duplicate) return json(callPayload(duplicate), 200);

  const recent = await env.DB.prepare(
    "SELECT COUNT(*) AS count FROM calls WHERE device_id = ?1 AND created_at > ?2"
  ).bind(device.device_id, Date.now() - 60 * 60 * 1000).first<{ count: number }>();
  if ((recent?.count ?? 0) >= 10) throw new HttpError("RATE_LIMITED", "Remote call limit reached", 429);

  const presenceResponse = await edgeRoom(env, input.edgeId).fetch("https://edge-room/presence");
  const presence = await presenceResponse.json<{ connected?: boolean; modemOnline?: boolean; lineBusy?: boolean }>();
  if (!presence.connected) throw new HttpError("EDGE_OFFLINE", "Edge is offline", 409);
  if (presence.modemOnline === false) throw new HttpError("MODEM_OFFLINE", "Modem is offline", 409);
  if (presence.lineBusy) throw new HttpError("LINE_BUSY", "Line is busy", 409);

  const now = Date.now();
  const callId = randomId("call");
  const commandId = randomId("command");
  const sessionId = randomId("session");
  const roomName = randomId("callpilot");
  const phoneIdentity = randomId("web", 12);
  const edgeIdentity = randomId("edgepart", 12);
  const expiresAt = now + 5 * 60 * 1000;
  const inserted = await env.DB.prepare(
    "INSERT OR IGNORE INTO calls(call_id, edge_id, device_id, idempotency_key, room_name, phone_identity, edge_identity, status, created_at, expires_at, updated_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'pending', ?8, ?9, ?8)"
  ).bind(callId, input.edgeId, device.device_id, input.idempotencyKey, roomName, phoneIdentity, edgeIdentity, now, expiresAt).run();
  if (!inserted.meta.changes) {
    const raced = await env.DB.prepare(
      "SELECT * FROM calls WHERE device_id = ?1 AND idempotency_key = ?2"
    ).bind(device.device_id, input.idempotencyKey).first<CallRecord>();
    if (raced) return json(callPayload(raced), 200);
    throw new HttpError("CONFLICT", "Call could not be created", 409);
  }

  const edgeToken = await issueParticipantToken(env, roomName, edgeIdentity);
  const command = {
    v: 1,
    type: "session.start",
    commandId,
    callId,
    expiresAt: new Date(expiresAt).toISOString(),
    session: {
      sessionId,
      roomName,
      browserIdentity: phoneIdentity,
      edgeIdentity,
      livekitUrl: env.LIVEKIT_URL,
      token: edgeToken
    }
  };
  const delivered = await edgeRoom(env, input.edgeId).fetch("https://edge-room/command", {
    method: "POST",
    body: JSON.stringify(command)
  });
  if (!delivered.ok) {
    await env.DB.prepare("UPDATE calls SET status = 'failed', updated_at = ?1 WHERE call_id = ?2")
      .bind(Date.now(), callId).run();
    throw new HttpError("EDGE_OFFLINE", "Edge disconnected before the call could start", 409);
  }
  await audit(env, "phone", device.device_id, "call.created", callId, "allowed");
  const call = await env.DB.prepare("SELECT * FROM calls WHERE call_id = ?1").bind(callId).first<CallRecord>();
  return json(callPayload(call as CallRecord), 202);
}

// ---- Inbound takeover (#95): paired devices poll open offers and claim one.

async function listInboundOffers(request: Request, env: Env): Promise<Response> {
  const device = await authenticateDevice(request, env);
  const rows = await env.DB.prepare(
    "SELECT offer_id, expires_at FROM inbound_offers WHERE edge_id = ?1 AND status = 'offered' AND expires_at > ?2 ORDER BY created_at DESC LIMIT 5"
  ).bind(device.edge_id, Date.now()).all<{ offer_id: string; expires_at: number }>();
  return json({
    offers: (rows.results ?? []).map((row) => ({
      offerId: row.offer_id,
      expiresAt: row.expires_at
    }))
  });
}

async function claimInboundOffer(request: Request, env: Env): Promise<Response> {
  requireSameOrigin(request, env.PUBLIC_ORIGIN);
  const device = await authenticateDevice(request, env);
  const input = claimInboundOfferSchema.parse(await readJson(request));

  const offer = await env.DB.prepare(
    "SELECT * FROM inbound_offers WHERE offer_id = ?1"
  ).bind(input.offerId).first<InboundOfferRecord>();
  if (!offer || offer.edge_id !== device.edge_id) {
    throw new HttpError("NOT_FOUND", "Offer not found", 404);
  }

  const now = Date.now();
  const claimId = randomId("claim");
  const commandId = randomId("command");
  const roomName = randomId("callpilot");
  // Contract: identical session shape to session.start — Edge's strict parser
  // requires the web_ browser-identity prefix.
  const phoneIdentity = randomId("web", 12);
  const edgeIdentity = randomId("edgepart", 12);
  // First-claim-wins: the conditional UPDATE is the atomic arbiter. A loser
  // (double claim, expired or revoked offer) changes zero rows.
  const claimed = await env.DB.prepare(
    "UPDATE inbound_offers SET status = 'claimed', claim_id = ?1, claimed_device_id = ?2, room_name = ?3, phone_identity = ?4, edge_identity = ?5, updated_at = ?6 WHERE offer_id = ?7 AND status = 'offered' AND expires_at > ?6"
  ).bind(claimId, device.device_id, roomName, phoneIdentity, edgeIdentity, now, input.offerId).run();
  if (!claimed.meta.changes) {
    await audit(env, "phone", device.device_id, "inbound_offer.claim", input.offerId, "rejected");
    throw new HttpError("OFFER_UNAVAILABLE", "Offer already claimed, expired or revoked", 409);
  }

  // Past the atomic UPDATE the row is 'claimed'; any failure below must mark it
  // failed instead of stranding an unclaimable row.
  try {
    const edgeToken = await issueParticipantToken(env, roomName, edgeIdentity);
    const command = {
      v: 1,
      type: "inbound.claim",
      commandId,
      offerId: offer.offer_id,
      callId: offer.call_id,
      claimId,
      generation: offer.generation,
      nonce: offer.nonce,
      session: {
        sessionId: randomId("session"),
        roomName,
        browserIdentity: phoneIdentity,
        edgeIdentity,
        livekitUrl: env.LIVEKIT_URL,
        token: edgeToken
      }
    };
    const delivered = await edgeRoom(env, offer.edge_id).fetch("https://edge-room/command", {
      method: "POST",
      body: JSON.stringify(command)
    });
    if (!delivered.ok) {
      throw new HttpError("EDGE_OFFLINE", "Edge disconnected before takeover could start", 409);
    }
    const phoneToken = await issueParticipantToken(env, roomName, phoneIdentity);
    await audit(env, "phone", device.device_id, "inbound_offer.claim", offer.offer_id, "allowed");
    return json({
      claimId,
      offerId: offer.offer_id,
      roomName,
      url: env.LIVEKIT_URL,
      token: phoneToken,
      expiresAt: offer.expires_at
    }, 202);
  } catch (failure) {
    const errorCode = failure instanceof HttpError ? failure.code : "CLAIM_SETUP_FAILED";
    await env.DB.prepare(
      "UPDATE inbound_offers SET status = 'failed', error_code = ?1, updated_at = ?2 WHERE offer_id = ?3 AND status = 'claimed'"
    ).bind(errorCode, Date.now(), offer.offer_id).run();
    throw failure;
  }
}

async function getCall(request: Request, env: Env, callId: string): Promise<Response> {
  const device = await authenticateDevice(request, env);
  const call = await env.DB.prepare(
    "SELECT * FROM calls WHERE call_id = ?1 AND device_id = ?2"
  ).bind(callId, device.device_id).first<CallRecord>();
  if (!call) throw new HttpError("NOT_FOUND", "Call not found", 404);
  const payload: Record<string, unknown> = callPayload(call);
  if (call.status === "ready" && call.expires_at > Date.now()) {
    payload.session = {
      livekitUrl: env.LIVEKIT_URL,
      token: await issueParticipantToken(env, call.room_name, call.phone_identity),
      expiresAt: call.expires_at
    };
  }
  return json(payload);
}

async function relayContentRead(
  request: Request,
  env: Env,
  resource: ContentResource,
  callId: string | undefined
): Promise<Response> {
  const device = await authenticateDevice(request, env);
  await requireContentCapability(env, device, resource);
  if (!contentReadEnabled(env.CONTENT_READ_ENABLED)) {
    throw new HttpError("FEATURE_DISABLED", "Content sync is disabled", 403);
  }
  await enforceContentRateLimit(env, device);

  const params = contentParams(new URL(request.url), resource, callId);
  const now = Date.now();
  const relayRequest: DataRequest = {
    v: 1,
    type: "data.request",
    requestId: randomId("request", 12),
    deviceId: device.device_id,
    resource,
    params,
    issuedAtUnixMs: now,
    expiresAtUnixMs: now + contentRelayTimeoutMs(env)
  } as DataRequest;
  const wire = JSON.stringify(relayRequest);
  if (serializedByteLength(wire) > CONTENT_WIRE_LIMIT_BYTES) {
    throw new HttpError("PAYLOAD_TOO_LARGE", "The request exceeds the protocol limit", 413);
  }

  const relayed = await edgeRoom(env, device.edge_id).fetch("https://edge-room/content-relay", {
    method: "POST",
    body: wire
  });
  if (!relayed.ok) {
    const internal = await safeInternalRelayError(relayed);
    throw contentHttpError(internal);
  }
  const rawResponse = await relayed.text();
  if (serializedByteLength(rawResponse) > CONTENT_WIRE_LIMIT_BYTES) {
    throw new HttpError("PAYLOAD_TOO_LARGE", "The content item exceeds the protocol limit", 413);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(rawResponse);
  } catch {
    throw new HttpError("INTERNAL_ERROR", "The request could not be completed", 500);
  }
  const response = dataResponseSchema.safeParse(parsed);
  if (!response.success || !responseMatchesRequest(response.data, relayRequest)) {
    throw new HttpError("INTERNAL_ERROR", "The request could not be completed", 500);
  }

  // The credential may have been revoked or moved out of the allowlist while
  // Edge was reading. Never return even an accepted body without a fresh check.
  const currentDevice = await authenticateDevice(request, env);
  if (currentDevice.device_id !== device.device_id || currentDevice.edge_id !== device.edge_id) {
    throw new HttpError("UNAUTHORIZED", "Credential is missing, invalid, or revoked", 401);
  }
  await requireContentCapability(env, currentDevice, resource);
  if (!contentReadEnabled(env.CONTENT_READ_ENABLED)) {
    throw new HttpError("FEATURE_DISABLED", "Content sync is disabled", 403);
  }

  if (response.data.status === "error") throw contentHttpError(response.data.error.code);
  await audit(env, "phone", device.device_id, "content.read", resource, "allowed");
  return json(response.data.body);
}

function contentParams(
  url: URL,
  resource: ContentResource,
  callId: string | undefined
): DataRequest["params"] {
  const allowed = resource === "call_records.get" ? new Set<string>() : new Set(["limit", "cursor"]);
  for (const key of url.searchParams.keys()) {
    if (!allowed.has(key) || url.searchParams.getAll(key).length !== 1) {
      throw new HttpError("INVALID_REQUEST", "The request fields are invalid", 400);
    }
  }
  if (resource === "call_records.get") {
    if (!callId) throw new HttpError("INVALID_REQUEST", "The request fields are invalid", 400);
    return { callId };
  }
  const defaultLimit = resource === "call_timeline.list" ? 50 : 25;
  const rawLimit = url.searchParams.get("limit");
  const limit = rawLimit === null ? defaultLimit : Number(rawLimit);
  if (rawLimit !== null && !/^\d+$/.test(rawLimit)) {
    throw new HttpError("INVALID_REQUEST", "The request fields are invalid", 400);
  }
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
    throw new HttpError("INVALID_REQUEST", "The request fields are invalid", 400);
  }
  const rawCursor = url.searchParams.get("cursor");
  const cursor = rawCursor === null ? null : rawCursor;
  if (cursor !== null && !contentCursorSchema.safeParse(cursor).success) {
    throw new HttpError("CURSOR_INVALID", "The cursor is not valid for this resource", 400);
  }
  if (resource === "call_timeline.list") {
    if (!callId) throw new HttpError("INVALID_REQUEST", "The request fields are invalid", 400);
    return { callId, limit, cursor };
  }
  return { limit, cursor };
}

async function contentCapabilities(env: Env, edgeId: string): Promise<string[]> {
  const granted = await env.DB.prepare(
    "SELECT allowlist.edge_id FROM content_read_edges AS allowlist JOIN edges ON edges.edge_id = allowlist.edge_id WHERE allowlist.edge_id = ?1 AND edges.revoked_at IS NULL"
  ).bind(edgeId).first<{ edge_id: string }>();
  return granted ? [...CONTENT_CAPABILITIES] : [];
}

async function requireContentCapability(
  env: Env,
  device: DeviceRecord,
  resource: ContentResource
): Promise<void> {
  const required = resource === "messages.list" ? "messages:read" : "call_records:read";
  if (!(await contentCapabilities(env, device.edge_id)).includes(required)) {
    throw new HttpError("FORBIDDEN", "The device cannot read this resource", 403);
  }
}

async function safeInternalRelayError(response: Response): Promise<ContentErrorCode> {
  try {
    const payload = await response.json<{ error?: unknown }>();
    const parsed = typeof payload.error === "string" ? payload.error : "INTERNAL_ERROR";
    return isContentErrorCode(parsed) ? parsed : "INTERNAL_ERROR";
  } catch {
    return "INTERNAL_ERROR";
  }
}

function isContentErrorCode(code: string): code is ContentErrorCode {
  return [
    "INVALID_REQUEST", "CURSOR_INVALID", "FORBIDDEN", "FEATURE_DISABLED", "NOT_FOUND",
    "RATE_LIMITED", "PAYLOAD_TOO_LARGE", "EDGE_OFFLINE", "TIMEOUT", "INTERNAL_ERROR"
  ].includes(code);
}

function contentHttpError(code: ContentErrorCode): HttpError {
  const mapping: Record<ContentErrorCode, [number, string]> = {
    INVALID_REQUEST: [400, "The request fields are invalid"],
    CURSOR_INVALID: [400, "The cursor is not valid for this resource"],
    FORBIDDEN: [403, "The device cannot read this resource"],
    FEATURE_DISABLED: [403, "Content sync is disabled"],
    NOT_FOUND: [404, "Call record was not found"],
    RATE_LIMITED: [429, "Too many content requests"],
    PAYLOAD_TOO_LARGE: [413, "The content item exceeds the protocol limit"],
    EDGE_OFFLINE: [503, "Edge is offline"],
    TIMEOUT: [504, "Edge did not respond in time"],
    INTERNAL_ERROR: [500, "The request could not be completed"]
  };
  const [status, message] = mapping[code];
  const headers = code === "RATE_LIMITED" ? { "Retry-After": "30" } : undefined;
  return new HttpError(code, message, status, headers);
}

async function authenticateActorForEdge(
  request: Request,
  env: Env,
  edgeId: string
): Promise<{ kind: "edge" | "device" }> {
  try {
    const edge = await authenticateEdge(request, env);
    if (edge.edge_id !== edgeId) throw new HttpError("FORBIDDEN", "Edge does not own this resource", 403);
    return { kind: "edge" };
  } catch (caught) {
    if (!(caught instanceof HttpError) || caught.status !== 401) throw caught;
  }
  const device = await authenticateDevice(request, env);
  if (device.edge_id !== edgeId) throw new HttpError("FORBIDDEN", "Device is not paired to this Edge", 403);
  return { kind: "device" };
}

function edgeRoom(env: Env, edgeId: string): DurableObjectStub {
  return env.EDGE_ROOMS.get(env.EDGE_ROOMS.idFromName(edgeId));
}

function callPayload(call: CallRecord): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    callId: call.call_id,
    edgeId: call.edge_id,
    status: call.status,
    createdAt: call.created_at,
    expiresAt: call.expires_at
  };
  if (call.error_code) payload.errorCode = call.error_code;
  return payload;
}

function normalizePairingCode(code: string): string {
  return code.replace(/-/g, "").toUpperCase();
}

function deviceCookie(value: string, maxAge: number): string {
  return `__Host-callpilot-device=${value}; Path=/; Max-Age=${maxAge}; Secure; HttpOnly; SameSite=Strict`;
}

function secureAsset(response: Response, env: Env): Response {
  const headers = new Headers(response.headers);
  const liveKitSources = liveKitConnectSources(env.LIVEKIT_URL);
  headers.set("Content-Security-Policy", `default-src 'none'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self'; connect-src 'self'${liveKitSources}; media-src blob:; worker-src 'self' blob:; manifest-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`);
  headers.set("Permissions-Policy", "microphone=(self), camera=(), geolocation=()");
  headers.set("Referrer-Policy", "no-referrer");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("X-Frame-Options", "DENY");
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

async function audit(
  env: Env,
  actorType: string,
  actorId: string,
  action: string,
  resourceId: string,
  result: string
): Promise<void> {
  await env.DB.prepare(
    "INSERT INTO audit_events(event_id, actor_type, actor_id, action, resource_id, result, occurred_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)"
  ).bind(randomId("event"), actorType, actorId, action, resourceId, result, Date.now()).run();
}

async function enforceRateLimit(
  env: Env,
  request: Request,
  action: string,
  limit: number,
  windowMs: number
): Promise<void> {
  const client = request.headers.get("CF-Connecting-IP") ?? "unknown";
  const now = Date.now();
  const bucket = Math.floor(now / windowMs) * windowMs;
  const key = `${action}:${client}:${bucket}`;
  const row = await env.DB.prepare(
    "INSERT INTO rate_limits(rate_key, window_start, count) VALUES (?1, ?2, 1) ON CONFLICT(rate_key) DO UPDATE SET count = count + 1 RETURNING count"
  ).bind(key, bucket).first<{ count: number }>();
  if ((row?.count ?? limit + 1) > limit) {
    throw new HttpError("RATE_LIMITED", "Too many attempts", 429);
  }
}

void EdgeRoom;
