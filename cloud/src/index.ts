import { authenticateDevice, authenticateEdge } from "./auth";
import { ClaimTiming } from "./claim-timing";
import { claimInboundFastpathV2, fallbackInboundLivekit, inboundIce, completeInboundSdp, endInboundMedia } from "./inbound-fastpath-v2";
import { turnConfigured } from "./turn";
import {
  contentReadEnabled,
  contentRelayTimeoutMs
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
import { issueParticipantToken, participantRequest } from "./livekit";
import { invalidateCandidates, invalidateCandidatesStatement, participantCleanupStatement, prepareInbound, queueParticipantCleanup, type Preparation } from "./inbound-preconnect";
import {
  appReviewPairingEnabled,
  PairingPolicyError,
  validatePairingPolicy
} from "./pairing-policy";
import {
  claimEnrollmentSchema,
  claimInboundOfferSchema,
  claimPairingSchema,
  createCallSchema,
  createEnrollmentInviteSchema,
  createPairingSchema,
  prepareInboundOfferSchema,
  registerVoipTokenSchema
} from "./schemas";
import { constantTimeEqual, randomId, randomPairingCode, randomSecret, sha256 } from "./security";
import type { CallRecord, DeviceRecord, EdgePresence, Env, InboundOfferRecord } from "./types";
import {
  decodeEncryptionKey,
  encryptPushToken,
  voipPushEnabled
} from "./voip-push";
import { ZodError } from "zod";

export { EdgeRoom };

const COOKIE_MAX_AGE = 180 * 24 * 60 * 60;

export default {
  async fetch(request: Request, env: Env, ctx?: ExecutionContext): Promise<Response> {
    const path = new URL(request.url).pathname;
    const timing = request.method === "POST" && ["/v1/inbound-offers/claim", "/v1/inbound-offers/prepare"].includes(path.replace(/\/$/, ""))
      ? new ClaimTiming() : undefined;
    const requestId = randomId(
      path.startsWith("/v1/messages") || path.startsWith("/v1/call-records") ? "request" : "req",
      12
    );
    let response: Response;
    try {
      response = await route(request, env, requestId, timing, ctx);
    } catch (caught) {
      if (caught instanceof HttpError) {
        response = error(caught.code, caught.message, caught.status, requestId, caught.headers);
      } else if (caught instanceof ZodError) {
        response = error("VALIDATION_ERROR", "Request fields are invalid", 400, requestId);
      } else {
        console.error(JSON.stringify({ requestId, errorType: caught instanceof Error ? caught.name : "unknown" }));
        response = error("INTERNAL_ERROR", "The request could not be completed", 500, requestId);
      }
    }
    return timing ? timing.finish(response) : response;
  }
};

async function route(request: Request, env: Env, requestId: string, timing?: ClaimTiming, ctx?: ExecutionContext): Promise<Response> {
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
  if (path === "/v1/device/alert-push-token" && request.method === "PUT") {
    return registerVoipToken(request, env, "alert");
  }
  if (path === "/v1/device/alert-push-token" && request.method === "DELETE") {
    return unregisterVoipToken(request, env, "alert");
  }
  if (path === "/v1/device/push-token" && request.method === "PUT") {
    return registerVoipToken(request, env);
  }
  if (path === "/v1/device/push-token" && request.method === "DELETE") {
    return unregisterVoipToken(request, env);
  }
  if (path === "/v1/calls" && request.method === "POST") return createCall(request, env);
  if (path === "/v1/inbound-offers" && request.method === "GET") {
    return listInboundOffers(request, env);
  }
  if (path === "/v1/inbound-offers/claim" && request.method === "POST") {
    if (env.INBOUND_FASTPATH_V2_ENABLED === "true") {
      const input = claimInboundOfferSchema.parse(await readJson(request.clone(), 32 * 1024));
      if (input.mediaTransport === "webrtc" && turnConfigured(env)) {
        const direct = await claimInboundFastpathV2(request.clone(), env, timing!, ctx);
        if (direct) return direct;
      }
      // A V2 rollout never activates the old restricted-room preparation path.
      return claimInboundOffer(request, { ...env, INBOUND_PRECONNECT_ENABLED: "false", INBOUND_CLAIM_FAST_PATH_ENABLED: "false" }, timing!, ctx);
    }
    return claimInboundOffer(request, env, timing!, ctx);
  }
  const inboundMedia = path.match(/^\/v1\/inbound-offers\/(offer_[A-Za-z0-9_-]{12,80})\/(ice|media\/livekit|media\/sdp|media\/end)$/);
  if (inboundMedia && request.method === "POST") {
    return inboundMedia[2] === "ice" ? inboundIce(request, env, inboundMedia[1]!, ctx)
      : inboundMedia[2] === "media/sdp" ? completeInboundSdp(request, env, inboundMedia[1]!)
      : inboundMedia[2] === "media/end" ? endInboundMedia(request, env, inboundMedia[1]!)
      : fallbackInboundLivekit(request, env, inboundMedia[1]!);
  }
  if (path === "/v1/inbound-offers/prepare" && request.method === "POST") {
    requireSameOrigin(request, env.PUBLIC_ORIGIN);
    const device = await timing!.measure("auth", () => authenticateDevice(request, env, ctx));
    const input = prepareInboundOfferSchema.parse(await readJson(request));
    return json(await prepareInbound(env, device, input.offerId, timing));
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
  try {
    validatePairingPolicy(
      input.purpose,
      input.ttlSeconds,
      appReviewPairingEnabled(env.APP_REVIEW_PAIRING_ENABLED)
    );
  } catch (caught) {
    if (caught instanceof PairingPolicyError && caught.code === "FEATURE_DISABLED") {
      throw new HttpError("FEATURE_DISABLED", "App Review pairing is disabled", 403);
    }
    if (caught instanceof PairingPolicyError) {
      throw new HttpError("VALIDATION_ERROR", "Pairing lifetime is invalid", 400);
    }
    throw caught;
  }
  const active = await env.DB.prepare(
    "SELECT COUNT(*) AS count FROM devices WHERE edge_id = ?1 AND revoked_at IS NULL"
  ).bind(edgeId).first<{ count: number }>();
  if ((active?.count ?? 0) >= 5) throw new HttpError("DEVICE_LIMIT", "Paired device limit reached", 409);

  const pairingId = randomId("pairing");
  const code = randomPairingCode();
  const now = Date.now();
  await env.DB.prepare(
    "INSERT INTO pairing_sessions(pairing_id, edge_id, code_hash, expires_at, created_at, purpose) VALUES (?1, ?2, ?3, ?4, ?5, ?6)"
  ).bind(
    pairingId,
    edgeId,
    await sha256(normalizePairingCode(code)),
    now + input.ttlSeconds * 1000,
    now,
    input.purpose
  ).run();
  return json({ pairingId, code, expiresAt: now + input.ttlSeconds * 1000 }, 201);
}

async function claimPairing(request: Request, env: Env): Promise<Response> {
  requireSameOrigin(request, env.PUBLIC_ORIGIN);
  await enforceRateLimit(env, request, "pairing", 10, 300_000);
  const input = claimPairingSchema.parse(await readJson(request));
  const now = Date.now();
  const codeHash = await sha256(normalizePairingCode(input.code));
  const pairing = await env.DB.prepare(
    "UPDATE pairing_sessions SET claimed_at = ?1 WHERE code_hash = ?2 AND claimed_at IS NULL AND expires_at > ?1 AND (purpose = 'standard' OR (purpose = 'app_review' AND ?3 = 1)) RETURNING pairing_id, edge_id"
  ).bind(
    now,
    codeHash,
    appReviewPairingEnabled(env.APP_REVIEW_PAIRING_ENABLED) ? 1 : 0
  ).first<{ pairing_id: string; edge_id: string }>();
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
  const v2 = env.INBOUND_FASTPATH_V2_ENABLED === "true";
  const features = { claimPrewarm: env.CLAIM_PREWARM_ENABLED === "true", preconnect: !v2 && env.INBOUND_PRECONNECT_ENABLED === "true",
    ...(v2 ? { inboundFastpathV2: false } : {}) };
  try {
    const device = await authenticateDevice(request, env);
    const response = await edgeRoom(env, device.edge_id).fetch("https://edge-room/presence");
    const presence = await response.json() as EdgePresence;
    if (v2) features.inboundFastpathV2 = turnConfigured(env) && presence.connected && presence.inboundFastpathV2 === true;
    return json({
      ok: true,
      paired: true,
      features,
      capabilities: await contentCapabilities(env, device.edge_id),
      device: { deviceId: device.device_id, edgeId: device.edge_id, displayName: device.display_name },
      edge: presence
    });
  } catch (caught) {
    if (caught instanceof HttpError && caught.status === 401) return json({ ok: true, paired: false, features });
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
  await env.DB.prepare("DELETE FROM device_push_tokens WHERE device_id = ?1")
    .bind(device.device_id).run();
  if (voipPushEnabled(env.ALERT_PUSH_ENABLED)) {
    await env.DB.prepare("DELETE FROM device_alert_push_tokens WHERE device_id = ?1")
      .bind(device.device_id).run();
  }
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
  await env.DB.prepare("DELETE FROM device_push_tokens WHERE device_id = ?1")
    .bind(deviceId).run();
  if (voipPushEnabled(env.ALERT_PUSH_ENABLED)) {
    await env.DB.prepare("DELETE FROM device_alert_push_tokens WHERE device_id = ?1")
      .bind(deviceId).run();
  }
  await audit(env, "edge", edge.edge_id, "device.revoked", deviceId, "allowed");
  return json({ revoked: true });
}

async function registerVoipToken(request: Request, env: Env, kind: "voip" | "alert" = "voip"): Promise<Response> {
  requireSameOrigin(request, env.PUBLIC_ORIGIN);
  const device = await authenticateDevice(request, env);
  if (!voipPushEnabled(kind === "voip" ? env.VOIP_PUSH_ENABLED : env.ALERT_PUSH_ENABLED)) {
    throw new HttpError("FEATURE_DISABLED", kind === "voip" ? "VoIP push is disabled" : "Alert push is disabled", 403);
  }
  if (!env.PUSH_TOKEN_ENCRYPTION_KEY) {
    throw new HttpError("FEATURE_UNAVAILABLE", "VoIP push is unavailable", 503);
  }
  const input = registerVoipTokenSchema.parse(await readJson(request));
  const encrypted = await encryptPushToken(
    input.token,
    decodeEncryptionKey(env.PUSH_TOKEN_ENCRYPTION_KEY)
  );
  const table = kind === "voip" ? "device_push_tokens" : "device_alert_push_tokens";
  await env.DB.prepare(
    `INSERT INTO ${table}(device_id, token_ciphertext, token_nonce, environment, updated_at) VALUES (?1, ?2, ?3, ?4, ?5) ON CONFLICT(device_id) DO UPDATE SET token_ciphertext = excluded.token_ciphertext, token_nonce = excluded.token_nonce, environment = excluded.environment, updated_at = excluded.updated_at`
  ).bind(
    device.device_id,
    encrypted.ciphertext,
    encrypted.nonce,
    input.environment,
    Date.now()
  ).run();
  await audit(env, "phone", device.device_id, `${kind}_token.registered`, device.edge_id, "allowed");
  return json({ registered: true });
}

async function unregisterVoipToken(request: Request, env: Env, kind: "voip" | "alert" = "voip"): Promise<Response> {
  requireSameOrigin(request, env.PUBLIC_ORIGIN);
  const device = await authenticateDevice(request, env);
  if (kind === "alert" && !voipPushEnabled(env.ALERT_PUSH_ENABLED)) {
    throw new HttpError("FEATURE_DISABLED", "Push is disabled", 403);
  }
  const table = kind === "voip" ? "device_push_tokens" : "device_alert_push_tokens";
  await env.DB.prepare(`DELETE FROM ${table} WHERE device_id = ?1`)
    .bind(device.device_id).run();
  await audit(env, "phone", device.device_id, `${kind}_token.unregistered`, device.edge_id, "allowed");
  return json({ registered: false });
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
    "SELECT offer_id, call_uuid, expires_at FROM inbound_offers WHERE edge_id = ?1 AND status = 'offered' AND expires_at > ?2 ORDER BY created_at DESC LIMIT 5"
  ).bind(device.edge_id, Date.now()).all<{
    offer_id: string;
    call_uuid: string | null;
    expires_at: number;
  }>();
  return json({
    offers: (rows.results ?? []).map((row) => ({
      offerId: row.offer_id,
      ...(row.call_uuid ? { callUUID: row.call_uuid } : {}),
      expiresAt: row.expires_at
    }))
  });
}

async function claimInboundOffer(request: Request, env: Env, timing: ClaimTiming, ctx?: ExecutionContext): Promise<Response> {
  requireSameOrigin(request, env.PUBLIC_ORIGIN);
  // Preconnect uses the existing atomic fast path without changing deployment
  // flags. With preconnect disabled the legacy/explicit-fast selection is intact.
  const fast = env.INBOUND_CLAIM_FAST_PATH_ENABLED === "true" || env.INBOUND_PRECONNECT_ENABLED === "true";
  // Only last_used_at bookkeeping is deferred by auth's context; credential and
  // revocation checks still finish before any speculative lookup or CAS.
  const device = await timing.measure("auth", () => authenticateDevice(request, env, fast ? ctx : undefined));
  const input = claimInboundOfferSchema.parse(await readJson(request, env.INBOUND_FASTPATH_V2_ENABLED === "true" ? 32 * 1024 : 16 * 1024));
  if (!fast) {
    const existing = await timing.measure("offer_read", () => env.DB.prepare(
      "SELECT offer_id FROM inbound_offers WHERE offer_id = ?1 AND edge_id = ?2"
    ).bind(input.offerId, device.edge_id).first());
    if (!existing) throw new HttpError("NOT_FOUND", "Offer not found", 404);
  }

  const now = Date.now();
  const claimId = randomId("claim");
  const commandId = randomId("command");
  let roomName = randomId("callpilot");
  // Contract: identical session shape to session.start — Edge's strict parser
  // requires the web_ browser-identity prefix.
  let phoneIdentity = randomId("web", 12);
  let edgeIdentity = randomId("edgepart", 12);
  // First-claim-wins: the conditional UPDATE is the atomic arbiter. A loser
  // (double claim, expired or revoked offer) changes zero rows.
  // These reads do not grant permissions or send commands. Run them alongside
  // CAS, but only the CAS winner may act on their results. Lookup/presence errors
  // make reuse unavailable, not the independent cold path.
  const preparation = input.prepareId && env.INBOUND_PRECONNECT_ENABLED === "true"
    ? Promise.all([
      timing.measure("prepare_read", () => env.DB.prepare(`SELECT * FROM inbound_preparations WHERE prepare_id = ?1
        AND offer_id = ?2 AND device_id = ?3 AND state = 'prepared' AND expires_at > ?4`)
        .bind(input.prepareId, input.offerId, device.device_id, now).first<Preparation>()),
      timing.measure("presence", async () => {
        const response = await edgeRoom(env, device.edge_id).fetch("https://edge-room/presence");
        return await response.json() as { connected?: boolean; inboundPreconnectV1?: boolean };
      })
    ]).then(([candidate, capability]) => capability.connected && capability.inboundPreconnectV1 ? candidate : null)
      .catch(() => null)
    : Promise.resolve(null);
  const [offer, prepared] = await Promise.all([timing.measure("cas", () => env.DB.prepare(
    `UPDATE inbound_offers SET status = 'claimed', claim_id = ?1, claimed_device_id = ?2, updated_at = ?3,
      room_name = CASE WHEN ?9 = 1 THEN room_name ELSE ?6 END,
      phone_identity = ?7, edge_identity = CASE WHEN ?9 = 1 THEN edge_identity ELSE ?8 END
      WHERE offer_id = ?4 AND edge_id = ?5 AND status = 'offered' AND expires_at > ?3 RETURNING *`
  ).bind(claimId, device.device_id, now, input.offerId, device.edge_id, roomName, phoneIdentity, edgeIdentity,
    input.prepareId && env.INBOUND_PRECONNECT_ENABLED === "true" ? 1 : 0).first<InboundOfferRecord>()), preparation]);
  const recordAudit = async (outcome: "allowed" | "rejected") => {
    const operation = audit(env, "phone", device.device_id, "inbound_offer.claim", input.offerId, outcome);
    if (fast && ctx) {
      // Audit is best-effort accounting, never an authorization or delivery gate.
      // Keep it alive after the response; callers without a context must await it.
      const background = operation.catch(() => { console.warn("Inbound claim audit failed"); });
      ctx.waitUntil(background);
    } else await operation;
  };
  if (!offer) {
    const existing = !fast || await timing.measure("offer_read", () => env.DB.prepare(
      "SELECT offer_id FROM inbound_offers WHERE offer_id = ?1 AND edge_id = ?2"
    ).bind(input.offerId, device.edge_id).first());
    if (!existing) throw new HttpError("NOT_FOUND", "Offer not found", 404);
    await timing.measure("audit", () => recordAudit("rejected"));
    throw new HttpError("OFFER_UNAVAILABLE", "Offer already claimed, expired or revoked", 409);
  }

  // Past the atomic UPDATE the row is 'claimed'; any failure below must mark it
  // failed instead of stranding an unclaimable row.
  try {
    const candidate = offer.preconnect_enabled ? prepared : null;
    let reuse = false;
    let reuseTokens: [string, string] | undefined;
    if (candidate && offer.edge_identity && candidate.room_name === offer.room_name) {
      // Local signing is independent of the admin API. Keep tokens private until
      // permission, durable finalization and delivery succeed. Settle all work so
      // even a signing error cannot leave promotion running past cold fallback.
      const [permission, edgeToken, phoneToken] = await Promise.allSettled([
        timing.measure("permission", () => participantRequest(env, "UpdateParticipant", candidate.room_name, candidate.participant_identity, true)),
        timing.measure("edge_token", () => issueParticipantToken(env, candidate.room_name, offer.edge_identity!)),
        timing.measure("phone_token", () => issueParticipantToken(env, candidate.room_name, candidate.participant_identity))
      ]);
      if (permission.status === "fulfilled" && edgeToken.status === "fulfilled" && phoneToken.status === "fulfilled") {
        reuse = true;
        reuseTokens = [edgeToken.value, phoneToken.value];
      }
      // An ambiguous timeout must never reuse this identity.
    }
    if (reuse && candidate) {
      roomName = candidate.room_name;
      phoneIdentity = candidate.participant_identity;
      edgeIdentity = offer.edge_identity!;
    }
    // A revoke/failed Edge ACK racing the admin API must win over its late result.
    const finalize = () => env.DB.prepare(`UPDATE inbound_offers SET room_name = ?1, phone_identity = ?2, edge_identity = ?3
      WHERE offer_id = ?4 AND claim_id = ?5 AND status = 'claimed' RETURNING offer_id`)
      .bind(roomName, phoneIdentity, edgeIdentity, offer.offer_id, claimId);
    // Do not defer these writes: the alarm needs durable loser states and cold
    // identities even if waitUntil is interrupted. One transaction also prevents
    // a sweep from observing a winner state without its finalized identities.
    const current = offer.preconnect_enabled
      ? (await timing.measure("claim_finalize", () => env.DB.batch([
        finalize(),
        invalidateCandidatesStatement(env, offer.offer_id, reuse ? candidate!.prepare_id : undefined),
        ...(!reuse ? [participantCleanupStatement(env, offer, roomName, edgeIdentity),
          participantCleanupStatement(env, offer, roomName, phoneIdentity)] : [])
      ])))[0]?.results[0]
      : !input.prepareId || await timing.measure("claim_finalize", () => finalize().first());
    if (!current) throw new HttpError("OFFER_UNAVAILABLE", "Offer ended during claim", 409);
    const [edgeToken, signedPhoneToken] = reuseTokens ?? (fast ? await Promise.all([
      timing.measure("edge_token", () => issueParticipantToken(env, roomName, edgeIdentity)),
      timing.measure("phone_token", () => issueParticipantToken(env, roomName, phoneIdentity))
    ]) : [await timing.measure("edge_token", () => issueParticipantToken(env, roomName, edgeIdentity)), null]);
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
    const delivered = await timing.measure("edge_send", () => edgeRoom(env, offer.edge_id).fetch("https://edge-room/command", {
      method: "POST",
      body: JSON.stringify(command)
    }));
    if (!delivered.ok) {
      throw new HttpError("EDGE_OFFLINE", "Edge disconnected before takeover could start", 409);
    }
    const phoneToken = signedPhoneToken ?? await timing.measure("phone_token", () => issueParticipantToken(env, roomName, phoneIdentity));
    if (offer.preconnect_enabled) {
      // This is a response correctness fence, not accounting: a revoke/failed
      // ACK during Edge delivery must still return 409, never a usable session.
      const active = await timing.measure("claim_check", () => env.DB.prepare(`SELECT offer_id FROM inbound_offers
        WHERE offer_id = ?1 AND claim_id = ?2 AND status IN ('claimed', 'edge_ready')`)
        .bind(offer.offer_id, claimId).first());
      if (!active) throw new HttpError("OFFER_UNAVAILABLE", "Offer ended during claim", 409);
    }
    await timing.measure("audit", () => recordAudit("allowed"));
    if (offer.preconnect_enabled) {
      // Only execution is deferred: durable cleanup above plus the offer's
      // already-armed DO alarm guarantee retry if this eager sweep is interrupted.
      // Start after delivery/check so the sweep does not contend with the claim.
      const cleanup = edgeRoom(env, offer.edge_id).fetch("https://edge-room/preconnect-cleanup", { method: "POST" })
        .catch(() => { console.warn("Inbound claim cleanup wake failed"); });
      if (ctx) ctx.waitUntil(cleanup);
      else await cleanup;
    }
    return json({
      claimId,
      offerId: offer.offer_id,
      roomName,
      url: env.LIVEKIT_URL,
      token: phoneToken,
      ...(input.prepareId ? { mode: reuse ? "reuse" : "cold", participantIdentity: phoneIdentity, edgeIdentity } : {}),
      expiresAt: offer.expires_at
    }, 202);
  } catch (failure) {
    const errorCode = failure instanceof HttpError ? failure.code : "CLAIM_SETUP_FAILED";
    await env.DB.prepare(
      "UPDATE inbound_offers SET status = 'failed', error_code = ?1, updated_at = ?2 WHERE offer_id = ?3 AND status IN ('claimed', 'edge_ready')"
    ).bind(errorCode, Date.now(), offer.offer_id).run();
    if (offer.preconnect_enabled) {
      await invalidateCandidates(env, offer.offer_id);
      if (offer.room_name && offer.edge_identity) await queueParticipantCleanup(env, offer, offer.room_name, offer.edge_identity);
      ctx?.waitUntil(edgeRoom(env, offer.edge_id).fetch("https://edge-room/preconnect-cleanup", { method: "POST" }));
    }
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
    throw contentHttpError(internal, relayed.headers.get("Retry-After"));
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

function contentHttpError(code: ContentErrorCode, retryAfter: string | null = null): HttpError {
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
  const headers = code === "RATE_LIMITED"
    ? { "Retry-After": retryAfter && /^[1-9]\d*$/.test(retryAfter) ? retryAfter : "30" }
    : undefined;
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
  const now = Date.now();
  const insert = "INSERT INTO audit_events(event_id, actor_type, actor_id, action, resource_id, result, occurred_at)";
  const values = [randomId("event"), actorType, actorId, action, resourceId, result, now];
  await (action === "content.read"
    ? env.DB.prepare(
      `${insert} SELECT ?1, ?2, ?3, ?4, ?5, ?6, ?7 WHERE NOT EXISTS (SELECT 1 FROM audit_events WHERE actor_id = ?3 AND action = ?4 AND resource_id = ?5 AND occurred_at > ?8)`
    ).bind(...values, now - 10 * 60_000)
    : env.DB.prepare(`${insert} VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)`).bind(...values)).run();
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
