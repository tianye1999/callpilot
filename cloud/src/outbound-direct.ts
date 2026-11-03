// A8: app-dialed calls over direct WebRTC, with the existing LiveKit room kept as
// the one same-call fallback. The relay never stores SDP or ICE credentials.
import { authenticateDevice } from "./auth";
import { ClaimTiming } from "./claim-timing";
import { HttpError, json, readJson, requireSameOrigin } from "./http";
import { issueParticipantToken, LIVEKIT_PARTICIPANT_TOKEN_TTL_SECONDS } from "./livekit";
import { callAnswerSchema, outboundIceSchema } from "./schemas";
import { randomId } from "./security";
import { issueIceServers, turnConfigured } from "./turn";
import type { CallRecord, EdgePresence, Env } from "./types";

type StartCommand = {
  v: 1; type: "session.start"; commandId: string; callId: string; expiresAt: string;
  session: Record<string, unknown>;
};

function edgeRoom(env: Env, edgeId: string) { return env.EDGE_ROOMS.get(env.EDGE_ROOMS.idFromName(edgeId)); }
function unavailable() { return new HttpError("CALL_UNAVAILABLE", "Call is no longer available", 409); }

export function outboundDirectEnabled(env: Env): boolean {
  return env.OUTBOUND_DIRECT_ENABLED === "true" && turnConfigured(env);
}

export function outboundDirectAvailable(env: Env, presence: Pick<EdgePresence, "outboundDirectV1"> | null): boolean {
  return outboundDirectEnabled(env) && presence?.outboundDirectV1 === true;
}

/** Phone ICE credentials for building a complete offer before creating the call. */
export async function outboundIce(request: Request, env: Env, timing = new ClaimTiming()): Promise<Response> {
  requireSameOrigin(request, env.PUBLIC_ORIGIN);
  const device = await timing.measure("auth", () => authenticateDevice(request, env));
  const input = await timing.measure("request_parse", async () => outboundIceSchema.parse(await readJson(request, 4096)));
  if (device.edge_id !== input.edgeId) throw new HttpError("FORBIDDEN", "Device is not paired to this Edge", 403);
  if (!outboundDirectEnabled(env)) throw new HttpError("DIRECT_UNAVAILABLE", "Direct media is not available", 503);
  const presence = await timing.measure("presence", async () =>
    (await edgeRoom(env, input.edgeId).fetch("https://edge-room/presence")).json() as Promise<EdgePresence>);
  if (!presence.connected || !outboundDirectAvailable(env, presence)) {
    throw new HttpError("DIRECT_UNAVAILABLE", "Direct media is not available", 503);
  }
  return json(await timing.measure("turn", () => issueIceServers(env)));
}

/** Payload shared by create, idempotent replay and GET. */
export function outboundPayload(call: CallRecord, base: Record<string, unknown>): Record<string, unknown> {
  return call.media_transport ? { ...base, directAttempted: true } : base;
}

/**
 * Deliver session.start with the phone's offer and wait for the Edge answer.
 * Returns null when Edge-side TURN credentials are unavailable, so the caller
 * can start the same call as a plain LiveKit session instead.
 */
export async function startOutboundDirect(env: Env, command: StartCommand, sdpOffer: string,
  edgeId: string, payload: (update: D1PreparedStatement) => Promise<Record<string, unknown>>, timing = new ClaimTiming()): Promise<Response | null> {
  let iceServers;
  try {
    iceServers = (await timing.measure("turn", () => issueIceServers(env))).iceServers;
  } catch {
    await timing.measure("call_finalize", () => env.DB.prepare("UPDATE calls SET media_transport = NULL, updated_at = ?1 WHERE call_id = ?2 AND status = 'pending'")
      .bind(Date.now(), command.callId).run());
    return null;
  }
  const response = await timing.measure("sdp_relay", () => edgeRoom(env, edgeId).fetch("https://edge-room/call-media-relay", {
    method: "POST", body: JSON.stringify({ ...command, direct: { sdpOffer, iceServers } })
  }).catch(() => null));
  if (response) timing.absorbRelay(response);
  const answer = await timing.measure("answer_parse", async () => response && (response.ok || response.status === 409)
    ? callAnswerSchema.safeParse(await response.json().catch(() => null)) : null);
  const now = Date.now();
  if (!answer?.success || answer.data.commandId !== command.commandId || answer.data.callId !== command.callId) {
    const timeout = response?.status === 504;
    await timing.measure("call_finalize", () => env.DB.prepare("UPDATE calls SET status = 'failed', error_code = ?1, updated_at = ?2 WHERE call_id = ?3 AND status = 'pending'")
      .bind(timeout ? "EDGE_TIMEOUT" : "EDGE_OFFLINE", now, command.callId).run());
    throw timeout ? new HttpError("EDGE_TIMEOUT", "Edge did not answer in time", 504)
      : new HttpError("EDGE_OFFLINE", "Edge disconnected before the call could start", 409);
  }
  if (answer.data.status === "accepted" && answer.data.sdpAnswer) {
    const base = await payload(env.DB.prepare(`UPDATE calls SET status = 'ready', media_transport = 'direct', updated_at = ?1
      WHERE call_id = ?2 AND status = 'pending'`).bind(now, command.callId));
    return json({ ...base, directAttempted: true,
      transports: [{ kind: "webrtc", sdpAnswer: answer.data.sdpAnswer }] }, 202);
  }
  if (answer.data.errorCode === "DIRECT_SETUP_FAILED") {
    // The Edge session is live and waits for the same-call LiveKit fallback.
    const base = await payload(env.DB.prepare("UPDATE calls SET status = 'ready', updated_at = ?1 WHERE call_id = ?2 AND status = 'pending'")
      .bind(now, command.callId));
    return json({ ...base, directAttempted: true, transports: [] }, 202);
  }
  const base = await payload(env.DB.prepare("UPDATE calls SET status = 'failed', error_code = ?1, updated_at = ?2 WHERE call_id = ?3 AND status = 'pending'")
    .bind(answer.data.errorCode ?? "EDGE_REJECTED", now, command.callId));
  return json(base, 202);
}

/** Switch a direct call to its LiveKit room once; repeats are idempotent at the Edge. */
export async function fallbackOutboundLivekit(request: Request, env: Env, callId: string): Promise<Response> {
  requireSameOrigin(request, env.PUBLIC_ORIGIN);
  if (env.OUTBOUND_DIRECT_ENABLED !== "true") throw new HttpError("FEATURE_DISABLED", "Direct media is disabled", 403);
  const device = await authenticateDevice(request, env);
  const now = Date.now();
  const call = await env.DB.prepare(`SELECT * FROM calls WHERE call_id = ?1 AND device_id = ?2 AND edge_id = ?3
    AND status = 'ready' AND expires_at > ?4 AND media_transport IS NOT NULL`)
    .bind(callId, device.device_id, device.edge_id, now).first<CallRecord>();
  if (!call) throw unavailable();
  const expiresAt = Math.floor(Date.now() / 1000) * 1000 + LIVEKIT_PARTICIPANT_TOKEN_TTL_SECONDS * 1000;
  const [edgeToken, phoneToken] = await Promise.all([
    issueParticipantToken(env, call.room_name, call.edge_identity),
    issueParticipantToken(env, call.room_name, call.phone_identity)
  ]);
  if (call.media_transport !== "livekit") {
    const reserved = await env.DB.prepare(`UPDATE calls SET media_transport = 'livekit_pending', updated_at = ?1
      WHERE call_id = ?2 AND status = 'ready' AND media_transport IN ('direct_pending', 'direct', 'livekit_pending')
      RETURNING call_id`).bind(now, callId).first();
    if (!reserved) throw unavailable();
    const command = { v: 1, type: "call.media.livekit", commandId: randomId("command"), callId,
      session: { sessionId: randomId("session"), roomName: call.room_name, browserIdentity: call.phone_identity,
        edgeIdentity: call.edge_identity, livekitUrl: env.LIVEKIT_URL, token: edgeToken } };
    const response = await edgeRoom(env, call.edge_id).fetch("https://edge-room/call-media-relay", {
      method: "POST", body: JSON.stringify(command)
    }).catch(() => { throw new HttpError("MEDIA_RELAY_UNAVAILABLE", "Media switch response is unavailable", 503); });
    if (response.status === 504) throw new HttpError("MEDIA_RELAY_TIMEOUT", "Media switch response timed out", 504);
    if (response.status >= 500 || response.status === 429) {
      throw new HttpError("MEDIA_RELAY_UNAVAILABLE", "Media switch response is unavailable", 503);
    }
    const answer = callAnswerSchema.safeParse(await response.json().catch(() => null));
    if (!response.ok || !answer.success || answer.data.status !== "accepted"
        || answer.data.commandId !== command.commandId || answer.data.callId !== callId) {
      const reason = answer.success ? answer.data.errorCode : undefined;
      console.warn(JSON.stringify({ event: "outbound_livekit_fallback_rejected",
        reason: reason && ["MEDIA_FALLBACK_REJECTED", "LIVEKIT_SETUP_FAILED", "MEDIA_ENDED"].includes(reason)
          ? reason : "other" }));
      throw unavailable();
    }
    const current = await env.DB.prepare(`UPDATE calls SET media_transport = 'livekit', updated_at = ?1
      WHERE call_id = ?2 AND status = 'ready' AND media_transport IN ('livekit_pending', 'livekit') RETURNING call_id`)
      .bind(Date.now(), callId).first();
    if (!current) throw unavailable();
  }
  return json({ callId, session: { livekitUrl: env.LIVEKIT_URL, token: phoneToken, expiresAt } }, 202);
}
