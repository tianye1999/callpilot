import { generateKeyPairSync } from "node:crypto";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  buildVoipPayload,
  createApnsProviderToken,
  decryptPushToken,
  dispatchInboundOfferPushes,
  encryptPushToken,
  voipPushEnabled
} from "../src/voip-push";
import type { Env } from "../src/types";
import { buildAlertPayload, dispatchAlertPushes } from "../src/alert-push";

afterEach(() => vi.restoreAllMocks());

describe("VoIP push security boundary", () => {
  it.each(["sandbox", "production"] as const)("sends alerts using the %s host and base topic", async (environment) => {
    const key = crypto.getRandomValues(new Uint8Array(32));
    const token = "ab".repeat(40);
    const encrypted = await encryptPushToken(token, key);
    const { privateKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
    const env = pushEnv({ key, privateKeyPEM: privateKey.export({ type: "pkcs8", format: "pem" }).toString(),
      alert: true, deleted: [], rows: [{ device_id: "device_abcdefghijkl",
        token_ciphertext: encrypted.ciphertext, token_nonce: encrypted.nonce, environment }] });
    // Assert outside the fetch stub: dispatch wraps sends in Promise.allSettled, so an
    // expectation thrown inside the stub would be swallowed and the test would pass anyway.
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    await dispatchAlertPushes(env, "edge_abcdefghijkl", "sms", async (url, init) => {
      requests.push({ url: String(url), init });
      return new Response(null, { status: 200 });
    });
    expect(requests).toHaveLength(1);
    expect(requests[0]?.url).toBe(`https://${environment === "sandbox" ? "api.sandbox" : "api"}.push.apple.com/3/device/${token}`);
    const headers = new Headers(requests[0]?.init?.headers);
    expect(headers.get("apns-push-type")).toBe("alert");
    expect(headers.get("apns-topic")).toBe("ai.bondings.callpilot");
    expect(headers.get("apns-priority")).toBe("10");
    expect(JSON.parse(String(requests[0]?.init?.body))).toEqual(buildAlertPayload("sms"));
  });

  it.each([[410, "Unregistered", true], [400, "BadDeviceToken", true],
    [400, "BadTopic", false], [403, "InvalidProviderToken", false], [503, "ServiceUnavailable", false]] as const)(
    "cleans up alerts only for invalid device tokens (%s %s)", async (status, reason, removed) => {
      const key = crypto.getRandomValues(new Uint8Array(32));
      const encrypted = await encryptPushToken("ef".repeat(32), key);
      const { privateKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
      const deleted: string[] = [];
      const statements: Statement[] = [];
      const env = pushEnv({ key, privateKeyPEM: privateKey.export({ type: "pkcs8", format: "pem" }).toString(),
        alert: true, deleted, statements, rows: [{ device_id: "device_abcdefghijkl",
          token_ciphertext: encrypted.ciphertext, token_nonce: encrypted.nonce, environment: "production" }] });
      await dispatchAlertPushes(env, "edge_abcdefghijkl", "missed_call",
        async () => Response.json({ reason }, { status }));
      expect(deleted).toEqual(removed ? ["device_abcdefghijkl"] : []);
      expect(statements[0]?.sql).toContain("FROM device_alert_push_tokens");
      expect(statements[0]?.sql).toContain("devices.edge_id = ?1 AND devices.revoked_at IS NULL");
      expect(statements[0]?.values).toEqual(["edge_abcdefghijkl"]);
      const deletes = statements.filter(statement => statement.sql.includes("DELETE FROM"));
      // A negative case must show no DELETE was attempted at all, not merely none recorded.
      expect(deletes).toHaveLength(removed ? 1 : 0);
      if (removed) {
        expect(deletes[0]?.sql).toContain("DELETE FROM device_alert_push_tokens");
        expect(deletes[0]?.sql).toContain("AND token_ciphertext = ?2");
        expect(deletes[0]?.values).toEqual(["device_abcdefghijkl", encrypted.ciphertext]);
      }
    }
  );

  it("does not query tokens or call APNs unless explicitly enabled", async () => {
    for (const flag of [undefined, "false", "TRUE", "1"]) {
      await dispatchAlertPushes({ ALERT_PUSH_ENABLED: flag } as Env, "edge_test", "sms", async () => {
        throw new Error("must not send");
      });
    }
  });
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
    const statements: Statement[] = [];
    const env = pushEnv({
      key,
      privateKeyPEM,
      rows: [{
        device_id: "device_abcdefghijkl",
        token_ciphertext: encrypted.ciphertext,
        token_nonce: encrypted.nonce,
        environment: "sandbox"
      }],
      deleted,
      statements
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
    expect(statements.map(statement => statement.sql).filter(sql => sql.includes("DELETE FROM"))).toEqual([]);
    expect(statements[0]?.sql).toContain("FROM device_push_tokens");
  });

  it("forgets a token rejected by APNs without exposing it", async () => {
    const key = crypto.getRandomValues(new Uint8Array(32));
    const encrypted = await encryptPushToken("ef".repeat(32), key);
    const { privateKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
    const deleted: string[] = [];
    const statements: Statement[] = [];
    const env = pushEnv({
      key,
      privateKeyPEM: privateKey.export({ type: "pkcs8", format: "pem" }).toString(),
      rows: [{
        device_id: "device_abcdefghijkl",
        token_ciphertext: encrypted.ciphertext,
        token_nonce: encrypted.nonce,
        environment: "production"
      }],
      deleted,
      statements
    });

    await dispatchInboundOfferPushes(env, {
      edgeId: "edge_abcdefghijkl",
      offerId: "offer_abcdefghijkl",
      callUUID: "12345678-1234-4abc-8def-1234567890ab",
      expiresAtUnixMs: 1_800_000_000_000
    }, async () => Response.json({ reason: "BadDeviceToken" }, { status: 400 }));

    expect(deleted).toEqual(["device_abcdefghijkl"]);
    const deletes = statements.filter(statement => statement.sql.includes("DELETE FROM device_push_tokens"));
    expect(deletes).toHaveLength(1);
    expect(deletes[0]?.values[0]).toBe("device_abcdefghijkl");
  });

  it.each(["accepted", "rejected", "invalid", "thrown"] as const)(
    "logs the %s VoIP result without exposing the device token", async (outcome) => {
      const token = "9a".repeat(32);
      const key = crypto.getRandomValues(new Uint8Array(32));
      const encrypted = await encryptPushToken(token, key);
      const { privateKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
      const deleted: string[] = [];
      const env = pushEnv({ key, privateKeyPEM: privateKey.export({ type: "pkcs8", format: "pem" }).toString(),
        deleted, rows: [{ device_id: "device_abcdefghijkl",
          token_ciphertext: encrypted.ciphertext, token_nonce: encrypted.nonce, environment: "production" }] });
      const log = vi.spyOn(console, "log").mockImplementation(() => {});
      const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
      const accepted = vi.fn();

      await dispatchInboundOfferPushes(env, {
        edgeId: "edge_abcdefghijkl",
        offerId: "offer_abcdefghijkl",
        callUUID: "12345678-1234-4abc-8def-1234567890ab",
        expiresAtUnixMs: 1_800_000_000_000
      }, async (url) => {
        if (outcome === "thrown") throw new TypeError(`fetch failed: ${String(url)}`);
        if (outcome === "accepted") return new Response(null, { status: 200 });
        return Response.json(
          { reason: outcome === "invalid" ? "BadDeviceToken" : "DeviceTokenNotForTopic" },
          { status: 400 }
        );
      }, accepted);

      expect(accepted).toHaveBeenCalledTimes(outcome === "accepted" ? 1 : 0);
      if (outcome === "accepted") expect(log).toHaveBeenCalledWith("VoIP push accepted", "production");
      if (outcome === "rejected") expect(warn).toHaveBeenCalledWith("VoIP push rejected", 400, "DeviceTokenNotForTopic");
      if (outcome === "invalid") expect(warn).toHaveBeenCalledWith("VoIP push rejected", 400, "BadDeviceToken");
      if (outcome === "thrown") expect(warn).toHaveBeenCalledWith("VoIP push failed", "TypeError");
      expect(deleted).toEqual(outcome === "invalid" ? ["device_abcdefghijkl"] : []);
      expect(JSON.stringify([...log.mock.calls, ...warn.mock.calls])).not.toContain(token);
    }
  );

  it("logs a failed VoIP target without waiting for a hung sibling", async () => {
    const key = crypto.getRandomValues(new Uint8Array(32));
    const failing = await encryptPushToken("aa".repeat(32), key);
    const hanging = await encryptPushToken("bb".repeat(32), key);
    const { privateKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
    const env = pushEnv({ key, privateKeyPEM: privateKey.export({ type: "pkcs8", format: "pem" }).toString(),
      deleted: [], rows: [
        { device_id: "device_failingabcde", token_ciphertext: failing.ciphertext,
          token_nonce: failing.nonce, environment: "production" },
        { device_id: "device_hangingabcde", token_ciphertext: hanging.ciphertext,
          token_nonce: hanging.nonce, environment: "production" }
      ] });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    let release: (response: Response) => void = () => {};
    const hung = new Promise<Response>((resolve) => { release = resolve; });

    const dispatched = dispatchInboundOfferPushes(env, {
      edgeId: "edge_abcdefghijkl",
      offerId: "offer_abcdefghijkl",
      callUUID: "12345678-1234-4abc-8def-1234567890ab",
      expiresAtUnixMs: 1_800_000_000_000
    }, async (url) => {
      if (String(url).includes("aa".repeat(32))) throw new TypeError("fetch failed");
      return hung;
    });

    await vi.waitFor(() => expect(warn).toHaveBeenCalledWith("VoIP push failed", "TypeError"));
    release(new Response(null, { status: 200 }));
    await dispatched;
  });

  it("notifies the first APNs acceptance immediately and only once across two targets", async () => {
    const key = crypto.getRandomValues(new Uint8Array(32));
    const encrypted = await encryptPushToken("aa".repeat(32), key);
    const { privateKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
    const env = pushEnv({ key, privateKeyPEM: privateKey.export({ type: "pkcs8", format: "pem" }).toString(),
      deleted: [], rows: ["device_firstabcdef", "device_secondabcde"].map(device_id => ({ device_id,
        token_ciphertext: encrypted.ciphertext, token_nonce: encrypted.nonce, environment: "production" })) });
    let notify!: () => void;
    const notified = new Promise<void>(resolve => { notify = resolve; });
    const accepted = vi.fn(notify);
    let release!: (response: Response) => void;
    const second = new Promise<Response>(resolve => { release = resolve; });
    let requests = 0;
    const pending = dispatchInboundOfferPushes(env, { edgeId: "edge_abcdefghijkl", offerId: "offer_abcdefghijkl",
      callUUID: "12345678-1234-4abc-8def-1234567890ab", expiresAtUnixMs: 1_800_000_000_000 },
    async () => ++requests === 1 ? new Response(null, { status: 200 }) : second, accepted);
    await notified;
    expect(accepted).toHaveBeenCalledOnce();
    release(new Response(null, { status: 200 }));
    await pending;
    expect(accepted).toHaveBeenCalledOnce();
  });

  it("logs when VoIP push is disabled without calling APNs", async () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const accepted = vi.fn();
    const env = { ...pushEnv({ key: crypto.getRandomValues(new Uint8Array(32)), privateKeyPEM: "unused",
      deleted: [], rows: [] }), VOIP_PUSH_ENABLED: "false" } as Env;
    await dispatchInboundOfferPushes(env, {
      edgeId: "edge_abcdefghijkl",
      offerId: "offer_abcdefghijkl",
      callUUID: "12345678-1234-4abc-8def-1234567890ab",
      expiresAtUnixMs: 1_800_000_000_000
    }, async () => { throw new Error("APNs must not be called"); }, accepted);
    expect(accepted).not.toHaveBeenCalled();
    expect(log).toHaveBeenCalledWith("VoIP push skipped: disabled");
  });

  it("logs when no device has a VoIP token", async () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const accepted = vi.fn();
    const env = pushEnv({ key: crypto.getRandomValues(new Uint8Array(32)), privateKeyPEM: "unused",
      deleted: [], rows: [] });
    await dispatchInboundOfferPushes(env, {
      edgeId: "edge_abcdefghijkl",
      offerId: "offer_abcdefghijkl",
      callUUID: "12345678-1234-4abc-8def-1234567890ab",
      expiresAtUnixMs: 1_800_000_000_000
    }, async () => { throw new Error("APNs must not be called"); }, accepted);
    expect(accepted).not.toHaveBeenCalled();
    expect(log).toHaveBeenCalledWith("VoIP push skipped: no registered devices");
  });
});

function decodeBase64URL(value: string | undefined): string {
  if (!value) throw new Error("JWT segment missing");
  return Buffer.from(value, "base64url").toString("utf8");
}

type Statement = { sql: string; values: unknown[] };

// The DB stub only records what was asked of it. Assertions live in the tests, after
// dispatch returns: production wraps sends in Promise.allSettled, so an expectation
// thrown inside the stub would be swallowed and a negative case would pass for free.
function pushEnv(input: {
  key: Uint8Array;
  privateKeyPEM: string;
  rows: Array<Record<string, string>>;
  deleted: string[];
  statements?: Statement[];
  alert?: boolean;
}): Env {
  const db = {
    prepare(sql: string) {
      return {
        bind(...values: unknown[]) {
          return {
            async all() {
              input.statements?.push({ sql, values });
              return { results: input.rows };
            },
            async run() {
              input.statements?.push({ sql, values });
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
    ALERT_PUSH_ENABLED: input.alert ? "true" : undefined,
    APNS_TEAM_ID: "TEAMID1234",
    APNS_KEY_ID: "KEYID12345",
    APNS_PRIVATE_KEY: input.privateKeyPEM,
    APNS_BUNDLE_ID: "ai.bondings.callpilot",
    PUSH_TOKEN_ENCRYPTION_KEY: Buffer.from(input.key).toString("base64url")
  } as unknown as Env;
}
