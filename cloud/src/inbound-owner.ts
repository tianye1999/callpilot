import { authenticateDevice } from "./auth";
import { HttpError, requireSameOrigin } from "./http";
import type { Env } from "./types";

export async function rejectOwnerDecision(env: Env, edgeId: string, offerId: string, deviceId: string): Promise<void> {
  if (env.INBOUND_MULTI_DEVICE_STOP_ENABLED !== "true" && env.INBOUND_OWNER_DECLINE_ENABLED !== "true") return;
  const row = await env.DB.prepare("SELECT status, claimed_device_id, error_code FROM inbound_offers WHERE offer_id=?1 AND edge_id=?2")
    .bind(offerId, edgeId).first<{status: string; claimed_device_id: string | null; error_code: string | null}>();
  if (env.INBOUND_OWNER_DECLINE_ENABLED === "true" && row?.error_code === "OWNER_DECLINED")
    throw new HttpError("OFFER_OWNER_DECLINED", "Owner delegated this call to AI", 409);
  if (env.INBOUND_MULTI_DEVICE_STOP_ENABLED === "true" && row && ["claimed", "edge_ready"].includes(row.status)
      && row.claimed_device_id !== deviceId)
    throw new HttpError("OFFER_ANSWERED_ELSEWHERE", "Answered on another device", 409);
}

export async function notifyOwnerDecision(env: Env, edgeId: string, offerId: string): Promise<void> {
  if (env.INBOUND_MULTI_DEVICE_STOP_ENABLED !== "true" && env.INBOUND_OWNER_DECLINE_ENABLED !== "true") return;
  const stub = env.EDGE_ROOMS.get(env.EDGE_ROOMS.idFromName(edgeId));
  try {
    await stub.fetch("https://edge-room/owner-decision", { method: "POST", body: JSON.stringify({ offerId }) });
  } catch {
    // A notification failure must never revoke the winning call.
    console.warn("Inbound owner notification unavailable");
  }
}

export async function inboundOwnerAction(request: Request, env: Env, offerId: string, action: string): Promise<Response> {
  requireSameOrigin(request, env.PUBLIC_ORIGIN);
  if (action === "decline" ? env.INBOUND_OWNER_DECLINE_ENABLED !== "true"
    : env.INBOUND_MULTI_DEVICE_STOP_ENABLED !== "true" && env.INBOUND_OWNER_DECLINE_ENABLED !== "true") {
    throw new HttpError("FEATURE_DISABLED", "Feature disabled", 404);
  }
  const device = await authenticateDevice(request, env);
  const stub = env.EDGE_ROOMS.get(env.EDGE_ROOMS.idFromName(device.edge_id));
  return stub.fetch("https://edge-room/owner-action", { method: "POST",
    body: JSON.stringify({ offerId, action, edgeId: device.edge_id, deviceId: device.device_id }) });
}
