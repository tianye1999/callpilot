import type { Env } from "./types";
import { dispatchApnsPushes, voipPushEnabled } from "./voip-push";

export type AlertKind = "sms" | "call_digest" | "missed_call";

export function buildAlertPayload(kind: AlertKind) {
  return {
    aps: { alert: { "loc-key": `push.${kind}` }, sound: "default" },
    v: 1, type: "device.notify", kind
  };
}

export async function reserveAlertEvent(
  env: Env, edgeId: string, eventId: string, now = Date.now()
): Promise<boolean> {
  if (!voipPushEnabled(env.ALERT_PUSH_ENABLED)) return false;
  // Seven-day replay protection. Atomic reservation also caps every recipient
  // of this Edge at 20 alerts in a rolling minute, across reconnects/restarts.
  await env.DB.prepare("DELETE FROM alert_push_events WHERE created_at < ?1")
    .bind(now - 7 * 86_400_000).run();
  const result = await env.DB.prepare(
    "INSERT OR IGNORE INTO alert_push_events(edge_id, event_id, created_at) SELECT ?1, ?2, ?3 WHERE (SELECT COUNT(*) FROM alert_push_events WHERE edge_id = ?1 AND created_at > ?4) < 20"
  ).bind(edgeId, eventId, now, now - 60_000).run();
  return Boolean(result.meta.changes);
}

export async function dispatchAlertPushes(
  env: Env, edgeId: string, kind: AlertKind, fetcher: typeof fetch = fetch
): Promise<void> {
  if (!voipPushEnabled(env.ALERT_PUSH_ENABLED)) return;
  await dispatchApnsPushes(env, {
    edgeId, pushType: "alert", payload: buildAlertPayload(kind),
    expiresAtUnixMs: Date.now() + 3_600_000
  }, fetcher);
}
