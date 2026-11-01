import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync, type SQLInputValue } from "node:sqlite";
import { URL } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("cloudflare:workers", () => ({ DurableObject: class {} }));

import worker from "../src/index";
import { sha256 } from "../src/security";
import type { DataRequest } from "../src/content-sync";
import type { Env } from "../src/types";

const databases: DatabaseSync[] = [];
const now = 1_800_000_000_000;
const deviceId = "device_abcdefghijkl";
const otherDevice = "device_otherabcdefg";

async function fixture() {
  vi.spyOn(Date, "now").mockReturnValue(now);
  const db = new DatabaseSync(":memory:");
  databases.push(db);
  for (const file of readdirSync(new URL("../migrations/", import.meta.url)).filter((name) => name.endsWith(".sql")).sort()) {
    db.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), "utf8"));
  }
  db.exec("INSERT INTO edges VALUES ('edge_abcdefghijkl', 'Test', 'public', 'hash', 0, NULL, NULL)");
  db.exec("INSERT INTO content_read_edges VALUES ('edge_abcdefghijkl', 0)");
  for (const id of [deviceId, otherDevice]) {
    db.prepare("INSERT INTO devices VALUES (?, 'edge_abcdefghijkl', 'Test', ?, 0, ?, NULL)")
      .run(id, await sha256("synthetic-secret"), now);
  }
  const prepare = vi.fn((sql: string) => ({
    bind: (...values: SQLInputValue[]) => {
      const statement = db.prepare(sql);
      const params = Object.fromEntries(values.map((value, index) => [String(index + 1), value]));
      return {
        first: async () => statement.get(params) ?? null,
        run: async () => statement.run(params)
      };
    }
  }));
  const relay = vi.fn(async (_url: string, init: RequestInit) => {
    const request = JSON.parse(init.body as string) as DataRequest;
    const file = request.resource === "messages.list" ? "messages-page.json" : "call-records-page.json";
    return Response.json({
      v: 1, type: "data.response", requestId: request.requestId, resource: request.resource,
      status: "ok",
      body: JSON.parse(readFileSync(new URL(`../../docs/fixtures/content-sync/v1/${file}`, import.meta.url), "utf8"))
    });
  });
  const env = {
    DB: { prepare }, CONTENT_READ_ENABLED: "true", PUBLIC_ORIGIN: "https://relay",
    EDGE_ROOMS: { idFromName: (id: string) => id, get: () => ({ fetch: relay }) }
  } as unknown as Env;
  const read = (path = "/v1/messages", id = deviceId) => worker.fetch(new Request(`https://relay${path}`, {
    headers: { Cookie: `__Host-callpilot-device=${id}.synthetic-secret` }
  }), env);
  const audits = () => db.prepare("SELECT * FROM audit_events ORDER BY occurred_at").all();
  return { db, relay, prepare, read, audits, env };
}

afterEach(() => {
  vi.restoreAllMocks();
  for (const db of databases.splice(0)) db.close();
});

describe("content read audit and relay boundary", () => {
  it("deduplicates the same device/resource for ten minutes without writing rate events", async () => {
    const f = await fixture();
    expect((await f.read()).status).toBe(200);
    vi.spyOn(Date, "now").mockReturnValue(now + 599_999);
    expect((await f.read()).status).toBe(200);
    expect(f.audits()).toHaveLength(1);
    expect(f.db.prepare("SELECT * FROM content_read_rate_events").all()).toHaveLength(0);
    vi.spyOn(Date, "now").mockReturnValue(now + 600_000);
    expect((await f.read()).status).toBe(200);
    expect(f.audits()).toHaveLength(2);
  });

  it("audits different resources and devices separately", async () => {
    const f = await fixture();
    for (const [path, id] of [["/v1/messages", deviceId], ["/v1/call-records", deviceId], ["/v1/messages", otherDevice]]) {
      expect((await f.read(path, id)).status).toBe(200);
    }
    expect(f.audits()).toHaveLength(3);
  });

  it("deduplicates concurrent reads atomically and uses the existing time index", async () => {
    const f = await fixture();
    const responses = await Promise.all([f.read(), f.read(), f.read()]);
    expect(responses.map((response) => response.status)).toEqual([200, 200, 200]);
    expect(f.audits()).toHaveLength(1);
    const plan = f.db.prepare(
      "EXPLAIN QUERY PLAN SELECT 1 FROM audit_events WHERE actor_id = ? AND action = ? AND resource_id = ? AND occurred_at > ?"
    ).all(deviceId, "content.read", "messages.list", now - 600_000);
    expect(plan.some((row) => String(row.detail).includes("USING INDEX audit_occurred"))).toBe(true);
  });

  it("keeps other audit actions per-request", async () => {
    const f = await fixture();
    for (let count = 0; count < 2; count += 1) {
      const response = await worker.fetch(new Request("https://relay/v1/device/push-token", {
        method: "DELETE",
        headers: { Origin: "https://relay", Cookie: `__Host-callpilot-device=${deviceId}.synthetic-secret` }
      }), f.env);
      expect(response.status).toBe(200);
    }
    expect(f.audits()).toHaveLength(2);
    expect(f.audits().every((row) => row.action === "voip_token.unregistered")).toBe(true);
  });

  it("rejects content when the audit statement fails, even within a dedup window", async () => {
    const f = await fixture();
    expect((await f.read()).status).toBe(200);
    const prepare = f.prepare.getMockImplementation()!;
    f.prepare.mockImplementation((sql) => {
      if (sql.startsWith("INSERT INTO audit_events")) throw new Error("D1 write limit");
      return prepare(sql);
    });
    vi.spyOn(console, "error").mockImplementation(() => {});
    const response = await f.read();
    expect(response.status).toBe(500);
    expect(await response.json()).toMatchObject({ error: { code: "INTERNAL_ERROR" } });
  });

  it("preserves the DO's Retry-After rather than substituting the Edge fallback", async () => {
    const f = await fixture();
    f.relay.mockResolvedValue(Response.json({ error: "RATE_LIMITED" }, {
      status: 429, headers: { "Retry-After": "59" }
    }));
    const response = await f.read();
    expect(response.status).toBe(429);
    expect(response.headers.get("Retry-After")).toBe("59");
    expect(await response.json()).toMatchObject({ error: { code: "RATE_LIMITED" } });
    expect(f.audits()).toHaveLength(0);
  });
});
