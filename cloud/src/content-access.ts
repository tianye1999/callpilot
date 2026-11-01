import { HttpError } from "./http";
import type { Env } from "./types";

export function contentReadEnabled(value: string | undefined): boolean {
  return value === "true";
}

export function contentRelayTimeoutMs(env: Env): number {
  return boundedInteger(env.CONTENT_RELAY_TIMEOUT_MS, 5_000, 100, 10_000);
}

// One instance per EdgeRoom; accepted requests bound both timestamps and keys.
export class ContentReadRateLimiter {
  private readonly deviceEvents = new Map<string, number[]>();
  private edgeEvents: number[] = [];

  enforce(env: Env, deviceId: string): void {
    const windowMs = 60_000;
    const now = Date.now();
    const cutoff = now - windowMs;
    this.edgeEvents = this.edgeEvents.filter((time) => time > cutoff);
    for (const [key, events] of this.deviceEvents) {
      const active = events.filter((time) => time > cutoff);
      if (active.length) this.deviceEvents.set(key, active);
      else this.deviceEvents.delete(key);
    }
    const deviceEvents = this.deviceEvents.get(deviceId) ?? [];
    const limits: Array<[number[], number]> = [
      [deviceEvents, boundedInteger(env.CONTENT_READ_DEVICE_LIMIT, 60, 1, 10_000)],
      [this.edgeEvents, boundedInteger(env.CONTENT_READ_EDGE_LIMIT, 300, 1, 50_000)]
    ];
    let retryAfter = 0;
    for (const [events, limit] of limits) {
      if (events.length < limit) continue;
      retryAfter = Math.max(retryAfter, 1, Math.ceil(((events[0] ?? now) + windowMs - now) / 1000));
    }
    if (retryAfter) {
      throw new HttpError(
        "RATE_LIMITED",
        "Too many content requests",
        429,
        { "Retry-After": String(retryAfter) }
      );
    }
    deviceEvents.push(now);
    this.deviceEvents.set(deviceId, deviceEvents);
    this.edgeEvents.push(now);
  }
}

function boundedInteger(value: string | undefined, fallback: number, min: number, max: number): number {
  if (!value || !/^\d+$/.test(value)) return fallback;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= min && parsed <= max ? parsed : fallback;
}
