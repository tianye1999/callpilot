import { afterEach, describe, expect, it, vi } from "vitest";

import { authenticateDevice } from "../src/auth";
import { sha256 } from "../src/security";
import type { Env } from "../src/types";

const now = 1_800_000_000_000;
const deviceId = "device_abcdefghijkl";
const secret = "test-secret-with-at-least-32-characters";

async function fixture(lastUsedAt: number | null) {
  vi.spyOn(Date, "now").mockReturnValue(now);
  const device = { device_id: deviceId, secret_hash: await sha256(secret), last_used_at: lastUsedAt };
  const run = vi.fn().mockResolvedValue({});
  const bind = vi.fn().mockReturnValue({ first: async () => device, run });
  const prepare = vi.fn().mockReturnValue({ bind });
  const env = { DB: { prepare } } as unknown as Env;
  const request = new Request("https://relay/v1/device", {
    headers: { Cookie: `__Host-callpilot-device=${deviceId}.${secret}` }
  });
  return { device, run, bind, prepare, env, request };
}

afterEach(() => vi.restoreAllMocks());

describe("device last-used bookkeeping", () => {
  it("does not update a fresh device", async () => {
    const f = await fixture(now - 599_999);
    expect(await authenticateDevice(f.request, f.env)).toBe(f.device);
    expect(f.run).not.toHaveBeenCalled();
  });

  it.each([null, now - 600_000, now - 600_001])("conditionally updates a stale timestamp (%s)", async (lastUsedAt) => {
    const f = await fixture(lastUsedAt);
    await authenticateDevice(f.request, f.env);
    expect(f.prepare).toHaveBeenLastCalledWith(
      "UPDATE devices SET last_used_at = ?1 WHERE device_id = ?2 AND (last_used_at IS NULL OR last_used_at <= ?3)"
    );
    expect(f.bind).toHaveBeenLastCalledWith(now, deviceId, now - 600_000);
    expect(f.run).toHaveBeenCalledOnce();
  });

  it("still authenticates when bookkeeping fails without logging error contents", async () => {
    const f = await fixture(0);
    f.run.mockRejectedValue(new Error(`D1 failure ${secret}`));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(await authenticateDevice(f.request, f.env)).toBe(f.device);
    expect(warn).toHaveBeenCalledOnce();
    expect(JSON.stringify(warn.mock.calls)).not.toContain(secret);
  });
});
