import { authenticateDevice } from "./auth";
import { ClaimTiming } from "./claim-timing";
import { HttpError, json, readJson, requireSameOrigin } from "./http";
import { invalidateCandidatesStatement } from "./inbound-preconnect";
import { notifyOwnerDecision, rejectOwnerDecision } from "./inbound-owner";
import { issueParticipantToken, LIVEKIT_PARTICIPANT_TOKEN_TTL_SECONDS } from "./livekit";
import { claimInboundOfferSchema, inboundAnswerSchema, inboundFallbackSchema, inboundSdpSchema } from "./schemas";
import { cookieCredential, randomId, sha256 } from "./security";
import { iceCredentialsSchema, issueIceServers, turnConfigured, type IceServer } from "./turn";
import type { EdgePresence, Env, InboundOfferRecord } from "./types";

function unavailable() { return new HttpError("OFFER_UNAVAILABLE", "Offer or claim is no longer available", 409); }
function unauthorized() { return new HttpError("UNAUTHORIZED", "Credential is missing, invalid, or revoked", 401); }
function edgeRoom(env: Env, edgeId: string) { return env.EDGE_ROOMS.get(env.EDGE_ROOMS.idFromName(edgeId)); }

export async function inboundIce(request: Request, env: Env, offerId: string, ctx?: ExecutionContext): Promise<Response> {
  requireSameOrigin(request, env.PUBLIC_ORIGIN);
  const device = await authenticateDevice(request, env);
  if (env.INBOUND_FASTPATH_V2_ENABLED !== "true" || !turnConfigured(env)) {
    throw new HttpError("TURN_UNAVAILABLE", "Direct media is not available", 503);
  }
  const offer = await env.DB.prepare(`SELECT * FROM inbound_offers WHERE offer_id = ?1 AND edge_id = ?2
    AND ((status = 'offered' AND expires_at > ?3) OR (status IN ('claimed', 'edge_ready')
      AND claimed_device_id = ?4 AND media_transport IN ('direct_pending', 'direct')))`)
    .bind(offerId, device.edge_id, Date.now(), device.device_id).first<InboundOfferRecord>();
  if (!offer) {
    await rejectOwnerDecision(env, device.edge_id, offerId, device.device_id);
    throw unavailable();
  }
  if (!["direct_eligible", "direct_pending", "direct"].includes(offer.media_transport ?? "")) {
    throw new HttpError("DIRECT_UNAVAILABLE", "Direct media is not available", 503);
  }
  const presence = await (await edgeRoom(env, device.edge_id).fetch("https://edge-room/presence")).json() as EdgePresence;
  if (!presence.connected || !presence.inboundFastpathV2) throw new HttpError("DIRECT_UNAVAILABLE", "Direct media is not available", 503);
  const warming = edgeRoom(env, device.edge_id).fetch("https://edge-room/inbound-ice", {
    method: "POST", body: JSON.stringify(iceRequest(offer, "warm"))
  }).then(() => undefined).catch(() => { console.warn("Inbound ICE warming unavailable"); });
  const phone = issueIceServers(env);
  if (ctx) ctx.waitUntil(warming);
  else {
    const [credentials] = await Promise.all([phone, warming]);
    return json(credentials);
  }
  return json(await phone);
}

export async function claimInboundFastpathV2(request: Request, env: Env, timing: ClaimTiming, ctx?: ExecutionContext): Promise<Response | null> {
  requireSameOrigin(request, env.PUBLIC_ORIGIN);
  const credential = cookieCredential(request);
  if (!credential || !credential[0].startsWith("device_")) throw unauthorized();
  const [deviceId, secret] = credential;
  const input = claimInboundOfferSchema.parse(await readJson(request, 32 * 1024));
  const now = Date.now(), claimId = randomId("claim");
  const room = randomId("callpilot"), phone = randomId("web", 12), edgeIdentity = randomId("edgepart", 12);
  const hash = await sha256(secret);
  const offer = await timing.measure("cas", () => env.DB.prepare(`UPDATE inbound_offers SET status = 'claimed',
    claim_id = ?1, claimed_device_id = ?2, updated_at = ?3, room_name = ?4, phone_identity = ?5,
    edge_identity = ?6, media_transport = 'direct_pending'
    WHERE offer_id = ?7 AND status = 'offered' AND expires_at > ?3 AND media_transport = 'direct_eligible'
      AND edge_id = (SELECT edge_id FROM devices WHERE device_id = ?2 AND secret_hash = ?8 AND revoked_at IS NULL)
    RETURNING *`).bind(claimId, deviceId, now, room, phone, edgeIdentity, input.offerId, hash).first<InboundOfferRecord>());
  if (!offer) {
    const valid = await env.DB.prepare(`SELECT edge_id FROM devices WHERE device_id = ?1 AND secret_hash = ?2 AND revoked_at IS NULL`)
      .bind(deviceId, hash).first<{edge_id: string}>();
    if (!valid) throw unauthorized();
    await rejectOwnerDecision(env, valid.edge_id, input.offerId, deviceId);
    const ineligible = await env.DB.prepare(`SELECT offer_id FROM inbound_offers WHERE offer_id = ?1
      AND status = 'offered' AND expires_at > ?2 AND media_transport IS NULL
      AND edge_id = (SELECT edge_id FROM devices WHERE device_id = ?3 AND secret_hash = ?4 AND revoked_at IS NULL)`)
      .bind(input.offerId, now, deviceId, hash).first();
    if (ineligible) return null;
    throw unavailable();
  }
  timing.tagClaim(claimId);
  if (env.INBOUND_MULTI_DEVICE_STOP_ENABLED === "true" || env.INBOUND_OWNER_DECLINE_ENABLED === "true") {
    const decision = notifyOwnerDecision(env, offer.edge_id, offer.offer_id);
    if (ctx) ctx.waitUntil(decision); else await decision;
  }
  try {
    const expiresAt = tokenExpiryBeforeMint();
    const [edgeToken, phoneToken] = await Promise.all([
      timing.measure("edge_token", () => issueParticipantToken(env, room, edgeIdentity, LIVEKIT_PARTICIPANT_TOKEN_TTL_SECONDS)),
      timing.measure("phone_token", () => issueParticipantToken(env, room, phone, LIVEKIT_PARTICIPANT_TOKEN_TTL_SECONDS))
    ]);
    // One stub preserves E-order at the DO; its dispatch gate preserves this
    // order through body reads without serializing the Edge acknowledgements.
    const stub = edgeRoom(env, offer.edge_id);
    const reserve = mediaCommand(offer, env, edgeToken, "inbound.claim");
    const reservation = timing.measure("edge_reserve", () => relayAnswer(env, offer, {
      ...reserve, direct: { deferred: true }
    }, stub));
    let iceServers: IceServer[] | undefined;
    let sdpAnswer: string | undefined;
    let reserveFailed = false;
    const media = (async () => {
      if (input.sdpOffer) {
        iceServers = await timing.measure("turn", () => edgeIce(env, offer, stub));
        if (reserveFailed) return;
        const command = mediaCommand(offer, env, edgeToken, "inbound.media.sdp");
        sdpAnswer = (await timing.measure("sdp_relay", () => relayAnswer(env, offer, {
          ...command, direct: { sdpOffer: input.sdpOffer, iceServers }
        }, stub)))?.sdpAnswer;
      }
    })().then(() => undefined, (error: unknown) => error);
    // Observe a late media rejection even if reservation fails first. Its
    // existing relay/TURN ceilings bound the background work, not claim HTTP.
    if (ctx) ctx.waitUntil(media.then(() => undefined));
    try {
      const reserved = await reservation;
      if (!reserved) throw new HttpError("EDGE_OFFLINE", "Edge reservation was not acknowledged", 409);
    } catch (error) {
      reserveFailed = true;
      throw error;
    }
    const mediaError = await media;
    if (mediaError instanceof HttpError && mediaError.status === 409) throw mediaError;
    // Only transport unavailability permits same-claim LiveKit fallback.
    const state = sdpAnswer ? "direct" : "direct_pending";
    const finalized = await timing.measure("claim_finalize", () => env.DB.batch([
      env.DB.prepare(`UPDATE inbound_offers SET media_transport = ?1, updated_at = ?2 WHERE offer_id = ?3
        AND claim_id = ?4 AND status IN ('claimed', 'edge_ready')
        AND EXISTS (SELECT device_id FROM devices WHERE device_id = ?5 AND secret_hash = ?6 AND revoked_at IS NULL)
        RETURNING offer_id`).bind(state, Date.now(), offer.offer_id, claimId, deviceId, hash),
      invalidateCandidatesStatement(env, offer.offer_id),
      env.DB.prepare(`INSERT INTO audit_events(event_id, actor_type, actor_id, action, resource_id, result, occurred_at)
        SELECT ?1, 'phone', ?2, 'inbound_offer.claim', ?3, 'allowed', ?4 FROM inbound_offers o
        JOIN devices d ON d.device_id = o.claimed_device_id
        WHERE o.offer_id = ?3 AND o.claim_id = ?5 AND o.status IN ('claimed', 'edge_ready')
          AND d.device_id = ?2 AND d.secret_hash = ?6 AND d.revoked_at IS NULL`)
        .bind(randomId("event"), deviceId, offer.offer_id, Date.now(), claimId, hash)
    ]));
    if (!finalized[0]?.results[0]) throw unavailable();
    const accounting = env.DB.prepare(`UPDATE devices SET last_used_at = ?1 WHERE device_id = ?2
      AND (last_used_at IS NULL OR last_used_at <= ?3)`).bind(now, deviceId, now - 600_000).run()
      .catch(() => { console.warn("Inbound V2 accounting failed"); });
    if (ctx) ctx.waitUntil(accounting); else await accounting;
    return json({ ...sessionPayload(offer, env, phoneToken, expiresAt), directAttempted: true,
      sdpPending: !input.sdpOffer,
      transports: [...(sdpAnswer ? [{ kind: "webrtc", sdpAnswer, iceServers }] : []),
        { kind: "livekit", url: env.LIVEKIT_URL, token: phoneToken }] }, 202);
  } catch (error) {
    await env.DB.prepare(`UPDATE inbound_offers SET status = 'failed', error_code = 'CLAIM_SETUP_FAILED', updated_at = ?1
      WHERE offer_id = ?2 AND claim_id = ?3 AND status IN ('claimed', 'edge_ready')`)
      .bind(Date.now(), offer.offer_id, claimId).run();
    throw error;
  }
}

export async function completeInboundSdp(request: Request, env: Env, offerId: string): Promise<Response> {
  requireSameOrigin(request, env.PUBLIC_ORIGIN);
  if (env.INBOUND_FASTPATH_V2_ENABLED !== "true") throw new HttpError("FEATURE_DISABLED", "Direct media is disabled", 403);
  const device = await authenticateDevice(request, env);
  const input = inboundSdpSchema.parse(await readJson(request, 32 * 1024));
  const offer = await env.DB.prepare(`SELECT * FROM inbound_offers WHERE offer_id = ?1 AND claim_id = ?2
    AND claimed_device_id = ?3 AND edge_id = ?4 AND status IN ('claimed', 'edge_ready')
    AND media_transport IN ('direct_pending', 'direct')`)
    .bind(offerId, input.claimId, device.device_id, device.edge_id).first<InboundOfferRecord>();
  if (!offer) throw unavailable();
  const expiresAt = tokenExpiryBeforeMint();
  const [edgeToken, phoneToken] = await Promise.all([
    issueParticipantToken(env, offer.room_name!, offer.edge_identity!, LIVEKIT_PARTICIPANT_TOKEN_TTL_SECONDS),
    issueParticipantToken(env, offer.room_name!, offer.phone_identity!, LIVEKIT_PARTICIPANT_TOKEN_TTL_SECONDS)
  ]);
  let iceServers: IceServer[] | undefined, sdpAnswer: string | undefined;
  try {
    iceServers = await edgeIce(env, offer);
    sdpAnswer = (await relayAnswer(env, offer, { ...mediaCommand(offer, env, edgeToken, "inbound.media.sdp"),
      direct: { sdpOffer: input.sdpOffer, iceServers } }))?.sdpAnswer;
  } catch (error) {
    if (error instanceof HttpError && error.status === 409) throw error;
    // TURN/relay failure retains ownership; the response permits same-claim fallback.
  }
  const current = await env.DB.prepare(`UPDATE inbound_offers SET media_transport = ?1 WHERE offer_id = ?2 AND claim_id = ?3
    AND claimed_device_id = ?4 AND status IN ('claimed', 'edge_ready') AND media_transport IN ('direct_pending', 'direct')
    AND EXISTS (SELECT device_id FROM devices WHERE device_id = ?4 AND revoked_at IS NULL) RETURNING offer_id`)
    .bind(sdpAnswer ? "direct" : offer.media_transport, offerId, input.claimId, device.device_id).first();
  if (!current) throw unavailable();
  return json({ ...sessionPayload(offer, env, phoneToken, expiresAt), directAttempted: true, sdpPending: false,
    transports: [...(sdpAnswer ? [{ kind: "webrtc", sdpAnswer, iceServers }] : []),
      { kind: "livekit", url: env.LIVEKIT_URL, token: phoneToken }] }, 202);
}

async function edgeIce(env: Env, offer: InboundOfferRecord, stub = edgeRoom(env, offer.edge_id)): Promise<IceServer[]> {
  const response = await stub.fetch("https://edge-room/inbound-ice", {
    method: "POST", body: JSON.stringify(iceRequest(offer, "get"))
  });
  if (!response.ok) throw new Error("ICE unavailable");
  return iceCredentialsSchema.parse(await response.json()).iceServers;
}

async function relayAnswer(env: Env, offer: InboundOfferRecord, command: { commandId: string; [key: string]: unknown },
  stub = edgeRoom(env, offer.edge_id)): Promise<ReturnType<typeof inboundAnswerSchema.parse> | null> {
  const response = await stub.fetch("https://edge-room/inbound-media-relay", {
    method: "POST", body: JSON.stringify(command)
  }).catch(() => null);
  if (response?.status === 409) throw unavailable();
  if (!response?.ok) return null;
  const answer = inboundAnswerSchema.safeParse(await response.json().catch(() => null));
  if (!answer.success || answer.data.commandId !== command.commandId || answer.data.offerId !== offer.offer_id
    || answer.data.callId !== offer.call_id || answer.data.claimId !== offer.claim_id) return null;
  if (answer.data.status !== "accepted") throw unavailable();
  return answer.data;
}

export async function endInboundMedia(request: Request, env: Env, offerId: string): Promise<Response> {
  requireSameOrigin(request, env.PUBLIC_ORIGIN);
  if (env.INBOUND_FASTPATH_V2_ENABLED !== "true") throw new HttpError("FEATURE_DISABLED", "Direct media is disabled", 403);
  const device = await authenticateDevice(request, env);
  const input = inboundFallbackSchema.parse(await readJson(request, 4096));
  const offer = await env.DB.prepare(`SELECT * FROM inbound_offers WHERE offer_id = ?1 AND claim_id = ?2
    AND claimed_device_id = ?3 AND edge_id = ?4 AND status IN ('claimed', 'edge_ready', 'revoked')
    AND media_transport IN ('direct_pending', 'direct', 'livekit_pending', 'livekit')`)
    .bind(offerId, input.claimId, device.device_id, device.edge_id).first<InboundOfferRecord>();
  if (!offer) throw unavailable();
  if (offer.status !== "revoked") {
    const token = await issueParticipantToken(env, offer.room_name!, offer.edge_identity!);
    const accepted = await relayAnswer(env, offer, { ...mediaCommand(offer, env, token, "inbound.media.end"),
      ...(input.clientTiming === undefined ? {} : { clientTiming: input.clientTiming }),
      ...(input.clientRtpStats === undefined ? {} : { clientRtpStats: input.clientRtpStats }) });
    if (!accepted) throw new HttpError("MEDIA_RELAY_UNAVAILABLE", "Media end response is unavailable", 503);
  }
  // Edge can independently publish revoke before its end answer reaches us.
  const current = await env.DB.prepare(`UPDATE inbound_offers SET status = 'revoked', updated_at = ?1
    WHERE offer_id = ?2 AND claim_id = ?3 AND claimed_device_id = ?4
      AND status IN ('claimed', 'edge_ready', 'revoked')
      AND EXISTS (SELECT device_id FROM devices WHERE device_id = ?4 AND revoked_at IS NULL) RETURNING offer_id`)
    .bind(Date.now(), offerId, input.claimId, device.device_id).first();
  if (!current) throw unavailable();
  return json({ accepted: true, offerId, claimId: input.claimId }, 202);
}

export async function fallbackInboundLivekit(request: Request, env: Env, offerId: string): Promise<Response> {
  requireSameOrigin(request, env.PUBLIC_ORIGIN);
  if (env.INBOUND_FASTPATH_V2_ENABLED !== "true") throw new HttpError("FEATURE_DISABLED", "Direct media is disabled", 403);
  const device = await authenticateDevice(request, env);
  const input = inboundFallbackSchema.parse(await readJson(request, 4096));
  const offer = await env.DB.prepare(`SELECT * FROM inbound_offers WHERE offer_id = ?1 AND claim_id = ?2
    AND claimed_device_id = ?3 AND edge_id = ?4 AND status IN ('claimed', 'edge_ready') AND media_transport IS NOT NULL`)
    .bind(offerId, input.claimId, device.device_id, device.edge_id).first<InboundOfferRecord>();
  if (!offer) throw unavailable();
  const expiresAt = tokenExpiryBeforeMint();
  const [edgeToken, phoneToken] = await Promise.all([
    issueParticipantToken(env, offer.room_name!, offer.edge_identity!, LIVEKIT_PARTICIPANT_TOKEN_TTL_SECONDS),
    issueParticipantToken(env, offer.room_name!, offer.phone_identity!, LIVEKIT_PARTICIPANT_TOKEN_TTL_SECONDS)
  ]);
  if (offer.media_transport !== "livekit") {
    const reserved = await env.DB.prepare(`UPDATE inbound_offers SET media_transport = 'livekit_pending'
      WHERE offer_id = ?1 AND claim_id = ?2 AND status IN ('claimed', 'edge_ready')
        AND media_transport IN ('direct', 'direct_pending', 'livekit_pending') RETURNING offer_id`).bind(offerId, input.claimId).first();
    if (!reserved) throw unavailable();
    const command = { ...mediaCommand(offer, env, edgeToken, "inbound.media.livekit"),
      ...(input.clientTiming === undefined ? {} : { clientTiming: input.clientTiming }),
      ...(input.clientRtpStats === undefined ? {} : { clientRtpStats: input.clientRtpStats }) };
    // A lost DO response leaves this same claim pending. Repeating the command
    // must be idempotent at Edge; never give another phone a fresh claim.
    const response = await edgeRoom(env, offer.edge_id).fetch("https://edge-room/inbound-media-relay", {
        method: "POST", body: JSON.stringify(command)
      }).catch(() => { throw new HttpError("MEDIA_RELAY_UNAVAILABLE", "Media switch response is unavailable", 503); });
    if (response.status === 504) throw new HttpError("MEDIA_RELAY_TIMEOUT", "Media switch response timed out", 504);
    if (response.status >= 500 || response.status === 429) {
      throw new HttpError("MEDIA_RELAY_UNAVAILABLE", "Media switch response is unavailable", 503);
    }
    const answer = inboundAnswerSchema.safeParse(await response.json().catch(() => null));
    if (!response.ok || !answer.success || answer.data.status !== "accepted"
        || answer.data.commandId !== command.commandId || answer.data.offerId !== offerId
        || answer.data.callId !== offer.call_id || answer.data.claimId !== input.claimId) {
      const reason = answer.success ? answer.data.errorCode : undefined;
      console.warn(JSON.stringify({ event: "inbound_livekit_fallback_rejected",
        reason: reason && ["MEDIA_FALLBACK_REJECTED", "LIVEKIT_SETUP_FAILED", "MEDIA_ENDED"].includes(reason)
          ? reason : "other" }));
      throw unavailable();
    }
    const current = await env.DB.prepare(`UPDATE inbound_offers SET media_transport = 'livekit', updated_at = ?1
      WHERE offer_id = ?2 AND claim_id = ?3 AND status IN ('claimed', 'edge_ready')
        AND media_transport IN ('livekit_pending', 'livekit') RETURNING offer_id`).bind(Date.now(), offerId, input.claimId).first();
    if (!current) throw unavailable();
  }
  const current = await env.DB.prepare(`SELECT o.offer_id FROM inbound_offers o JOIN devices d ON d.device_id = o.claimed_device_id
    WHERE o.offer_id = ?1 AND o.claim_id = ?2 AND o.claimed_device_id = ?3 AND o.status IN ('claimed', 'edge_ready')
      AND o.media_transport = 'livekit' AND d.revoked_at IS NULL`).bind(offerId, input.claimId, device.device_id).first();
  if (!current) throw unavailable();
  return json({ ...sessionPayload(offer, env, phoneToken, expiresAt), directAttempted: true,
    transports: [{ kind: "livekit", url: env.LIVEKIT_URL, token: phoneToken }] }, 202);
}

function mediaCommand(offer: InboundOfferRecord, env: Env, token: string, type: "inbound.claim" | "inbound.media.sdp" | "inbound.media.livekit" | "inbound.media.end") {
  return { v: 1, type, commandId: randomId("command"), offerId: offer.offer_id, callId: offer.call_id,
    claimId: offer.claim_id, generation: offer.generation, nonce: offer.nonce,
    session: { sessionId: randomId("session"), roomName: offer.room_name, browserIdentity: offer.phone_identity,
      edgeIdentity: offer.edge_identity, livekitUrl: env.LIVEKIT_URL, token } };
}

function tokenExpiryBeforeMint(): number {
  // JWT exp is second-granularity. Capturing before signing is conservative;
  // this credential lifetime never changes the offer fence or Edge media lease.
  return Math.floor(Date.now() / 1000) * 1000 + LIVEKIT_PARTICIPANT_TOKEN_TTL_SECONDS * 1000;
}

function sessionPayload(offer: InboundOfferRecord, env: Env, token: string, expiresAt: number) {
  return { claimId: offer.claim_id, offerId: offer.offer_id, callId: offer.call_id, roomName: offer.room_name,
    url: env.LIVEKIT_URL, token, participantIdentity: offer.phone_identity, edgeIdentity: offer.edge_identity, expiresAt };
}

function iceRequest(offer: InboundOfferRecord, mode: "warm" | "get") {
  return { offerId: offer.offer_id, generation: offer.generation, nonce: offer.nonce, expiresAt: offer.expires_at, mode };
}
