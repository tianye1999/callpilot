import { describe, expect, it } from "vitest";
import { buildAlertPayload, reserveAlertEvent } from "../src/alert-push";
import { edgeMessageSchema } from "../src/schemas";
import type { Env } from "../src/types";

describe("alert push boundary", () => {
  it("accepts only opaque, content-free events", () => {
    const event = { v: 1, type: "device.notify", kind: "sms", eventId: `event_${"a".repeat(64)}` };
    expect(edgeMessageSchema.safeParse(event).success).toBe(true);
    expect(edgeMessageSchema.safeParse({ ...event, body: "private" }).success).toBe(false);
    expect(edgeMessageSchema.safeParse({ ...event, edgeId: "edge_other" }).success).toBe(false);
    expect(edgeMessageSchema.safeParse({ ...event, kind: "unknown" }).success).toBe(false);
    expect(edgeMessageSchema.safeParse({ ...event, eventId: "phone-123" }).success).toBe(false);
  });

  it.each(["sms", "call_digest", "missed_call"] as const)("uses only localization keys for %s", (kind) => {
    expect(buildAlertPayload(kind)).toEqual({
      aps: { alert: { "loc-key": `push.${kind}` }, sound: "default" },
      v: 1, type: "device.notify", kind
    });
  });

  it("does not touch storage when disabled", async () => {
    expect(await reserveAlertEvent({} as Env, "edge_test", "event_test")).toBe(false);
  });
});
