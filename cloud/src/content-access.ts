import { HttpError } from "./http";
import { randomId } from "./security";
import type { DeviceRecord, Env } from "./types";

export function contentReadEnabled(value: string | undefined): boolean {
  return value === "true";
}

export function contentRelayTimeoutMs(env: Env): number {
  return boundedInteger(env.CONTENT_RELAY_TIMEOUT_MS, 5_000, 100, 10_000);
}

export async function enforceContentRateLimit(env: Env, device: DeviceRecord): Promise<void> {
  const windowMs = 60_000;
  const now = Date.now();
  const cutoff = now - windowMs;
  await env.DB.prepare(
    "DELETE FROM content_read_rate_events WHERE occurred_at <= ?1"
  ).bind(cutoff).run();
  const limits: Array<[string, number]> = [
    [`content:device:${device.device_id}`, boundedInteger(env.CONTENT_READ_DEVICE_LIMIT, 60, 1, 10_000)],
    [`content:edge:${device.edge_id}`, boundedInteger(env.CONTENT_READ_EDGE_LIMIT, 300, 1, 50_000)]
  ];
  for (const [key, limit] of limits) {
    const inserted = await env.DB.prepare(
      "INSERT INTO content_read_rate_events(event_id, scope_key, occurred_at) SELECT ?1, ?2, ?3 WHERE (SELECT COUNT(*) FROM content_read_rate_events WHERE scope_key = ?2 AND occurred_at > ?4) < ?5 RETURNING event_id"
    ).bind(randomId("rate", 12), key, now, cutoff, limit).first<{ event_id: string }>();
    if (!inserted) {
      const oldest = await env.DB.prepare(
        "SELECT MIN(occurred_at) AS occurred_at FROM content_read_rate_events WHERE scope_key = ?1 AND occurred_at > ?2"
      ).bind(key, cutoff).first<{ occurred_at: number | null }>();
      const retryAfter = Math.max(
        1,
        Math.ceil(((oldest?.occurred_at ?? now) + windowMs - now) / 1000)
      );
      throw new HttpError(
        "RATE_LIMITED",
        "Too many content requests",
        429,
        { "Retry-After": String(retryAfter) }
      );
    }
  }
}

function boundedInteger(value: string | undefined, fallback: number, min: number, max: number): number {
  if (!value || !/^\d+$/.test(value)) return fallback;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= min && parsed <= max ? parsed : fallback;
}
