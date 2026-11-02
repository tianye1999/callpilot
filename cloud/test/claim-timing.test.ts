import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync, type SQLInputValue } from "node:sqlite";
import { URL } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("cloudflare:workers", () => ({ DurableObject: class {} }));
vi.mock("../src/livekit", () => ({ issueParticipantToken: vi.fn() }));

import worker from "../src/index";
import { issueParticipantToken } from "../src/livekit";
import { sha256 } from "../src/security";
import type { Env } from "../src/types";

const databases: DatabaseSync[] = [];
const now = 1_800_000_000_000;
const deviceId = "device_abcdefghijkl";
const edgeId = "edge_abcdefghijkl";
const offerId = "offer_abcdefghijkl";
const stages = ["auth", "offer_read", "cas", "edge_token", "edge_send", "phone_token", "audit", "total"];

async function fixture() {
  vi.spyOn(Date, "now").mockReturnValue(now);
  const log = vi.spyOn(console, "log").mockImplementation(() => {});
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.mocked(issueParticipantToken).mockReset().mockResolvedValue("synthetic-livekit-token");
  const db = new DatabaseSync(":memory:");
  databases.push(db);
  for (const file of readdirSync(new URL("../migrations/", import.meta.url)).filter((name) => name.endsWith(".sql")).sort()) {
    db.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), "utf8"));
  }
  db.prepare("INSERT INTO edges VALUES (?, 'Test', 'public', 'hash', 0, NULL, NULL)").run(edgeId);
  db.prepare("INSERT INTO devices VALUES (?, ?, 'Test', ?, 0, ?, NULL)")
    .run(deviceId, edgeId, await sha256("synthetic-secret"), now);
  db.prepare(
    "INSERT INTO inbound_offers(offer_id, edge_id, call_id, generation, nonce, status, created_at, expires_at, updated_at) VALUES (?, ?, 'call_abcdefghijkl', 1, 'synthetic-nonce', 'offered', ?, ?, ?)"
  ).run(offerId, edgeId, now, now + 15_000, now);
  const prepare = vi.fn((sql: string) => ({
    bind: (...values: SQLInputValue[]) => {
      const statement = db.prepare(sql);
      const params = Object.fromEntries(values.map((value, index) => [String(index + 1), value]));
      return {
        first: async () => statement.get(params) ?? null,
        run: async () => ({ meta: { changes: Number(statement.run(params).changes) } })
      };
    }
  }));
  const relay = vi.fn(async () => Response.json({ connected: true, modemOnline: true }, { status: 202 }));
  const env = {
    DB: { prepare }, PUBLIC_ORIGIN: "https://relay", LIVEKIT_URL: "wss://media.example.test",
    EDGE_ROOMS: { idFromName: (id: string) => id, get: () => ({ fetch: relay }) }
  } as unknown as Env;
  const claim = (id = offerId, authenticated = true) => worker.fetch(new Request("https://relay/v1/inbound-offers/claim", {
    method: "POST",
    headers: {
      Origin: "https://relay", "Content-Type": "application/json",
      ...(authenticated ? { Cookie: `__Host-callpilot-device=${deviceId}.synthetic-secret` } : {})
    },
    body: JSON.stringify({ offerId: id })
  }), env);
  const device = (authenticated = true) => worker.fetch(new Request("https://relay/v1/device", {
    headers: authenticated ? { Cookie: `__Host-callpilot-device=${deviceId}.synthetic-secret` } : {}
  }), env);
  return { db, prepare, relay, env, claim, device, log };
}

function assertTiming(response: Response, expected: string[], log: ReturnType<typeof vi.spyOn>) {
  const header = response.headers.get("Server-Timing");
  expect(header).not.toBeNull();
  const entries = header!.split(/,\s*/).map((entry) => {
    expect(entry).toMatch(/^[a-z_]+;dur=\d+(?:\.\d+)?$/);
    const [stage, duration] = entry.split(";dur=");
    return [stage, Number(duration)] as const;
  });
  expect(entries.map(([stage]) => stage)).toEqual(expected);
  expect(log).toHaveBeenCalledTimes(1);
  expect(log.mock.calls[0]).toHaveLength(1);
  const line = log.mock.calls[0]![0];
  expect(typeof line).toBe("string");
  expect(line).not.toContain("\n");
  const payload = JSON.parse(line as string) as Record<string, unknown>;
  expect(Object.keys(payload)).toEqual(expected);
  expect(payload).toEqual(Object.fromEntries(entries));
  for (const value of Object.values(payload)) {
    expect(typeof value).toBe("number");
    expect(Number.isFinite(value)).toBe(true);
    expect(value).toBeGreaterThanOrEqual(0);
  }
  for (const forbidden of [deviceId, edgeId, offerId, "call_", "web_", "edgepart_", "synthetic", "token", "roomName"]) {
    // Stage names contain "token"; only inspect values for identifier leakage.
    expect(JSON.stringify(Object.values(payload))).not.toContain(forbidden);
  }
}

afterEach(() => {
  vi.restoreAllMocks();
  for (const db of databases.splice(0)) db.close();
});

describe("inbound claim timing", () => {
  it("reports all successful stages as durations in the header and one identifier-free log", async () => {
    const f = await fixture();
    const response = await f.claim();
    expect(response.status).toBe(202);
    assertTiming(response, stages, f.log);
  });

  it("reports authentication failure without inventing unentered stages", async () => {
    const f = await fixture();
    const response = await f.claim(offerId, false);
    expect(response.status).toBe(401);
    assertTiming(response, ["auth", "total"], f.log);
  });

  it("reports a missing offer through the read stage", async () => {
    const f = await fixture();
    const response = await f.claim("offer_missingabcde");
    expect(response.status).toBe(404);
    assertTiming(response, ["auth", "offer_read", "total"], f.log);
  });

  it.each(["claimed", "revoked"])("reports an unavailable %s offer through CAS and rejection audit", async (status) => {
    const f = await fixture();
    f.db.prepare("UPDATE inbound_offers SET status = ? WHERE offer_id = ?").run(status, offerId);
    const response = await f.claim();
    expect(response.status).toBe(409);
    assertTiming(response, ["auth", "offer_read", "cas", "audit", "total"], f.log);
  });

  it("reports an expired offer through CAS and rejection audit", async () => {
    const f = await fixture();
    f.db.prepare("UPDATE inbound_offers SET expires_at = ? WHERE offer_id = ?").run(now - 1, offerId);
    const response = await f.claim();
    expect(response.status).toBe(409);
    assertTiming(response, ["auth", "offer_read", "cas", "audit", "total"], f.log);
  });

  it("includes the failed Edge token stage and preserves failed-offer cleanup", async () => {
    const f = await fixture();
    vi.mocked(issueParticipantToken).mockRejectedValueOnce(new Error("synthetic private error"));
    const response = await f.claim();
    expect(response.status).toBe(500);
    assertTiming(response, ["auth", "offer_read", "cas", "edge_token", "total"], f.log);
    expect(f.db.prepare("SELECT status FROM inbound_offers WHERE offer_id = ?").get(offerId)?.status).toBe("failed");
  });

  it("includes failed command delivery without later phone-token or audit stages", async () => {
    const f = await fixture();
    f.relay.mockResolvedValue(Response.json({ error: "EDGE_OFFLINE" }, { status: 503 }));
    const response = await f.claim();
    expect(response.status).toBe(409);
    assertTiming(response, ["auth", "offer_read", "cas", "edge_token", "edge_send", "total"], f.log);
  });

  it("includes a failed phone token stage", async () => {
    const f = await fixture();
    vi.mocked(issueParticipantToken).mockResolvedValueOnce("synthetic-edge-token")
      .mockRejectedValueOnce(new Error("synthetic phone token error"));
    const response = await f.claim();
    expect(response.status).toBe(500);
    assertTiming(response, stages.filter((stage) => stage !== "audit"), f.log);
  });

  it("includes a failed audit stage without logging the error message", async () => {
    const f = await fixture();
    const prepare = f.prepare.getMockImplementation()!;
    f.prepare.mockImplementation((sql) => {
      if (sql.startsWith("INSERT INTO audit_events")) throw new Error("synthetic audit error");
      return prepare(sql);
    });
    const response = await f.claim();
    expect(response.status).toBe(500);
    assertTiming(response, stages, f.log);
  });
});

describe("device claim prewarm feature", () => {
  it.each([undefined, "", "false", "TRUE", "1", " true ", "true"])("enables only the exact true spelling (%s)", async (value) => {
    const f = await fixture();
    Object.assign(f.env, { CLAIM_PREWARM_ENABLED: value });
    const response = await f.device();
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({
      ok: true,
      paired: true,
      capabilities: [],
      features: { claimPrewarm: value === "true" },
      device: { deviceId, edgeId, displayName: "Test" },
      edge: { connected: true, modemOnline: true }
    });
    expect(response.headers.has("Server-Timing")).toBe(false);
    const unpaired = await f.device(false);
    expect(unpaired.status).toBe(200);
    expect(await unpaired.json()).toEqual({
      ok: true, paired: false, features: { claimPrewarm: value === "true" }
    });
    expect(f.log).not.toHaveBeenCalled();
  });
});
