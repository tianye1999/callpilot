import { HttpError } from "./http";
import { issueParticipantToken, LiveKitParticipantError, participantRequest } from "./livekit";
import { randomId } from "./security";
import type { DeviceRecord, EdgePresence, Env, InboundOfferRecord } from "./types";

export const PRECONNECT_ACTIVE_INTERVAL_MS = 3000;
export const PRECONNECT_CLEANUP_INTERVAL_MS = 15_000;
const CLEANUP_CONCURRENCY = 4;

export interface Preparation {
  prepare_id: string;
  offer_id: string;
  device_id: string;
  room_name: string;
  participant_identity: string;
  expires_at: number;
  state: string;
}

export async function prepareInbound(env: Env, device: DeviceRecord, offerId: string) {
  if (env.INBOUND_PRECONNECT_ENABLED !== "true") throw unavailable();
  const presence = await env.EDGE_ROOMS.get(env.EDGE_ROOMS.idFromName(device.edge_id))
    .fetch("https://edge-room/presence");
  const capability = await presence.json() as EdgePresence;
  if (!capability.connected || !capability.inboundPreconnectV1) throw unavailable();
  // INSERT SELECT and the final read both fence against a racing claim/revoke.
  await env.DB.prepare(`INSERT OR IGNORE INTO inbound_preparations
    (prepare_id, offer_id, device_id, room_name, participant_identity, expires_at)
    SELECT ?1, offer_id, ?2, room_name, ?3, expires_at FROM inbound_offers
    WHERE offer_id = ?4 AND edge_id = ?5 AND status = 'offered' AND expires_at > ?6
      AND preconnect_enabled = 1`)
    .bind(randomId("prepare"), device.device_id, randomId("web", 12), offerId, device.edge_id, Date.now()).run();
  const row = await env.DB.prepare(`SELECT p.* FROM inbound_preparations p JOIN inbound_offers o ON o.offer_id = p.offer_id
    WHERE p.offer_id = ?1 AND p.device_id = ?2 AND p.state = 'prepared'
      AND o.edge_id = ?3 AND o.status = 'offered' AND o.expires_at > ?4`)
    .bind(offerId, device.device_id, device.edge_id, Date.now()).first<Preparation>();
  if (!row) throw new HttpError("OFFER_UNAVAILABLE", "Offer is not available for preparation", 409);
  const token = await issueParticipantToken(env, row.room_name, row.participant_identity,
    Math.max(1, Math.ceil((row.expires_at - Date.now()) / 1000)), true);
  return { prepareId: row.prepare_id, roomName: row.room_name, participantIdentity: row.participant_identity,
    url: env.LIVEKIT_URL, token, expiresAt: row.expires_at };
}

function unavailable() {
  return new HttpError("PRECONNECT_UNAVAILABLE", "Silent preconnect is not available", 409);
}

export async function queueParticipantCleanup(env: Env, offer: Pick<InboundOfferRecord, "edge_id" | "offer_id" | "expires_at">, room: string, identity: string) {
  // A reused Edge identity can receive a five-minute token when claimed just
  // before offer expiry. Persist that bound instead of extending it every sweep.
  await env.DB.prepare(`INSERT OR IGNORE INTO inbound_participant_cleanup
    (edge_id, offer_id, room_name, participant_identity, retry_until) VALUES (?1, ?2, ?3, ?4, ?5)`)
    .bind(offer.edge_id, offer.offer_id, room, identity, Math.max(offer.expires_at, Date.now()) + 360_000).run();
}

export async function invalidateCandidates(env: Env, offerId: string, winner?: string) {
  await env.DB.prepare(`UPDATE inbound_preparations SET state = CASE
    WHEN prepare_id = ?2 THEN 'winner' ELSE 'invalid' END WHERE offer_id = ?1
      AND state != 'invalid' AND (state != 'winner' OR prepare_id IS NOT ?2)`)
    .bind(offerId, winner ?? null).run();
}

// The DO alarm retries removals, including late joins with still-valid JWTs.
// Active winners are excluded; expiry only ends an unclaimed offer.
export async function sweepPreconnect(env: Env, edgeId: string): Promise<number | null> {
  const work = await env.DB.prepare(`SELECT 1 FROM inbound_offers INDEXED BY idx_inbound_offers_edge_status
    WHERE edge_id = ?1 AND preconnect_enabled = 1 AND status IN ('offered', 'claimed', 'edge_ready')
    UNION ALL SELECT 1 FROM inbound_participant_cleanup INDEXED BY inbound_cleanup_pending
    WHERE edge_id = ?1 AND completed = 0
    UNION ALL SELECT 1 FROM inbound_preparations p INDEXED BY inbound_preparations_live
    CROSS JOIN inbound_offers o ON o.offer_id = p.offer_id
    WHERE p.state != 'invalid' AND o.edge_id = ?1 AND o.status NOT IN ('offered', 'claimed', 'edge_ready')
    UNION ALL SELECT 1 FROM inbound_preparations p INDEXED BY inbound_preparations_unqueued
    CROSS JOIN inbound_offers o ON o.offer_id = p.offer_id
    WHERE p.state = 'invalid' AND p.cleanup_queued = 0 AND o.edge_id = ?1 LIMIT 1`)
    .bind(edgeId).first();
  if (!work) return null;
  await env.DB.prepare(`UPDATE inbound_offers INDEXED BY idx_inbound_offers_edge_status SET status = 'expired', updated_at = ?1
    WHERE edge_id = ?2 AND preconnect_enabled = 1 AND status = 'offered' AND expires_at <= ?1`)
    .bind(Date.now(), edgeId).run();
  // Drive joins from pending partial indexes, never the Edge's completed history.
  await env.DB.prepare(`UPDATE inbound_preparations SET state = 'invalid' WHERE state != 'invalid' AND prepare_id IN
    (SELECT p.prepare_id FROM inbound_preparations p INDEXED BY inbound_preparations_live
      CROSS JOIN inbound_offers o ON o.offer_id = p.offer_id
      WHERE p.state != 'invalid' AND o.edge_id = ?1 AND o.status NOT IN ('offered', 'claimed', 'edge_ready'))`)
    .bind(edgeId).run();
  await env.DB.prepare(`INSERT OR IGNORE INTO inbound_participant_cleanup
    (edge_id, offer_id, room_name, participant_identity, retry_until)
    SELECT o.edge_id, p.offer_id, p.room_name, p.participant_identity,
      MAX(p.expires_at + 60000, ?2, CASE WHEN p.participant_identity = o.phone_identity
        THEN o.updated_at + 360000 ELSE 0 END)
    FROM inbound_preparations p INDEXED BY inbound_preparations_unqueued
    CROSS JOIN inbound_offers o ON o.offer_id = p.offer_id
    WHERE o.edge_id = ?1 AND p.state = 'invalid' AND p.cleanup_queued = 0`)
    .bind(edgeId, Date.now() + 60_000).run();
  // Mark only after the durable queue exists; an interrupted sweep can safely retry.
  await env.DB.prepare(`UPDATE inbound_preparations SET cleanup_queued = 1
    WHERE state = 'invalid' AND cleanup_queued = 0 AND prepare_id IN
      (SELECT p.prepare_id FROM inbound_preparations p INDEXED BY inbound_preparations_unqueued
        CROSS JOIN inbound_offers o ON o.offer_id = p.offer_id
        WHERE p.state = 'invalid' AND p.cleanup_queued = 0 AND o.edge_id = ?1
          AND EXISTS (SELECT 1 FROM inbound_participant_cleanup c
            WHERE c.room_name = p.room_name AND c.participant_identity = p.participant_identity))`)
    .bind(edgeId).run();
  // A promoted phone candidate has a five-minute token; its bound is persisted
  // when queued above. Recent offer updates alone must never keep the alarm alive.
  const rows = await env.DB.prepare(`SELECT c.room_name, c.participant_identity, c.retry_until
    FROM inbound_participant_cleanup c INDEXED BY inbound_cleanup_pending
    CROSS JOIN inbound_offers o ON o.offer_id = c.offer_id
    WHERE c.edge_id = ?1 AND c.completed = 0 AND (o.status NOT IN ('offered', 'claimed', 'edge_ready') OR c.room_name != o.room_name
      OR c.participant_identity NOT IN (COALESCE(o.phone_identity, ''), COALESCE(o.edge_identity, '')) AND o.status != 'offered')`)
    .bind(edgeId).all<{ room_name: string; participant_identity: string; retry_until: number }>();
  let next = 0;
  let failed = 0;
  let completed = 0;
  let pending = 0;
  const reasons = new Map<string, { errorName: string; httpStatus?: number; code?: string; count: number }>();
  await Promise.all(Array.from({ length: Math.min(CLEANUP_CONCURRENCY, rows.results.length) }, async () => {
    while (next < rows.results.length) {
      const row = rows.results[next++]!;
      try {
        await participantRequest(env, "RemoveParticipant", row.room_name, row.participant_identity);
      } catch (error) {
        failed++;
        // Never log messages, stacks, request fields, or arbitrary upstream names.
        const reason = error instanceof LiveKitParticipantError
          ? { errorName: error.name, httpStatus: error.httpStatus, code: error.code }
          : { errorName: error instanceof Error && ["Error", "TypeError", "SyntaxError", "AbortError", "TimeoutError"]
            .includes(error.name) ? error.name : "UnknownError" };
        const key = JSON.stringify(reason);
        const existing = reasons.get(key);
        if (existing) existing.count++;
        else reasons.set(key, { ...reason, count: 1 });
      }
      // One final removal attempt, then retire even on failure. The stored
      // deadline already bounds token re-entry; an outage must not retry forever.
      if (row.retry_until <= Date.now()) {
        await env.DB.prepare(`UPDATE inbound_participant_cleanup SET completed = 1
          WHERE room_name = ?1 AND participant_identity = ?2 AND completed = 0`).bind(row.room_name, row.participant_identity).run();
        completed++;
      } else pending++;
    }
  }));
  if (failed) console.warn("Inbound participant cleanup summary", {
    attempted: rows.results.length, failed, completed, pending, reasons: [...reasons.values()]
  });
  const active = await env.DB.prepare(`SELECT offer_id FROM inbound_offers INDEXED BY idx_inbound_offers_edge_status
    WHERE edge_id = ?1 AND preconnect_enabled = 1
    AND status IN ('offered', 'claimed', 'edge_ready') LIMIT 1`)
    .bind(edgeId).first();
  return active !== null ? PRECONNECT_ACTIVE_INTERVAL_MS : pending ? PRECONNECT_CLEANUP_INTERVAL_MS : null;
}
