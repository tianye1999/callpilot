import { afterEach, describe, expect, it, vi } from "vitest";

import {
  contentReadEnabled,
  contentRelayTimeoutMs,
  ContentReadRateLimiter
} from "../src/content-access";
import type { Env } from "../src/types";

afterEach(() => vi.restoreAllMocks());

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

  it.each([{}, { CONTENT_READ_DEVICE_LIMIT: "0", CONTENT_READ_EDGE_LIMIT: "50001" }])(
    "retains default limits for missing or invalid settings (%s)", (vars) => {
      const limiter = new ContentReadRateLimiter();
      const env = vars as Env;
      for (let device = 0; device < 5; device += 1) {
        for (let count = 0; count < 60; count += 1) limiter.enforce(env, String(device));
        expect(() => limiter.enforce(env, String(device))).toThrow("Too many content requests");
      }
      expect(() => limiter.enforce(env, "another-device")).toThrow("Too many content requests");
    }
  );

  it("bounds keys by accepted requests and removes expired keys on the next check", () => {
    const clock = vi.spyOn(Date, "now").mockReturnValue(100_000);
    const limiter = new ContentReadRateLimiter();
    const env = { CONTENT_READ_DEVICE_LIMIT: "2", CONTENT_READ_EDGE_LIMIT: "3" } as Env;
    for (const id of ["a", "b", "c"]) limiter.enforce(env, id);
    for (let id = 0; id < 100; id += 1) {
      expect(() => limiter.enforce(env, String(id))).toThrow("Too many content requests");
    }
    expect(limiter["deviceEvents"].size).toBe(3);
    expect(limiter["edgeEvents"]).toHaveLength(3);
    clock.mockReturnValue(160_000);
    limiter.enforce(env, "fresh");
    expect([...limiter["deviceEvents"].keys()]).toEqual(["fresh"]);
    expect(limiter["edgeEvents"]).toEqual([160_000]);
  });

  it("uses a sliding window and waits for both exhausted buckets", () => {
    const clock = vi.spyOn(Date, "now").mockReturnValue(100_000);
    const limiter = new ContentReadRateLimiter();
    const env = { CONTENT_READ_DEVICE_LIMIT: "1", CONTENT_READ_EDGE_LIMIT: "2" } as Env;
    limiter.enforce(env, "a");
    clock.mockReturnValue(130_000);
    limiter.enforce(env, "b");
    clock.mockReturnValue(159_999);
    expect(() => limiter.enforce(env, "b")).toThrow(expect.objectContaining({
      status: 429, headers: { "Retry-After": "31" }
    }));
    clock.mockReturnValue(160_000);
    expect(() => limiter.enforce(env, "a")).not.toThrow();
    expect(() => limiter.enforce(env, "c")).toThrow(expect.objectContaining({
      status: 429, headers: { "Retry-After": "30" }
    }));
  });
});
