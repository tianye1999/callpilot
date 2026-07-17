import { describe, expect, it } from "vitest";

import { liveKitConnectSources } from "../src/csp";
import {
  bearerCredential,
  constantTimeEqual,
  cookieCredential,
  randomId,
  randomPairingCode,
  randomSecret,
  sha256
} from "../src/security";
import {
  claimPairingSchema,
  createCallSchema,
  createPairingSchema,
  edgeMessageSchema,
  registerVoipTokenSchema
} from "../src/schemas";

describe("credential helpers", () => {
  it("creates opaque identifiers and secrets without punctuation separators", () => {
    expect(randomId("edge")).toMatch(/^edge_[A-Za-z0-9_-]{20,}$/);
    expect(randomSecret()).toMatch(/^[A-Za-z0-9_-]{40,}$/);
    expect(randomPairingCode()).toMatch(/^[23456789A-HJ-NP-Z]{4}-[23456789A-HJ-NP-Z]{4}$/);
  });

  it("hashes secrets deterministically and compares without plaintext persistence", async () => {
    expect(await sha256("secret")).toBe(await sha256("secret"));
    expect(await sha256("secret")).not.toContain("secret");
    expect(await constantTimeEqual("same", "same")).toBe(true);
    expect(await constantTimeEqual("same", "different")).toBe(false);
  });

  it("accepts only bounded bearer and cookie credentials", () => {
    const secret = "s".repeat(40);
    const bearer = new Request("https://example.test", {
      headers: { Authorization: `Bearer edge_abcdefghijkl.${secret}` }
    });
    expect(bearerCredential(bearer)).toEqual(["edge_abcdefghijkl", secret]);
    expect(bearerCredential(new Request("https://example.test"))).toBeNull();

    const cookie = new Request("https://example.test", {
      headers: { Cookie: `other=x; __Host-callpilot-device=device_abcdefghijkl.${secret}` }
    });
    expect(cookieCredential(cookie)).toEqual(["device_abcdefghijkl", secret]);
  });
});

describe("protocol schema", () => {
  it("rejects unknown and unbounded call fields", () => {
    expect(createCallSchema.safeParse({ edgeId: "edge_abcdefghijkl", idempotencyKey: "x".repeat(16) }).success).toBe(true);
    expect(createCallSchema.safeParse({ edgeId: "edge_abcdefghijkl", idempotencyKey: "x".repeat(16), number: "10000" }).success).toBe(false);
  });

  it("normalizes only syntax at the pairing boundary", () => {
    expect(claimPairingSchema.safeParse({ code: "ABCD-EFGH", displayName: "Phone" }).success).toBe(true);
    expect(claimPairingSchema.safeParse({ code: "0000-0000", displayName: "Phone" }).success).toBe(false);
  });

  it("keeps pairing purpose explicit while preserving the legacy default", () => {
    expect(createPairingSchema.parse({ ttlSeconds: 300 })).toEqual({
      ttlSeconds: 300,
      purpose: "standard"
    });
    expect(createPairingSchema.safeParse({
      ttlSeconds: 604_800,
      purpose: "app_review"
    }).success).toBe(true);
    expect(createPairingSchema.safeParse({
      ttlSeconds: 604_801,
      purpose: "app_review"
    }).success).toBe(false);
    expect(createPairingSchema.safeParse({ ttlSeconds: 300, purpose: "other" }).success).toBe(false);
  });

  it("accepts only bounded hexadecimal VoIP tokens and a known APNs environment", () => {
    expect(registerVoipTokenSchema.safeParse({
      token: "ab".repeat(32),
      environment: "production"
    }).success).toBe(true);
    expect(registerVoipTokenSchema.safeParse({
      token: "not-a-token",
      environment: "production"
    }).success).toBe(false);
    expect(registerVoipTokenSchema.safeParse({
      token: "ab".repeat(32),
      environment: "other"
    }).success).toBe(false);
  });

  it("allows documented edge messages and rejects arbitrary message types", () => {
    expect(edgeMessageSchema.safeParse({
      v: 1,
      type: "heartbeat",
      occurredAt: new Date().toISOString(),
      status: { modemOnline: true, lineBusy: false }
    }).success).toBe(true);
    expect(edgeMessageSchema.safeParse({ v: 1, type: "run.shell", command: "rm" }).success).toBe(false);
  });

  it("accepts a stable error code on rejected command acknowledgements", () => {
    expect(edgeMessageSchema.safeParse({
      v: 1,
      type: "command.ack",
      commandId: "command_abcdefghijkl",
      callId: "call_abcdefghijkl",
      status: "rejected",
      errorCode: "SIM_NOT_REGISTERED"
    }).success).toBe(true);
  });
});

describe("asset CSP", () => {
  it("allows only the configured secure LiveKit origin", () => {
    expect(liveKitConnectSources("wss://tenant.livekit.cloud")).toBe(
      " https://tenant.livekit.cloud wss://tenant.livekit.cloud",
    );
    expect(liveKitConnectSources("wss://localhost:7880/path")).toBe(
      " https://localhost:7880 wss://localhost:7880",
    );
  });

  it("fails closed for malformed or insecure LiveKit URLs", () => {
    expect(liveKitConnectSources("not a url")).toBe("");
    expect(liveKitConnectSources("https://tenant.livekit.cloud")).toBe("");
    expect(liveKitConnectSources("wss://user:secret@tenant.livekit.cloud")).toBe("");
  });
});
