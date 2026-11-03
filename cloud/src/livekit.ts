import { AccessToken, TrackSource } from "livekit-server-sdk";

import type { Env } from "./types";

export const LIVEKIT_PARTICIPANT_TOKEN_TTL_SECONDS = 300;

export class LiveKitParticipantError extends Error {
  override name = "LiveKitParticipantError";
  readonly code?: string;

  constructor(readonly httpStatus: number, code: unknown) {
    super("LiveKit participant request failed");
    // Only protocol codes are loggable; upstream messages can contain identities.
    if (typeof code === "string" && ["canceled", "unknown", "invalid_argument", "malformed",
      "deadline_exceeded", "not_found", "bad_route", "already_exists", "permission_denied",
      "unauthenticated", "resource_exhausted", "failed_precondition", "aborted", "out_of_range",
      "unimplemented", "internal", "unavailable", "data_loss"].includes(code)) this.code = code;
  }
}

export async function issueParticipantToken(
  env: Env,
  room: string,
  identity: string,
  ttlSeconds = LIVEKIT_PARTICIPANT_TOKEN_TTL_SECONDS,
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
  env: Env, method: "UpdateParticipant" | "RemoveParticipant" | "DeleteRoom", room: string,
  identity: string, grant = false, budgetMs = 1200
): Promise<void> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), budgetMs);
  try {
    const auth = new AccessToken(env.LIVEKIT_API_KEY, env.LIVEKIT_API_SECRET, { ttl: 60 });
    auth.addGrant(method === "DeleteRoom" ? { roomCreate: true } : { room, roomAdmin: true });
    const token = await auth.toJwt();
    controller.signal.throwIfAborted();
    const url = new URL(env.LIVEKIT_URL);
    if (url.protocol === "wss:") url.protocol = "https:";
    else if (url.protocol === "ws:") url.protocol = "http:";
    url.pathname = `/twirp/livekit.RoomService/${method}`;
    const response = await fetch(url, {
      method: "POST", signal: controller.signal,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ room, ...(method === "DeleteRoom" ? {} : { identity }), ...(method === "UpdateParticipant" ? {
        permission: {
          canPublish: grant, canSubscribe: grant, canPublishData: grant,
          canPublishSources: grant ? [TrackSource.MICROPHONE] : [],
          canUpdateMetadata: false, hidden: false, recorder: false, agent: false,
          canSubscribeMetrics: false, canManageAgentSession: false
        }
      } : {}) })
    });
    const payload = await response.json().catch((error: unknown) => {
      if (!response.ok && method !== "UpdateParticipant") throw new LiveKitParticipantError(response.status, undefined);
      throw error;
    }) as { code?: string } | null;
    if (!response.ok && !(method !== "UpdateParticipant" && payload?.code === "not_found")) {
      if (method === "RemoveParticipant") throw new LiveKitParticipantError(response.status, payload?.code);
      throw new Error("LiveKit participant request failed");
    }
  } finally {
    clearTimeout(timeout);
  }
}
