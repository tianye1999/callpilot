import { describe, expect, it } from "vitest";

import {
  contentReadEnabled,
  contentRelayTimeoutMs,
  enforceContentRateLimit
} from "../src/content-access";
import { HttpError } from "../src/http";
import type { DeviceRecord, Env } from "../src/types";

const device: DeviceRecord = {
  device_id: "device_abcdefghijkl",
  edge_id: "edge_abcdefghijkl",
  display_name: "Test",
  secret_hash: "hash",
  created_at: 0,
  last_used_at: 0,
  revoked_at: null
};

describe("content access gates and bounds", () => {
  it("enables the Cloud gate only for the exact true spelling", () => {
    expect(contentReadEnabled("true")).toBe(true);
    for (const value of [undefined, "", "1", "TRUE", "yes", " true "]) {
      expect(contentReadEnabled(value)).toBe(false);
    }
  });

  it("uses a bounded relay timeout and fails closed to its default", () => {
    expect(contentRelayTimeoutMs({ CONTENT_RELAY_TIMEOUT_MS: "250" } as Env)).toBe(250);
    expect(contentRelayTimeoutMs({ CONTENT_RELAY_TIMEOUT_MS: "10001" } as Env)).toBe(5_000);
    expect(contentRelayTimeoutMs({ CONTENT_RELAY_TIMEOUT_MS: "invalid" } as Env)).toBe(5_000);
  });

  it("charges both the authenticated device and its Edge without content keys", async () => {
    const seen: string[] = [];
    const env = rateEnv([true, true], seen);
    await enforceContentRateLimit(env, device);
    expect(seen).toHaveLength(2);
    expect(seen[0]).toBe("content:device:device_abcdefghijkl");
    expect(seen[1]).toBe("content:edge:edge_abcdefghijkl");
    expect(seen.join(" ")).not.toMatch(/cursor|message|transcript|summary/);
  });

  it("returns the stable rate error and Retry-After for either exhausted bucket", async () => {
    const deviceLimited = rateEnv([false], [], {
      CONTENT_READ_DEVICE_LIMIT: "1",
      CONTENT_READ_EDGE_LIMIT: "10"
    });
    await expect(enforceContentRateLimit(deviceLimited, device)).rejects.toMatchObject({
      code: "RATE_LIMITED",
      status: 429
    } satisfies Partial<HttpError>);

    const edgeLimited = rateEnv([true, false], [], {
      CONTENT_READ_DEVICE_LIMIT: "10",
      CONTENT_READ_EDGE_LIMIT: "1"
    });
    try {
      await enforceContentRateLimit(edgeLimited, device);
      expect.fail("edge limit should reject");
    } catch (error) {
      expect(error).toBeInstanceOf(HttpError);
      expect(new Headers((error as HttpError).headers).get("Retry-After")).toMatch(/^\d+$/);
    }
  });
});

function rateEnv(
  allowed: boolean[],
  seen: string[],
  vars: Partial<Env> = {}
): Env {
  let insertIndex = 0;
  const db = {
    prepare: (sql: string) => ({
      bind: (...values: unknown[]) => ({
        run: async () => ({ success: true }),
        first: async () => {
          if (sql.startsWith("SELECT MIN")) return { occurred_at: Date.now() };
          const key = String(values[1]);
          seen.push(key);
          const isAllowed = allowed[insertIndex];
          insertIndex += 1;
          return isAllowed ? { event_id: "rate_fixture_0001" } : null;
        }
      })
    })
  };
  return { DB: db, ...vars } as unknown as Env;
}
