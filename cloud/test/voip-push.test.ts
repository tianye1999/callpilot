import { generateKeyPairSync } from "node:crypto";

import { describe, expect, it } from "vitest";

import {
  buildVoipPayload,
  createApnsProviderToken,
  decryptPushToken,
  dispatchInboundOfferPushes,
  encryptPushToken,
  voipPushEnabled
} from "../src/voip-push";
import type { Env } from "../src/types";

describe("VoIP push security boundary", () => {
  it("enables delivery only for the exact opt-in value", () => {
    expect(voipPushEnabled("true")).toBe(true);
    expect(voipPushEnabled(undefined)).toBe(false);
    expect(voipPushEnabled("TRUE")).toBe(false);
    expect(voipPushEnabled("1")).toBe(false);
  });

  it("encrypts device tokens at rest and rejects the wrong key", async () => {
    const key = crypto.getRandomValues(new Uint8Array(32));
    const otherKey = crypto.getRandomValues(new Uint8Array(32));
    const token = "ab".repeat(32);

    const encrypted = await encryptPushToken(token, key);

    expect(encrypted.ciphertext).not.toContain(token);
    expect(encrypted.nonce).not.toBe("");
    await expect(decryptPushToken(encrypted, key)).resolves.toBe(token);
    await expect(decryptPushToken(encrypted, otherKey)).rejects.toThrow();
  });

  it("builds an opaque call-only payload", () => {
    const payload = buildVoipPayload({
      offerId: "offer_abcdefghijkl",
      callUUID: "12345678-1234-4abc-8def-1234567890ab",
      expiresAtUnixMs: 1_800_000_000_000
    });

    expect(payload).toEqual({
      aps: { "content-available": 1 },
      v: 1,
      type: "inbound.offer",
      offerId: "offer_abcdefghijkl",
      callUUID: "12345678-1234-4abc-8def-1234567890ab",
      expiresAtUnixMs: 1_800_000_000_000
    });
    expect(JSON.stringify(payload)).not.toMatch(/phone|caller|transcript|message|nonce/i);
  });

  it("creates a short-lived ES256 provider token without embedding the key", async () => {
    const { privateKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
    const privateKeyPEM = privateKey.export({ type: "pkcs8", format: "pem" }).toString();

    const token = await createApnsProviderToken({
      keyId: "KEYID12345",
      teamId: "TEAMID1234",
      privateKeyPEM,
      nowSeconds: 1_800_000_000
    });
    const [header, claims, signature] = token.split(".");

    expect(JSON.parse(decodeBase64URL(header))).toEqual({ alg: "ES256", kid: "KEYID12345" });
    expect(JSON.parse(decodeBase64URL(claims))).toEqual({ iss: "TEAMID1234", iat: 1_800_000_000 });
    expect(signature).toMatch(/^[A-Za-z0-9_-]{86}$/);
    expect(token).not.toContain(privateKeyPEM);
  });

  it("delivers an encrypted token through the correct APNs VoIP boundary", async () => {
    const key = crypto.getRandomValues(new Uint8Array(32));
    const token = "cd".repeat(32);
    const encrypted = await encryptPushToken(token, key);
    const { privateKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
    const privateKeyPEM = privateKey.export({ type: "pkcs8", format: "pem" }).toString();
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const deleted: string[] = [];
    const env = pushEnv({
      key,
      privateKeyPEM,
      rows: [{
        device_id: "device_abcdefghijkl",
        token_ciphertext: encrypted.ciphertext,
        token_nonce: encrypted.nonce,
        environment: "sandbox"
      }],
      deleted
    });

    await dispatchInboundOfferPushes(env, {
      edgeId: "edge_abcdefghijkl",
      offerId: "offer_abcdefghijkl",
      callUUID: "12345678-1234-4abc-8def-1234567890ab",
      expiresAtUnixMs: 1_800_000_000_000
    }, async (input, init) => {
      requests.push({ url: String(input), init });
      return new Response(null, { status: 200 });
    });

    expect(requests).toHaveLength(1);
    expect(requests[0]?.url).toBe(`https://api.sandbox.push.apple.com/3/device/${token}`);
    const headers = new Headers(requests[0]?.init?.headers);
    expect(headers.get("apns-push-type")).toBe("voip");
    expect(headers.get("apns-topic")).toBe("ai.bondings.callpilot.voip");
    expect(headers.get("apns-priority")).toBe("10");
    expect(headers.get("authorization")).toMatch(/^bearer [A-Za-z0-9_.-]+$/);
    expect(requests[0]?.init?.body).toBe(JSON.stringify(buildVoipPayload({
      offerId: "offer_abcdefghijkl",
      callUUID: "12345678-1234-4abc-8def-1234567890ab",
      expiresAtUnixMs: 1_800_000_000_000
    })));
    expect(deleted).toEqual([]);
  });

  it("forgets a token rejected by APNs without exposing it", async () => {
    const key = crypto.getRandomValues(new Uint8Array(32));
    const encrypted = await encryptPushToken("ef".repeat(32), key);
    const { privateKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
    const deleted: string[] = [];
    const env = pushEnv({
      key,
      privateKeyPEM: privateKey.export({ type: "pkcs8", format: "pem" }).toString(),
      rows: [{
        device_id: "device_abcdefghijkl",
        token_ciphertext: encrypted.ciphertext,
        token_nonce: encrypted.nonce,
        environment: "production"
      }],
      deleted
    });

    await dispatchInboundOfferPushes(env, {
      edgeId: "edge_abcdefghijkl",
      offerId: "offer_abcdefghijkl",
      callUUID: "12345678-1234-4abc-8def-1234567890ab",
      expiresAtUnixMs: 1_800_000_000_000
    }, async () => Response.json({ reason: "BadDeviceToken" }, { status: 400 }));

    expect(deleted).toEqual(["device_abcdefghijkl"]);
  });
});

function decodeBase64URL(value: string | undefined): string {
  if (!value) throw new Error("JWT segment missing");
  return Buffer.from(value, "base64url").toString("utf8");
}

function pushEnv(input: {
  key: Uint8Array;
  privateKeyPEM: string;
  rows: Array<Record<string, string>>;
  deleted: string[];
}): Env {
  const db = {
    prepare(sql: string) {
      return {
        bind(...values: unknown[]) {
          return {
            async all() {
              expect(sql).toContain("FROM device_push_tokens");
              expect(values).toEqual(["edge_abcdefghijkl"]);
              return { results: input.rows };
            },
            async run() {
              expect(sql).toContain("DELETE FROM device_push_tokens");
              input.deleted.push(String(values[0]));
              return { meta: { changes: 1 } };
            }
          };
        }
      };
    }
  };
  return {
    DB: db,
    VOIP_PUSH_ENABLED: "true",
    APNS_TEAM_ID: "TEAMID1234",
    APNS_KEY_ID: "KEYID12345",
    APNS_PRIVATE_KEY: input.privateKeyPEM,
    APNS_BUNDLE_ID: "ai.bondings.callpilot",
    PUSH_TOKEN_ENCRYPTION_KEY: Buffer.from(input.key).toString("base64url")
  } as unknown as Env;
}
