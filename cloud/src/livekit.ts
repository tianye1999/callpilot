import { AccessToken, TrackSource } from "livekit-server-sdk";

import type { Env } from "./types";

export async function issueParticipantToken(
  env: Env,
  room: string,
  identity: string,
  ttlSeconds = 300,
  restricted = false
): Promise<string> {
  if (!env.LIVEKIT_API_KEY || !env.LIVEKIT_API_SECRET) throw new Error("LiveKit is not configured");
  const token = new AccessToken(env.LIVEKIT_API_KEY, env.LIVEKIT_API_SECRET, {
    identity,
    ttl: ttlSeconds
  });
  token.addGrant({
    room,
    roomJoin: true,
    canPublish: !restricted,
    canSubscribe: !restricted,
    canPublishData: !restricted,
    canPublishSources: [TrackSource.MICROPHONE]
  });
  return token.toJwt();
}

// Use one abortable Twirp request, without SDK retry/failover extending the
// claim budget. A timeout is ambiguous; the caller must quarantine the identity.
export async function participantRequest(
  env: Env, method: "UpdateParticipant" | "RemoveParticipant", room: string,
  identity: string, grant = false, budgetMs = 1200
): Promise<void> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), budgetMs);
  try {
    const auth = new AccessToken(env.LIVEKIT_API_KEY, env.LIVEKIT_API_SECRET, { ttl: 60 });
    auth.addGrant({ room, roomAdmin: true });
    const token = await auth.toJwt();
    controller.signal.throwIfAborted();
    const url = new URL(env.LIVEKIT_URL);
    if (url.protocol === "wss:") url.protocol = "https:";
    else if (url.protocol === "ws:") url.protocol = "http:";
    url.pathname = `/twirp/livekit.RoomService/${method}`;
    const response = await fetch(url, {
      method: "POST", signal: controller.signal,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ room, identity, ...(method === "UpdateParticipant" ? {
        permission: {
          canPublish: grant, canSubscribe: grant, canPublishData: grant,
          canPublishSources: grant ? [TrackSource.MICROPHONE] : [],
          canUpdateMetadata: false, hidden: false, recorder: false, agent: false,
          canSubscribeMetrics: false, canManageAgentSession: false
        }
      } : {}) })
    });
    const payload = await response.json() as { code?: string };
    if (!response.ok && !(method === "RemoveParticipant" && payload.code === "not_found")) {
      throw new Error("LiveKit participant request failed");
    }
  } finally {
    clearTimeout(timeout);
  }
}
