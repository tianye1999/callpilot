import { describe, expect, it } from "vitest";

import { issueParticipantToken } from "../src/livekit";
import type { Env } from "../src/types";

describe("LiveKit token grants", () => {
  it("allows the microphone source used by both browser and Edge", async () => {
    const env = {
      LIVEKIT_API_KEY: "test-key",
      LIVEKIT_API_SECRET: "test-secret-that-is-long-enough-for-hmac",
    } as Env;

    const token = await issueParticipantToken(
      env,
      "callpilot_testroom",
      "web_testidentity",
    );
    const encodedPayload = token.split(".")[1];
    expect(encodedPayload).toBeDefined();
    const payload = JSON.parse(
      Buffer.from(encodedPayload ?? "", "base64url").toString("utf8"),
    ) as { video?: { canPublishSources?: string[] } };

    expect(payload.video?.canPublishSources).toEqual(["microphone"]);
  });
});
