import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync, type SQLInputValue } from "node:sqlite";
import { URL } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("cloudflare:workers", () => ({ DurableObject: class {
  constructor(protected ctx: DurableObjectState, protected env: Env) {}
} }));
import worker from "../src/index";
import { sha256 } from "../src/security";
import type { Env } from "../src/types";
import { invalidateCandidates, queueParticipantCleanup, sweepPreconnect } from "../src/inbound-preconnect";
import { participantRequest } from "../src/livekit";
import * as livekit from "../src/livekit";
import { EdgeRoom } from "../src/edge-room";

const databases: DatabaseSync[] = [];
const edge = "edge_abcdefghijkl";
const offer = "offer_abcdefghijkl";
const device = "device_abcdefghijkl";

async function fixture(beforeCleanupMigration = false) {
  const db = new DatabaseSync(":memory:");
  databases.push(db);
  for (const name of readdirSync(new URL("../migrations/", import.meta.url)).filter(n => n.endsWith(".sql")).sort()) {
    if (beforeCleanupMigration && name === "0011_preconnect_cleanup_workset.sql") continue;
    db.exec(readFileSync(new URL(`../migrations/${name}`, import.meta.url), "utf8"));
  }
  db.prepare("INSERT INTO edges VALUES (?, 'Test', 'public', 'hash', 0, NULL, NULL)").run(edge);
  for (const id of [device, "device_otherabcdefg"]) {
    db.prepare("INSERT INTO devices VALUES (?, ?, 'Test', ?, 0, ?, NULL)")
      .run(id, edge, await sha256("secret"), Date.now());
  }
  db.prepare(`INSERT INTO inbound_offers(offer_id, edge_id, call_id, generation, nonce, status,
    created_at, expires_at, updated_at, room_name, edge_identity, preconnect_room, preconnect_enabled)
    VALUES (?, ?, 'call_abcdefghijkl', 1, 'abcdefghijklmnop', 'offered', ?, ?, ?,
    'callpilot_prepared', 'edgepart_prepared', 'callpilot_prepared', 1)`)
    .run(offer, edge, Date.now(), Date.now() + 20_000, Date.now());
  const background: Promise<unknown>[] = [];
  const ctx = { waitUntil: (p: Promise<unknown>) => { background.push(p); } } as ExecutionContext;
  const relay = vi.fn(async (_url: string, _init?: RequestInit) => Response.json({ connected: true, inboundPreconnectV1: true }));
  const queries: { sql: string; plan: string[]; changes: number }[] = [];
  const env = {
    DB: { batch: vi.fn(async (statements: D1PreparedStatement[]) => {
      db.exec("BEGIN");
      try {
        const results = [];
        for (const statement of statements) results.push((statement as unknown as { execute: () => unknown }).execute());
        db.exec("COMMIT");
        return results;
      } catch (error) {
        db.exec("ROLLBACK");
        throw error;
      }
    }), prepare: (sql: string) => ({ bind: (...values: SQLInputValue[]) => {
      const params = Object.fromEntries(values.map((v, i) => [String(i + 1), v]));
      const query = { sql, changes: 0, plan: db.prepare(`EXPLAIN QUERY PLAN ${sql}`).all(params).map(row => String(row.detail)) };
      queries.push(query);
      return {
        execute: () => ({ results: db.prepare(sql).all(params) }),
        first: async () => db.prepare(sql).get(params) ?? null,
        all: async () => ({ results: db.prepare(sql).all(params) }),
        run: async () => {
          query.changes = Number(db.prepare(sql).run(params).changes);
          return { meta: { changes: query.changes } };
        }
      };
    } }) },
    PUBLIC_ORIGIN: "https://relay", INBOUND_PRECONNECT_ENABLED: "true", INBOUND_CLAIM_FAST_PATH_ENABLED: "true",
    LIVEKIT_URL: "wss://media.test", LIVEKIT_API_KEY: "testkey", LIVEKIT_API_SECRET: "testsecret",
    EDGE_ROOMS: { idFromName: (id: string) => id, get: () => ({ fetch: relay }) }
  } as unknown as Env;
  const request = (path: string, body: object, id = device) => worker.fetch(new Request(`https://relay/v1/inbound-offers/${path}`, {
    method: "POST", headers: { Origin: "https://relay", "Content-Type": "application/json", Cookie: `__Host-callpilot-device=${id}.secret` },
    body: JSON.stringify(body)
  }), env, ctx);
  return { db, env, request, relay, background, queries };
}

async function roomFixture() {
  const f = await fixture();
  vi.stubGlobal("WebSocketRequestResponsePair", class {});
  const storage = new Map<string, unknown>([["presence", { connected: true, inboundPreconnectV1: true }]]);
  const socket = {
    readyState: WebSocket.OPEN as number,
    deserializeAttachment: () => ({ edgeId: edge, connectedAt: Date.now() }),
    send: vi.fn(), close: vi.fn()
  };
  const state = {
    getWebSockets: () => [socket], setWebSocketAutoResponse: vi.fn(),
    waitUntil: (p: Promise<unknown>) => { f.background.push(p); },
    storage: { get: async (key: string) => storage.get(key), put: async (key: string, value: unknown) => { storage.set(key, value); },
      setAlarm: vi.fn(), deleteAlarm: vi.fn() }
  };
  const room = new EdgeRoom(state as unknown as DurableObjectState, f.env);
  const message = (data: object) => room.webSocketMessage(socket as unknown as WebSocket, JSON.stringify(data));
  return { ...f, room, socket, state, message };
}

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); databases.splice(0).forEach(db => db.close()); });

describe("inbound preconnect", () => {
  it("prepares in one D1 batch alongside presence and emits identifier-free stage timings", async () => {
    const f = await fixture();
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    f.relay.mockImplementation(async () => { await gate; return Response.json({ connected: true, inboundPreconnectV1: true }); });
    const pending = f.request("prepare", { offerId: offer });
    await vi.waitFor(() => expect(f.env.DB.batch).toHaveBeenCalledOnce());
    release();
    const response = await pending;
    expect(response.status).toBe(200);
    expect(response.headers.get("Server-Timing")).toMatch(/prepare_batch;dur=/);
    const line = JSON.parse(log.mock.calls[0]![0] as string);
    expect(Object.keys(line).sort()).toEqual(["auth", "phone_token", "prepare_batch", "presence", "total"]);
    expect(Object.values(line).every(value => typeof value === "number" && value >= 0)).toBe(true);
    expect(JSON.stringify(line)).not.toMatch(/device_|offer_|web_|testsecret/);
    await Promise.all(f.background);
  });

  it("uses the fast adjudication with the fast flag off and reads candidates alongside CAS", async () => {
    const f = await fixture();
    const a = await (await f.request("prepare", { offerId: offer })).json() as Record<string, string>;
    f.env.INBOUND_CLAIM_FAST_PATH_ENABLED = "false";
    const original = f.env.DB.prepare.bind(f.env.DB);
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    let casStarted = false;
    let candidateRead = false;
    vi.spyOn(f.env.DB, "prepare").mockImplementation(sql => {
      const statement = original(sql);
      const bind = statement.bind.bind(statement);
      statement.bind = (...values) => {
        const bound = bind(...values);
        if (sql.startsWith("SELECT * FROM inbound_preparations")) candidateRead = true;
        if (sql.startsWith("UPDATE inbound_offers SET status = 'claimed'")) {
          const first = bound.first.bind(bound);
          bound.first = (async () => { casStarted = true; await gate; return first(); }) as typeof bound.first;
        }
        return bound;
      };
      return statement;
    });
    const admin = vi.fn(async () => Response.json({}));
    vi.stubGlobal("fetch", admin);
    f.queries.length = 0;
    const pending = f.request("claim", { offerId: offer, prepareId: a.prepareId });
    await vi.waitFor(() => expect(casStarted && candidateRead).toBe(true));
    expect(admin).not.toHaveBeenCalled();
    expect(f.relay.mock.calls.filter(args => String(args[0]).endsWith("/command"))).toHaveLength(0);
    release();
    const response = await pending;
    expect(response.status).toBe(202);
    expect(response.headers.get("Server-Timing")).not.toContain("offer_read;");
    expect(f.queries.filter(q => q.sql.startsWith("SELECT offer_id FROM inbound_offers WHERE offer_id"))).toHaveLength(0);
    await Promise.all(f.background);
  });

  it("returns before audit and cleanup execution, but only after durable cleanup and Edge delivery", async () => {
    const f = await fixture();
    f.env.INBOUND_CLAIM_FAST_PATH_ENABLED = "false";
    const a = await (await f.request("prepare", { offerId: offer })).json() as Record<string, string>;
    const original = f.env.DB.prepare.bind(f.env.DB);
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    vi.spyOn(f.env.DB, "prepare").mockImplementation(sql => {
      const statement = original(sql);
      if (!sql.startsWith("INSERT INTO audit_events")) return statement;
      const bind = statement.bind.bind(statement);
      statement.bind = (...values) => {
        const bound = bind(...values);
        const run = bound.run.bind(bound);
        bound.run = async () => { await gate; return run(); };
        return bound;
      };
      return statement;
    });
    f.relay.mockImplementation(async url => {
      if (url.endsWith("/command")) {
        expect(f.relay.mock.calls.filter(args => String(args[0]).endsWith("/preconnect-cleanup"))).toHaveLength(0);
      }
      if (url.endsWith("/preconnect-cleanup")) await gate;
      return Response.json({ connected: true, inboundPreconnectV1: true });
    });
    const pending = f.request("claim", { offerId: offer }, "device_otherabcdefg");
    let response: Response | undefined;
    void pending.then(value => { response = value; });
    await vi.waitFor(() => expect(response?.status).toBe(202));
    expect(f.db.prepare("SELECT * FROM audit_events").all()).toHaveLength(0);
    expect(f.db.prepare("SELECT state FROM inbound_preparations WHERE prepare_id = ?").get(a.prepareId!)?.state).toBe("invalid");
    expect(f.db.prepare("SELECT * FROM inbound_participant_cleanup").all()).toHaveLength(2);
    expect(f.relay.mock.calls.filter(args => String(args[0]).endsWith("/command"))).toHaveLength(1);
    release();
    await Promise.all(f.background);
    expect(f.db.prepare("SELECT * FROM audit_events").all()).toHaveLength(1);
  });

  it("does not send or return while the durable finalization batch is pending", async () => {
    const f = await fixture();
    await f.request("prepare", { offerId: offer });
    const batch = f.env.DB.batch.bind(f.env.DB);
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    vi.mocked(f.env.DB.batch).mockClear().mockImplementationOnce(async statements => {
      await gate;
      return batch(statements);
    });
    const pending = f.request("claim", { offerId: offer }, "device_otherabcdefg");
    let response: Response | undefined;
    void pending.then(value => { response = value; });
    await vi.waitFor(() => expect(f.env.DB.batch).toHaveBeenCalledOnce());
    expect(response).toBeUndefined();
    expect(f.relay.mock.calls.filter(args => String(args[0]).endsWith("/command"))).toHaveLength(0);
    release();
    expect((await pending).status).toBe(202);
    expect(vi.mocked(f.env.DB.batch).mock.calls[0]![0]).toHaveLength(4);
    expect(f.db.prepare("SELECT * FROM inbound_participant_cleanup").all()).toHaveLength(2);
    await Promise.all(f.background);
  });

  it("rolls back finalization if a cleanup insert fails, then fails closed without delivery", async () => {
    const f = await fixture();
    await f.request("prepare", { offerId: offer });
    f.db.exec(`CREATE TEMP TRIGGER reject_cold_phone BEFORE INSERT ON inbound_participant_cleanup
      WHEN NEW.participant_identity LIKE 'web_%'
      BEGIN SELECT RAISE(ABORT, 'synthetic queue failure'); END`);
    const batch = f.env.DB.batch.bind(f.env.DB);
    let rolledBack = false;
    vi.mocked(f.env.DB.batch).mockImplementationOnce(async statements => {
      try { return await batch(statements); }
      catch (error) {
        expect(f.db.prepare("SELECT state FROM inbound_preparations").get()?.state).toBe("prepared");
        expect(f.db.prepare("SELECT * FROM inbound_participant_cleanup").all()).toHaveLength(0);
        rolledBack = true;
        throw error;
      }
    });
    expect((await f.request("claim", { offerId: offer }, "device_otherabcdefg")).status).toBe(500);
    expect(rolledBack).toBe(true);
    expect(f.db.prepare("SELECT status FROM inbound_offers").get()?.status).toBe("failed");
    expect(f.relay.mock.calls.filter(args => String(args[0]).endsWith("/command"))).toHaveLength(0);
    await Promise.all(f.background);
  });

  it.each(["revoked", "failed"])("still rejects a %s offer during Edge delivery instead of deferring claim_check", async status => {
    const f = await fixture();
    const a = await (await f.request("prepare", { offerId: offer })).json() as Record<string, string>;
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
    f.relay.mockImplementation(async url => {
      if (url.endsWith("/command")) f.db.prepare("UPDATE inbound_offers SET status = ?").run(status);
      return Response.json({ connected: true, inboundPreconnectV1: true });
    });
    expect((await f.request("claim", { offerId: offer, prepareId: a.prepareId })).status).toBe(409);
    expect(f.db.prepare("SELECT * FROM audit_events").all()).toHaveLength(0);
    expect(f.db.prepare("SELECT state FROM inbound_preparations").get()?.state).toBe("invalid");
    await Promise.all(f.background);
  });

  it("the alarm removes losers even when the background cleanup wake fails", async () => {
    const f = await roomFixture();
    await f.state.storage.put("preconnectEdge", edge);
    const a = await (await f.request("prepare", { offerId: offer })).json() as Record<string, string>;
    const b = await (await f.request("prepare", { offerId: offer }, "device_otherabcdefg")).json() as Record<string, string>;
    const admin = vi.fn(async () => Response.json({}));
    vi.stubGlobal("fetch", admin);
    f.relay.mockImplementation(async url => {
      if (url.endsWith("/preconnect-cleanup")) throw new Error("synthetic interrupted wake");
      return Response.json({ connected: true, inboundPreconnectV1: true });
    });
    expect((await f.request("claim", { offerId: offer, prepareId: a.prepareId })).status).toBe(202);
    await Promise.all(f.background);
    await f.room.alarm();
    const removals = vi.mocked(fetch).mock.calls.filter(args => String(args[0]).endsWith("/RemoveParticipant"))
      .map(args => JSON.parse(args[1]!.body as string).identity);
    expect(removals).toContain(b.participantIdentity);
    expect(removals).not.toContain(a.participantIdentity);
  });

  it.each(["revoked", "expired"])("a %s offer cannot be claimed or promoted", async status => {
    const f = await fixture();
    const a = await (await f.request("prepare", { offerId: offer })).json() as Record<string, string>;
    if (status === "expired") f.db.prepare("UPDATE inbound_offers SET expires_at = ?").run(Date.now() - 1);
    else f.db.prepare("UPDATE inbound_offers SET status = 'revoked'").run();
    const admin = vi.fn(async () => Response.json({}));
    vi.stubGlobal("fetch", admin);
    expect((await f.request("claim", { offerId: offer, prepareId: a.prepareId })).status).toBe(409);
    expect(admin).not.toHaveBeenCalled();
    expect(f.relay.mock.calls.filter(args => String(args[0]).endsWith("/command"))).toHaveLength(0);
    await Promise.all(f.background);
  });

  it("signs reuse tokens during permission but delivers neither credentials nor commands before permission completes", async () => {
    const f = await fixture();
    const a = await (await f.request("prepare", { offerId: offer })).json() as Record<string, string>;
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    vi.stubGlobal("fetch", vi.fn(async () => { await gate; return Response.json({}); }));
    const signing = vi.spyOn(livekit, "issueParticipantToken");
    const pending = f.request("claim", { offerId: offer, prepareId: a.prepareId });
    let response: Response | undefined;
    void pending.then(value => { response = value; });
    await vi.waitFor(() => expect(signing).toHaveBeenCalledTimes(2));
    expect(response).toBeUndefined();
    expect(f.relay.mock.calls.filter(args => String(args[0]).endsWith("/command"))).toHaveLength(0);
    release();
    expect(await (await pending).json()).toMatchObject({ mode: "reuse" });
    await Promise.all(f.background);
  });

  it("unchanged sweeps write zero rows and never scan completed history", async () => {
    const f = await fixture();
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
    const old = Date.now() - 86_400_000;
    f.db.exec("BEGIN");
    for (let i = 0; i < 1500; i++) {
      f.db.prepare(`INSERT INTO inbound_offers (offer_id, edge_id, call_id, generation, nonce, status,
        created_at, expires_at, updated_at, preconnect_enabled)
        VALUES (?, ?, 'old_call', 1, 'old_nonce', 'revoked', ?, ?, ?, 1)`)
        .run(`old_offer_${i}`, edge, old, old, old);
      f.db.prepare(`INSERT INTO inbound_preparations
        (prepare_id, offer_id, device_id, room_name, participant_identity, expires_at, state)
        VALUES (?, ?, ?, 'old_room', ?, ?, 'invalid')`)
        .run(`old_prepare_${i}`, `old_offer_${i}`, device, `old_identity_${i}`, old);
      f.db.prepare(`INSERT INTO inbound_participant_cleanup
        (edge_id, offer_id, room_name, participant_identity, retry_until, completed)
        VALUES (?, ?, 'old_room', ?, ?, 1)`)
        .run(edge, `old_offer_${i}`, `old_identity_${i}`, old);
    }
    f.db.exec("COMMIT");
    // Drain any one-time queue bookkeeping before checking steady-state cost.
    await sweepPreconnect(f.env, edge);
    await f.request("prepare", { offerId: offer });
    f.db.prepare("UPDATE inbound_offers SET status = 'revoked' WHERE offer_id = ?").run(offer);
    await sweepPreconnect(f.env, edge);
    const before = f.db.prepare("SELECT total_changes() AS count").get()!.count;
    f.queries.length = 0;
    vi.mocked(fetch).mockClear();
    for (let i = 0; i < 120; i++) await sweepPreconnect(f.env, edge);
    expect(f.db.prepare("SELECT total_changes() AS count").get()!.count).toBe(before);
    expect(f.queries.reduce((sum, query) => sum + query.changes, 0)).toBe(0);
    expect(fetch).toHaveBeenCalledTimes(120);
    const plans = [...new Set(f.queries.flatMap(query => query.plan))];
    // Only indexed pending sets or bounded offer ranges may drive the alarm.
    for (const plan of plans) {
      if (/\bSCAN\b/.test(plan)) expect(plan).toMatch(/USING (COVERING )?INDEX inbound_(preparations_live|preparations_unqueued)/);
    }
    expect(plans.join("\n")).toContain("inbound_cleanup_pending");
    for (const plan of plans.filter(plan => plan.startsWith("SEARCH inbound_offers"))) {
      expect(plan).toMatch(/edge_id=\? AND (status=\?|updated_at>\?)/);
    }
  });

  it("migration marks existing queued candidates without losing unqueued work", async () => {
    const f = await fixture(true);
    const a = await (await f.request("prepare", { offerId: offer })).json() as Record<string, string>;
    await f.request("prepare", { offerId: offer }, "device_otherabcdefg");
    f.db.prepare("UPDATE inbound_preparations SET state = 'invalid'").run();
    f.db.prepare(`INSERT INTO inbound_participant_cleanup
      (edge_id, offer_id, room_name, participant_identity, retry_until, completed)
      VALUES (?, ?, ?, ?, 0, 1)`).run(edge, offer, a.roomName!, a.participantIdentity!);
    f.db.exec(readFileSync(new URL("../migrations/0011_preconnect_cleanup_workset.sql", import.meta.url), "utf8"));
    expect(f.db.prepare("SELECT cleanup_queued FROM inbound_preparations WHERE prepare_id = ?")
      .get(a.prepareId!)?.cleanup_queued).toBe(1);
    expect(f.db.prepare("SELECT COUNT(*) AS count FROM inbound_preparations WHERE cleanup_queued = 0").get()?.count).toBe(1);
    f.db.prepare("UPDATE inbound_offers SET status = 'revoked'").run();
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
    await sweepPreconnect(f.env, edge);
    expect(f.db.prepare("SELECT COUNT(*) AS count FROM inbound_preparations WHERE cleanup_queued = 0").get()?.count).toBe(0);
    expect(fetch).toHaveBeenCalledOnce();
    f.queries.length = 0;
    await sweepPreconnect(f.env, edge);
    expect(f.queries.reduce((sum, query) => sum + query.changes, 0)).toBe(0);
  });

  it("completes expired work despite failure and concurrent completion writes once", async () => {
    const f = await fixture();
    const a = await (await f.request("prepare", { offerId: offer })).json() as Record<string, string>;
    const old = Date.now() - 86_400_000;
    f.db.prepare("UPDATE inbound_offers SET status = 'revoked', updated_at = ?, expires_at = ?").run(old, old);
    f.db.prepare("UPDATE inbound_preparations SET expires_at = ?").run(old);
    f.db.prepare(`INSERT INTO inbound_participant_cleanup
      (edge_id, offer_id, room_name, participant_identity, retry_until)
      VALUES (?, ?, ?, ?, ?)`).run(edge, offer, a.roomName!, a.participantIdentity!, old);
    vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(new Error("offline"))
      .mockImplementation(async () => Response.json({})));
    expect(await Promise.all([sweepPreconnect(f.env, edge), sweepPreconnect(f.env, edge)])).toEqual([null, null]);
    expect(f.db.prepare("SELECT state, cleanup_queued FROM inbound_preparations").get())
      .toMatchObject({ state: "invalid", cleanup_queued: 1 });
    expect(f.queries.filter(query => query.sql.includes("SET completed = 1"))
      .reduce((sum, query) => sum + query.changes, 0)).toBe(1);
    f.queries.length = 0;
    vi.mocked(fetch).mockClear();
    expect(await sweepPreconnect(f.env, edge)).toBeNull();
    expect(f.queries.reduce((sum, query) => sum + query.changes, 0)).toBe(0);
    expect(f.queries.every(query => query.sql.trimStart().startsWith("SELECT"))).toBe(true);
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each(["true", "false"])("drains persistent failures at retry_until and stops with flag %s", async flag => {
    const f = await roomFixture();
    const now = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(now);
    await f.state.storage.put("preconnectEdge", edge);
    f.env.INBOUND_PRECONNECT_ENABLED = flag;
    f.db.prepare("UPDATE inbound_offers SET status = 'revoked'").run();
    f.db.prepare(`INSERT INTO inbound_participant_cleanup
      (edge_id, offer_id, room_name, participant_identity, retry_until)
      VALUES (?, ?, 'private_room', 'private_identity', ?)`).run(edge, offer, now + 30_000);
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ code: "unavailable", msg: "private_room private_identity secret" }, { status: 503 })));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    for (const elapsed of [0, 15_000, 30_000]) {
      vi.mocked(Date.now).mockReturnValue(now + elapsed);
      await f.room.alarm();
      expect(f.db.prepare("SELECT completed FROM inbound_participant_cleanup").get()?.completed).toBe(elapsed === 30_000 ? 1 : 0);
      if (elapsed < 30_000) expect(f.state.storage.setAlarm).toHaveBeenLastCalledWith(now + elapsed + 15_000);
    }
    expect(f.state.storage.deleteAlarm).toHaveBeenCalledOnce();
    expect(warn).toHaveBeenCalledTimes(3);
    expect(warn.mock.calls[0]).toEqual(["Inbound participant cleanup summary", expect.objectContaining({
      attempted: 1, failed: 1, completed: 0, pending: 1,
      reasons: [{ errorName: "LiveKitParticipantError", httpStatus: 503, code: "unavailable", count: 1 }]
    })]);
    expect(warn.mock.calls[2]![1]).toMatchObject({ completed: 1, pending: 0 });
    expect(JSON.stringify(warn.mock.calls)).not.toMatch(/private_room|private_identity|secret|Bearer/);
    f.queries.length = 0;
    await f.room.alarm();
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(warn).toHaveBeenCalledTimes(3);
    expect(f.queries.every(query => query.sql.trimStart().startsWith("SELECT"))).toBe(true);
  });

  it.each(["offered", "claimed", "edge_ready"])("schedules active %s offers every three seconds", async status => {
    const f = await roomFixture();
    const now = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(now);
    await f.state.storage.put("preconnectEdge", edge);
    f.db.prepare("UPDATE inbound_offers SET status = ?").run(status);
    await f.room.alarm();
    expect(f.state.storage.setAlarm).toHaveBeenLastCalledWith(now + 3000);
    expect(f.state.storage.deleteAlarm).not.toHaveBeenCalled();
  });

  it.each([true, false])("deletes an idle alarm without D1 writes (edge stored: %s)", async stored => {
    const f = await roomFixture();
    if (stored) await f.state.storage.put("preconnectEdge", edge);
    // A recently updated terminal offer alone is not work.
    f.db.prepare("UPDATE inbound_offers SET status = 'revoked'").run();
    await f.room.alarm();
    expect(f.state.storage.deleteAlarm).toHaveBeenCalledOnce();
    expect(f.queries.every(query => query.sql.trimStart().startsWith("SELECT"))).toBe(true);
  });

  it("caps cleanup concurrency at four and aggregates failure reasons once", async () => {
    const f = await fixture();
    const now = Date.now();
    f.db.prepare("UPDATE inbound_offers SET status = 'revoked'").run();
    for (let i = 0; i < 9; i++) f.db.prepare(`INSERT INTO inbound_participant_cleanup
      (edge_id, offer_id, room_name, participant_identity, retry_until)
      VALUES (?, ?, 'private_room', ?, ?)`).run(edge, offer, `private_identity_${i}`, now + 60_000);
    let running = 0;
    let peak = 0;
    const releases: (() => void)[] = [];
    vi.stubGlobal("fetch", vi.fn(async () => {
      peak = Math.max(peak, ++running);
      await new Promise<void>(resolve => { releases.push(resolve); });
      running--;
      throw new TypeError("private_room private_identity token-secret");
    }));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const sweep = sweepPreconnect(f.env, edge);
    for (const total of [4, 8, 9]) {
      await vi.waitFor(() => { expect(fetch).toHaveBeenCalledTimes(total); });
      releases.splice(0).forEach(resolve => resolve());
    }
    expect(await sweep).toBe(15_000);
    expect(peak).toBe(4);
    expect(warn).toHaveBeenCalledExactlyOnceWith("Inbound participant cleanup summary", expect.objectContaining({
      attempted: 9, failed: 9, pending: 9, reasons: [{ errorName: "TypeError", count: 9 }]
    }));
    expect(JSON.stringify(warn.mock.calls)).not.toMatch(/private_room|private_identity|token-secret/);
  });

  it("retains a slow fallback alarm when D1 fails", async () => {
    const f = await roomFixture();
    const now = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(now);
    await f.state.storage.put("preconnectEdge", edge);
    vi.spyOn(f.env.DB, "prepare").mockImplementation(() => { throw new Error("D1 unavailable"); });
    await expect(f.room.alarm()).rejects.toThrow("D1 unavailable");
    expect(f.state.storage.setAlarm).toHaveBeenLastCalledWith(now + 15_000);
    expect(f.state.storage.deleteAlarm).not.toHaveBeenCalled();
  });

  it("switches from active to cleanup cadence and waits for the last deadline", async () => {
    const f = await roomFixture();
    const now = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(now);
    await f.state.storage.put("preconnectEdge", edge);
    f.db.prepare("UPDATE inbound_offers SET status = 'edge_ready'").run();
    for (const delay of [15_000, 30_000]) f.db.prepare(`INSERT INTO inbound_participant_cleanup
      (edge_id, offer_id, room_name, participant_identity, retry_until)
      VALUES (?, ?, 'old_room', ?, ?)`).run(edge, offer, `loser_${delay}`, now + delay);
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ code: "not_found" }, { status: 404 })));
    const warn = vi.spyOn(console, "warn");
    await f.room.alarm();
    expect(f.state.storage.setAlarm).toHaveBeenLastCalledWith(now + 3000);
    f.db.prepare("UPDATE inbound_offers SET status = 'revoked'").run();
    for (const elapsed of [15_000, 30_000]) {
      vi.mocked(Date.now).mockReturnValue(now + elapsed);
      await f.room.alarm();
      expect(f.db.prepare("SELECT COUNT(*) AS count FROM inbound_participant_cleanup WHERE completed = 0").get()?.count)
        .toBe(elapsed === 15_000 ? 1 : 0);
      if (elapsed === 15_000) {
        expect(f.state.storage.setAlarm).toHaveBeenLastCalledWith(now + 30_000);
        expect(f.state.storage.deleteAlarm).not.toHaveBeenCalled();
      }
    }
    expect(f.state.storage.deleteAlarm).toHaveBeenCalledOnce();
    expect(warn).not.toHaveBeenCalled();
  });

  it("persists token safety bounds for a reused Edge and promoted phone candidate", async () => {
    const f = await fixture();
    const now = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(now);
    const a = await (await f.request("prepare", { offerId: offer })).json() as Record<string, string>;
    const expires = now + 120_000;
    await queueParticipantCleanup(f.env, { edge_id: edge, offer_id: offer, expires_at: expires }, a.roomName!, "edgepart_prepared");
    expect(f.db.prepare("SELECT retry_until FROM inbound_participant_cleanup").get()?.retry_until).toBe(expires + 360_000);
    f.db.prepare("UPDATE inbound_offers SET status = 'revoked', phone_identity = ?, updated_at = ?").run(a.participantIdentity!, now);
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
    expect(await sweepPreconnect(f.env, edge)).toBe(15_000);
    expect(f.db.prepare("SELECT retry_until FROM inbound_participant_cleanup WHERE participant_identity = ?")
      .get(a.participantIdentity!)?.retry_until).toBe(now + 360_000);
    // Later metadata changes must not extend an already queued deadline.
    vi.mocked(Date.now).mockReturnValue(expires + 360_000);
    f.db.prepare("UPDATE inbound_offers SET updated_at = ?").run(Date.now());
    expect(await sweepPreconnect(f.env, edge)).toBeNull();
    expect(f.db.prepare("SELECT COUNT(*) AS count FROM inbound_participant_cleanup WHERE completed = 0").get()?.count).toBe(0);
  });

  it.each(["invalid-json", "untrusted-code", "abort", "untrusted-name"])("logs safe diagnostics for %s", async failure => {
    const f = await fixture();
    f.db.prepare("UPDATE inbound_offers SET status = 'revoked'").run();
    f.db.prepare(`INSERT INTO inbound_participant_cleanup
      (edge_id, offer_id, room_name, participant_identity, retry_until)
      VALUES (?, ?, 'private_room', 'private_identity', 0)`).run(edge, offer);
    vi.stubGlobal("fetch", vi.fn(async () => {
      if (failure === "abort") throw new DOMException("private_identity", "AbortError");
      if (failure === "untrusted-name") throw Object.assign(new Error("token-secret"), { name: "private_identity" });
      return failure === "invalid-json" ? new Response("private_room token-secret", { status: 502 })
        : Response.json({ code: "private_identity", msg: "token-secret" }, { status: 503 });
    }));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(await sweepPreconnect(f.env, edge)).toBeNull();
    expect(warn).toHaveBeenCalledExactlyOnceWith("Inbound participant cleanup summary", expect.objectContaining({
      failed: 1, completed: 1, pending: 0, reasons: [expect.objectContaining({
        errorName: failure === "abort" ? "AbortError" : failure === "untrusted-name" ? "UnknownError" : "LiveKitParticipantError",
        ...(["invalid-json", "untrusted-code"].includes(failure) ? { httpStatus: failure === "invalid-json" ? 502 : 503 } : {}), count: 1
      })]
    }));
    expect(JSON.stringify(warn.mock.calls)).not.toMatch(/private_room|private_identity|token-secret/);
  });

  it("repeated invalidation changes only candidates whose state changes", async () => {
    const f = await fixture();
    const a = await (await f.request("prepare", { offerId: offer })).json() as Record<string, string>;
    await f.request("prepare", { offerId: offer }, "device_otherabcdefg");
    await invalidateCandidates(f.env, offer, a.prepareId);
    f.queries.length = 0;
    await invalidateCandidates(f.env, offer, a.prepareId);
    expect(f.queries.reduce((sum, query) => sum + query.changes, 0)).toBe(0);
    expect(f.db.prepare("SELECT state FROM inbound_preparations WHERE prepare_id = ?").get(a.prepareId!)?.state).toBe("winner");
    await invalidateCandidates(f.env, offer);
    f.queries.length = 0;
    await invalidateCandidates(f.env, offer);
    expect(f.queries.reduce((sum, query) => sum + query.changes, 0)).toBe(0);
  });

  it("revocation empties both candidates and Edge, including a late restricted rejoin", async () => {
    const f = await roomFixture();
    f.db.prepare("DELETE FROM inbound_offers").run();
    await f.message({ v: 1, type: "inbound.offer", offerId: offer, callId: "call_abcdefghijkl", generation: 1,
      nonce: "abcdefghijklmnop", expiresAtUnixMs: Date.now() + 15_000, mode: "phone_first" });
    const edgeCommand = JSON.parse(f.socket.send.mock.calls[0]![0]);
    const a = await (await f.request("prepare", { offerId: offer })).json() as Record<string, string>;
    const b = await (await f.request("prepare", { offerId: offer }, "device_otherabcdefg")).json() as Record<string, string>;
    const members = new Set([edgeCommand.session.edgeIdentity, a.participantIdentity, b.participantIdentity]);
    vi.stubGlobal("fetch", vi.fn(async (url: URL, init: RequestInit) => {
      if (String(url).endsWith("/RemoveParticipant")) members.delete(JSON.parse(init.body as string).identity);
      return Response.json({});
    }));
    expect((await f.request("claim", { offerId: offer, prepareId: a.prepareId })).status).toBe(202);
    await f.message({ v: 1, type: "inbound.offer.revoke", offerId: offer, callId: "call_abcdefghijkl", reason: "CALL_ENDED" });
    await Promise.all(f.background);
    expect(members.size).toBe(0);
    members.add(b.participantIdentity);
    await f.room.alarm();
    expect(members.size).toBe(0);
    expect((await f.request("prepare", { offerId: offer })).status).toBe(409);
  });

  it("allocates the room once, sends fenced prepare, and replay never resurrects it", async () => {
    const f = await roomFixture();
    f.db.prepare("DELETE FROM inbound_offers").run();
    const event = { v: 1, type: "inbound.offer", offerId: offer, callId: "call_abcdefghijkl", generation: 7,
      nonce: "abcdefghijklmnop", expiresAtUnixMs: Date.now() + 15_000, mode: "phone_first" };
    await f.message(event);
    const first = f.db.prepare("SELECT * FROM inbound_offers").get();
    const command = JSON.parse(f.socket.send.mock.calls[0]![0]);
    expect(command).toMatchObject({ type: "inbound.prepare", offerId: offer, generation: 7, nonce: event.nonce, mode: "phone_first" });
    expect(command.session.roomName).toBe(first!.room_name);
    expect(command.session.edgeIdentity).toBe(first!.edge_identity);
    await f.message(event);
    expect(f.socket.send).toHaveBeenCalledOnce();
    expect(f.db.prepare("SELECT * FROM inbound_offers").get()).toEqual(first);
    await Promise.all(f.background);
  });

  it.each(["flag", "capability", "mode"])("does not send prepare without %s", async missing => {
    const f = await roomFixture();
    f.db.prepare("DELETE FROM inbound_offers").run();
    if (missing === "flag") f.env.INBOUND_PRECONNECT_ENABLED = "false";
    if (missing === "capability") await f.state.storage.put("presence", { connected: true });
    await f.message({ v: 1, type: "inbound.offer", offerId: offer, callId: "call_abcdefghijkl", generation: 1,
      nonce: "abcdefghijklmnop", expiresAtUnixMs: Date.now() + 15_000,
      ...(missing === "mode" ? {} : { mode: "phone_first" }) });
    expect(f.socket.send).not.toHaveBeenCalled();
    expect(f.state.storage.setAlarm).not.toHaveBeenCalled();
    await Promise.all(f.background);
  });

  it("Edge disconnect invalidates offered candidates and removes room participants", async () => {
    const f = await roomFixture();
    await f.state.storage.put("preconnectEdge", edge);
    await f.request("prepare", { offerId: offer });
    const admin = vi.fn(async () => Response.json({}));
    vi.stubGlobal("fetch", admin);
    f.socket.readyState = WebSocket.CLOSED;
    await f.room.webSocketClose(f.socket as unknown as WebSocket, 1000, "closed");
    expect(f.db.prepare("SELECT status FROM inbound_offers").get()?.status).toBe("revoked");
    expect(f.db.prepare("SELECT state FROM inbound_preparations").get()?.state).toBe("invalid");
    expect(admin).toHaveBeenCalled();
  });

  it("expiry alarm invalidates candidates at the exact deadline without touching active claims", async () => {
    const f = await fixture();
    await f.request("prepare", { offerId: offer });
    f.db.prepare("UPDATE inbound_offers SET expires_at = ?").run(Date.now());
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
    await sweepPreconnect(f.env, edge);
    expect(f.db.prepare("SELECT status FROM inbound_offers").get()?.status).toBe("expired");
    expect(f.db.prepare("SELECT state FROM inbound_preparations").get()?.state).toBe("invalid");
    f.db.prepare("UPDATE inbound_offers SET status = 'edge_ready'").run();
    await sweepPreconnect(f.env, edge);
    expect(f.db.prepare("SELECT status FROM inbound_offers").get()?.status).toBe("edge_ready");
  });

  it("a concurrent prepare cannot return a privileged credential after a cold claim", async () => {
    const f = await fixture();
    const [prepared, claimed] = await Promise.all([
      f.request("prepare", { offerId: offer }), f.request("claim", { offerId: offer }, "device_otherabcdefg")
    ]);
    expect(claimed.status).toBe(202);
    if (prepared.status === 200) {
      const result = await prepared.json() as { token: string };
      expect(JSON.parse(atob(result.token.split(".")[1]!)).video.canSubscribe).toBe(false);
    } else expect(prepared.status).toBe(409);
    await Promise.all(f.background);
  });

  it.each([undefined, "false", "TRUE"])("fails closed with flag %s", async flag => {
    const f = await fixture();
    f.env.INBOUND_PRECONNECT_ENABLED = flag;
    const response = await f.request("prepare", { offerId: offer });
    expect(response.status).toBe(409);
    expect(await response.json()).toMatchObject({ error: { code: "PRECONNECT_UNAVAILABLE" } });
    expect(f.db.prepare("SELECT * FROM inbound_preparations").all()).toHaveLength(0);
  });

  it("requires live Edge capability", async () => {
    const f = await fixture();
    f.relay.mockImplementation(async () => Response.json({ connected: true }));
    expect((await f.request("prepare", { offerId: offer })).status).toBe(409);
  });

  it.each(["revoked", "expired", "claimed"])("rejects prepare after %s", async status => {
    const f = await fixture();
    f.db.prepare("UPDATE inbound_offers SET status = ?").run(status);
    expect((await f.request("prepare", { offerId: offer })).status).toBe(409);
    expect(f.db.prepare("SELECT * FROM inbound_preparations").all()).toHaveLength(0);
  });

  it("fences cross-Edge claims and prepares without consuming the offer", async () => {
    const f = await fixture();
    f.db.prepare("INSERT INTO edges VALUES ('edge_otherabcdefg', 'Other', 'key', 'hash', 0, NULL, NULL)").run();
    f.db.prepare("UPDATE devices SET edge_id = 'edge_otherabcdefg' WHERE device_id = ?").run(device);
    expect((await f.request("prepare", { offerId: offer })).status).toBe(409);
    expect((await f.request("claim", { offerId: offer })).status).toBe(404);
    expect(f.db.prepare("SELECT status FROM inbound_offers").get()?.status).toBe("offered");
  });

  it.each(["true", "false", undefined])("grants only the CAS winner and removes losers with fast flag %s", async fastFlag => {
    const f = await fixture();
    f.env.INBOUND_CLAIM_FAST_PATH_ENABLED = fastFlag;
    const a = await (await f.request("prepare", { offerId: offer })).json() as Record<string, string>;
    const b = await (await f.request("prepare", { offerId: offer }, "device_otherabcdefg")).json() as Record<string, string>;
    const admin = vi.fn(async (_url: URL, init: RequestInit) => {
      const body = JSON.parse(init.body as string);
      if (body.permission) {
        const winnerDevice = f.db.prepare("SELECT claimed_device_id FROM inbound_offers").get()?.claimed_device_id;
        expect([device, "device_otherabcdefg"]).toContain(winnerDevice);
        expect(body.identity).toBe((winnerDevice === device ? a : b).participantIdentity);
        expect(body.permission).toMatchObject({ canPublish: true, canSubscribe: true, canPublishData: true,
          canPublishSources: [2], canUpdateMetadata: false, hidden: false, recorder: false });
      }
      return Response.json({});
    });
    vi.stubGlobal("fetch", admin);
    const [one, two] = await Promise.all([
      f.request("claim", { offerId: offer, prepareId: a.prepareId }),
      f.request("claim", { offerId: offer, prepareId: b.prepareId }, "device_otherabcdefg")
    ]);
    expect([one.status, two.status].sort()).toEqual([202, 409]);
    const [winner, loser] = one.status === 202 ? [a, b] : [b, a];
    expect(await (one.status === 202 ? one : two).json()).toMatchObject({ mode: "reuse", participantIdentity: winner.participantIdentity });
    const commands = f.relay.mock.calls.filter(args => String(args[0]).endsWith("/command"));
    expect(commands).toHaveLength(1);
    await sweepPreconnect(f.env, edge);
    const removals = admin.mock.calls.filter(args => String(args[0]).endsWith("/RemoveParticipant"))
      .map(args => JSON.parse(args[1].body as string).identity);
    expect(removals).toContain(loser.participantIdentity);
    expect(removals).not.toContain(winner.participantIdentity);
    await Promise.all(f.background);
  });

  it("admin failure isolates cold credentials and invalidates the attempted candidate", async () => {
    const f = await fixture();
    const a = await (await f.request("prepare", { offerId: offer })).json() as Record<string, string>;
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ code: "unavailable" }, { status: 503 })));
    const response = await f.request("claim", { offerId: offer, prepareId: a.prepareId });
    const result = await response.json() as Record<string, string>;
    expect(response.status).toBe(202);
    expect(result.mode).toBe("cold");
    expect(result.roomName).not.toBe(a.roomName);
    expect(result.participantIdentity).not.toBe(a.participantIdentity);
    expect(JSON.parse(atob(result.token!.split(".")[1]!)).video.canPublish).toBe(true);
    expect(f.db.prepare("SELECT state FROM inbound_preparations").get()?.state).toBe("invalid");
    await Promise.all(f.background);
  });

  it("late promotion cannot resurrect a revoked offer", async () => {
    const f = await fixture();
    const a = await (await f.request("prepare", { offerId: offer })).json() as Record<string, string>;
    vi.stubGlobal("fetch", vi.fn(async () => {
      f.db.prepare("UPDATE inbound_offers SET status = 'revoked'").run();
      return Response.json({});
    }));
    expect((await f.request("claim", { offerId: offer, prepareId: a.prepareId })).status).toBe(409);
    expect(f.relay.mock.calls.filter(args => String(args[0]).endsWith("/command"))).toHaveLength(0);
    expect(f.db.prepare("SELECT state FROM inbound_preparations").get()?.state).toBe("invalid");
    await Promise.all(f.background);
  });

  it("legacy winner keeps the exact response shape and evicts prepared iPhones", async () => {
    const f = await fixture();
    await f.request("prepare", { offerId: offer });
    const response = await f.request("claim", { offerId: offer }, "device_otherabcdefg");
    expect(response.status).toBe(202);
    expect(Object.keys(await response.json()).sort()).toEqual(["claimId", "expiresAt", "offerId", "roomName", "token", "url"]);
    expect(f.db.prepare("SELECT state FROM inbound_preparations").get()?.state).toBe("invalid");
    await Promise.all(f.background);
  });

  it.each(["revoked", "expired", "failed"])("terminal %s invalidates candidates and retries removal after failure", async status => {
    const f = await fixture();
    const a = await (await f.request("prepare", { offerId: offer })).json() as Record<string, string>;
    f.db.prepare("UPDATE inbound_offers SET status = ?").run(status);
    const admin = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(Response.json({}));
    vi.stubGlobal("fetch", admin);
    await sweepPreconnect(f.env, edge);
    await sweepPreconnect(f.env, edge);
    expect(admin).toHaveBeenCalledTimes(2);
    expect(JSON.parse(admin.mock.calls[1]![1].body).identity).toBe(a.participantIdentity);
    expect(f.db.prepare("SELECT state FROM inbound_preparations").get()?.state).toBe("invalid");
    expect((await f.request("prepare", { offerId: offer })).status).toBe(409);
  });

  it("bounds the entire admin response body wait and aborts the request", async () => {
    const f = await fixture();
    vi.stubGlobal("fetch", vi.fn((_url, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal!.addEventListener("abort", () => reject(new Error("aborted")));
    })));
    await expect(participantRequest(f.env, "UpdateParticipant", "room", "winner", true, 10)).rejects.toThrow("aborted");
  });

  it("does not downgrade an HTTPS admin endpoint", async () => {
    const f = await fixture();
    f.env.LIVEKIT_URL = "https://media.test";
    const admin = vi.fn(async () => Response.json({}));
    vi.stubGlobal("fetch", admin);
    await participantRequest(f.env, "RemoveParticipant", "room", "identity");
    expect(String(vi.mocked(fetch).mock.calls[0]![0])).toBe("https://media.test/twirp/livekit.RoomService/RemoveParticipant");
  });

  it("atomically returns one restricted candidate per device, never shared identities", async () => {
    const f = await fixture();
    const responses = await Promise.all([f.request("prepare", { offerId: offer }), f.request("prepare", { offerId: offer })]);
    expect(responses.map(r => r.status)).toEqual([200, 200]);
    const [a, b] = await Promise.all(responses.map(r => r.json())) as Record<string, string>[];
    expect(a!.prepareId).toBe(b!.prepareId);
    const c = await (await f.request("prepare", { offerId: offer }, "device_otherabcdefg")).json() as Record<string, string>;
    expect(c.participantIdentity).not.toBe(a!.participantIdentity);
    const grants = JSON.parse(atob(a!.token!.split(".")[1]!)).video;
    expect(grants).toMatchObject({ canPublish: false, canSubscribe: false, canPublishData: false });
    await Promise.all(f.background);
  });
});
