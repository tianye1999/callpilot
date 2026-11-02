import { HttpError } from "./http";
import { iceServerSchema } from "./schemas";
import type { Env } from "./types";
import { z } from "zod";

export type IceServer = z.infer<typeof iceServerSchema>;
export const iceCredentialsSchema = z.object({ iceServers: z.array(iceServerSchema).min(1).max(16), expiresAt: z.number().int().positive() });
export type IceCredentials = z.infer<typeof iceCredentialsSchema>;
export const TURN_REQUEST_MAX_MS = 10_000;
// Covers the call, not just ICE setup; refreshing mid-call is not implemented.
// 24-hour configured call maximum plus the ringing/setup resource lease.
const TURN_CREDENTIAL_TTL_SECONDS = 86400 + 120;

export function turnConfigured(env: Env): boolean {
  return Boolean(env.TURN_KEY_ID?.trim() && env.TURN_API_TOKEN?.trim());
}

export async function issueIceServers(env: Env): Promise<{ iceServers: IceServer[]; expiresAt: number }> {
  if (!turnConfigured(env)) throw unavailable();
  const requestedAt = Date.now();
  const controller = new AbortController();
  let timeout: ReturnType<typeof setTimeout> | undefined;
  try {
    // A resource upper bound, not a warm/cold decision based on measured RTT.
    return await Promise.race([
      (async () => {
        const response = await fetch(`https://rtc.live.cloudflare.com/v1/turn/keys/${encodeURIComponent(env.TURN_KEY_ID!)}/credentials/generate-ice-servers`, {
          method: "POST", signal: controller.signal,
          headers: { Authorization: `Bearer ${env.TURN_API_TOKEN}`, "Content-Type": "application/json", "User-Agent": "callpilot-sim-relay/1" },
          body: JSON.stringify({ ttl: TURN_CREDENTIAL_TTL_SECONDS })
        });
        if (!response.ok) throw unavailable();
        const payload = z.object({ iceServers: z.array(iceServerSchema).min(1).max(16) }).parse(await response.json());
        const iceServers = payload.iceServers.map(server => ({ ...server,
          urls: server.urls.filter(url => /^(?:stun|stuns|turn|turns):[^\s]+$/.test(url) && !/:53(?:\?|$)/.test(url))
        })).filter(server => server.urls.length > 0);
        if (!iceServers.some(server => server.urls.some(url => /^turns?:/.test(url)) && server.username && server.credential)) {
          throw unavailable();
        }
        return { iceServers, expiresAt: requestedAt + TURN_CREDENTIAL_TTL_SECONDS * 1000 };
      })(),
      new Promise<never>((_resolve, reject) => {
        timeout = setTimeout(() => { controller.abort(); reject(unavailable()); }, TURN_REQUEST_MAX_MS);
      })
    ]);
  } catch {
    // Upstream errors may contain credentials: never propagate their message.
    throw unavailable();
  } finally {
    if (timeout !== undefined) clearTimeout(timeout);
    controller.abort();
  }
}

function unavailable() { return new HttpError("TURN_UNAVAILABLE", "Direct media is not available", 503); }
