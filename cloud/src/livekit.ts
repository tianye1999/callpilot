import { AccessToken } from "livekit-server-sdk";

import type { Env } from "./types";

export async function issueParticipantToken(
  env: Env,
  room: string,
  identity: string,
  ttlSeconds = 300
): Promise<string> {
  if (!env.LIVEKIT_API_KEY || !env.LIVEKIT_API_SECRET) throw new Error("LiveKit is not configured");
  const token = new AccessToken(env.LIVEKIT_API_KEY, env.LIVEKIT_API_SECRET, {
    identity,
    ttl: ttlSeconds
  });
  token.addGrant({
    room,
    roomJoin: true,
    canPublish: true,
    canSubscribe: true,
    canPublishData: true,
    canPublishSources: [1]
  });
  return token.toJwt();
}

