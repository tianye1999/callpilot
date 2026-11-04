import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync, type SQLInputValue } from "node:sqlite";
import { URL } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Env } from "../src/types";

vi.mock("cloudflare:workers", () => ({ DurableObject: class {
  constructor(protected ctx: DurableObjectState, protected env: Env) {}
} }));
import worker from "../src/index";
import { EdgeRoom, OUTBOUND_DIRECT_START_MAX_MS } from "../src/edge-room";
import { callMediaCommandSchema, createCallSchema } from "../src/schemas";
import { sha256 } from "../src/security";

const databases: DatabaseSync[] = [];
const edge = "edge_abcdefghijkl";
const device = "device_abcdefghijkl";
const sdp = "v=0\r\ns=synthetic\r\n";
const answerSdp = "v=0\r\ns=edge-answer\r\n";
const iceServers = [{ urls: ["turn:turn.example:3478?transport=udp"], username: "temporary", credential: "temporary-password" }];
type Command = { type: string; commandId: string; callId: string; direct?: { sdpOffer: string; iceServers: unknown[] };
  session: { token: string; roomName: string; browserIdentity: string; edgeIdentity: string } };

async function fixture(options: { edgeDirect?: boolean; enabled?: boolean; rateGate?: Promise<void>;
  auditGate?: Promise<void>; failAudit?: boolean; insertGate?: Promise<void> } = {}) {
  const db = new DatabaseSync(":memory:");
  databases.push(db);
  for (const name of readdirSync(new URL("../migrations/", import.meta.url)).filter(n => n.endsWith(".sql")).sort()) {
    db.exec(readFileSync(new URL(`../migrations/${name}`, import.meta.url), "utf8"));
  }
  db.prepare("INSERT INTO edges VALUES (?, 'Test', 'public', 'hash', 0, NULL, NULL)").run(edge);
  db.prepare("INSERT INTO devices VALUES (?, ?, 'Test', ?, 0, ?, NULL)").run(device, edge, await sha256("secret"), Date.now());
  const env = {
    DB: {
      batch: vi.fn(async (statements: D1PreparedStatement[]) => {
        await options.rateGate;
        db.exec("BEGIN");
        try {
          const results = statements.map(statement => (statement as unknown as { execute(): unknown }).execute());
          db.exec("COMMIT");
          return results;
        } catch (error) {
          db.exec("ROLLBACK");
          throw error;
        }
      }),
      prepare: (sql: string) => ({ bind: (...values: SQLInputValue[]) => {
        const params = Object.fromEntries(values.map((value, index) => [String(index + 1), value]));
        return {
          execute: () => {
            return { results: db.prepare(sql).all(params) };
          },
          first: async () => {
            if (sql.includes("SELECT COUNT(*) AS count FROM calls")) await options.rateGate;
            return db.prepare(sql).get(params) ?? null;
          },
          all: async () => ({ results: db.prepare(sql).all(params) }),
          run: async () => {
            if (sql.startsWith("INSERT OR IGNORE INTO calls")) await options.insertGate;
            if (sql.startsWith("INSERT INTO audit_events")) {
              await options.auditGate;
              if (options.failAudit) throw new Error("synthetic private audit failure");
            }
            return { meta: { changes: Number(db.prepare(sql).run(params).changes) } };
          }
        };
      } })
    },
    PUBLIC_ORIGIN: "https://relay", OUTBOUND_DIRECT_ENABLED: options.enabled === false ? "false" : "true",
    LIVEKIT_URL: "wss://media.test", LIVEKIT_API_KEY: "testkey", LIVEKIT_API_SECRET: "testsecret",
    TURN_KEY_ID: "synthetic-key", TURN_API_TOKEN: "synthetic-token"
  } as unknown as Env;
  const commands: Command[] = [];
  let edgeReply: (command: Command) => Response = command => command.type === "session.start"
    ? Response.json({ v: 1, type: "call.answer", commandId: command.commandId, callId: command.callId,
      status: "accepted", sdpAnswer: answerSdp })
    : Response.json({ v: 1, type: "call.answer", commandId: command.commandId, callId: command.callId, status: "accepted" });
  const relay = vi.fn(async (url: string, init?: RequestInit): Promise<Response> => {
    if (url.endsWith("/presence")) {
      return Response.json({ connected: true, modemOnline: true, lineBusy: false,
        ...(options.edgeDirect === false ? {} : { outboundDirectV1: true }) });
    }
    const command = JSON.parse(init!.body as string) as Command;
    commands.push(command);
    if (url.endsWith("/call-media-relay")) return edgeReply(command);
    return Response.json({ accepted: true }, { status: 202 });
  });
  env.EDGE_ROOMS = { idFromName: (id: string) => id, get: vi.fn(() => ({ fetch: relay })) } as unknown as DurableObjectNamespace;
  const background: Promise<unknown>[] = [];
  const request = (path: string, body?: object, cookie = `${device}.secret`) => worker.fetch(new Request(`https://relay${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { Origin: "https://relay", "Content-Type": "application/json", Cookie: `__Host-callpilot-device=${cookie}` },
    ...(body === undefined ? {} : { body: JSON.stringify(body) })
  }), env, { waitUntil: (operation: Promise<unknown>) => { background.push(operation); } } as unknown as ExecutionContext);
  const turn = vi.fn(async () => Response.json({ iceServers }));
  vi.stubGlobal("fetch", turn);
  return { db, env, request, relay, commands, turn, background, setEdgeReply: (reply: typeof edgeReply) => { edgeReply = reply; } };
}

const create = (key = "idempotency-key-0001", extra: object = { mediaTransport: "webrtc", sdpOffer: sdp }) =>
  ({ edgeId: edge, idempotencyKey: key, ...extra });
const row = (f: Awaited<ReturnType<typeof fixture>>) =>
  f.db.prepare("SELECT status, error_code, media_transport FROM calls").get() as { status: string; error_code: string | null; media_transport: string | null };

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); databases.splice(0).forEach(db => db.close()); });

describe("outbound direct ICE", () => {
  it("issues phone ICE only when the relay flag, TURN and the Edge capability all allow it", async () => {
    const f = await fixture();
    const response = await f.request("/v1/calls/ice", { edgeId: edge });
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ iceServers });
    for (const options of [{ enabled: false }, { edgeDirect: false }]) {
      const blocked = await fixture(options);
      const refused = await blocked.request("/v1/calls/ice", { edgeId: edge });
      expect(refused.status).toBe(503);
      expect(await refused.json()).toMatchObject({ error: { code: "DIRECT_UNAVAILABLE" } });
    }
    expect((await f.request("/v1/calls/ice", { edgeId: "edge_otherabcdefg" })).status).toBe(403);
  });
});

describe("outbound direct device feature", () => {
  it("advertises outbound direct to phones only when the relay and the Edge both enable it", async () => {
    const features = async (options: { edgeDirect?: boolean; enabled?: boolean }) => {
      const f = await fixture(options);
      return (await (await f.request("/v1/device")).json() as { features: Record<string, unknown> }).features;
    };
    expect((await features({})).outboundDirectV1).toBe(true);
    expect((await features({ edgeDirect: false })).outboundDirectV1).toBe(false);
    expect((await features({ enabled: false })).outboundDirectV1).toBeUndefined();
  });
});

describe("outbound direct create", () => {
  it("batches fresh auth with checks and rejects revoked credentials even on replay", async () => {
    const f = await fixture();
    const response = await f.request("/v1/calls", create());
    expect(response.status).toBe(202);
    expect(response.headers.get("Server-Timing")).toMatch(/auth_checks;dur=/);
    expect(f.env.DB.batch).toHaveBeenCalledTimes(2);
    expect(vi.mocked(f.env.DB.batch).mock.calls[0]![0]).toHaveLength(3);
    f.db.prepare("UPDATE devices SET revoked_at = ? WHERE device_id = ?").run(Date.now(), device);
    expect((await f.request("/v1/calls", create())).status).toBe(401);
    expect(f.commands).toHaveLength(1);
  });

  it("does not expose replay, rate or validation results to invalid credentials", async () => {
    const f = await fixture();
    for (let i = 0; i < 10; i++) await f.request("/v1/calls", create(`idempotency-key-${i.toString().padStart(4, "0")}`));
    f.relay.mockClear();
    f.db.prepare("UPDATE devices SET last_used_at = 0").run();
    for (const body of [create(), {}, create("new-idempotency-key", {})]) {
      expect((await f.request("/v1/calls", body, `${device}.wrong`)).status).toBe(401);
    }
    expect((await f.request("/v1/calls", create(), "device_missingabc.secret")).status).toBe(401);
    expect((await f.request("/v1/calls", create(), "")).status).toBe(401);
    expect(f.db.prepare("SELECT last_used_at FROM devices").get()).toMatchObject({ last_used_at: 0 });
    expect(f.relay.mock.calls.every(([url]) => url.endsWith("/presence"))).toBe(true);
    expect(f.commands).toHaveLength(10);
  });

  it("rejects replay and new calls immediately after the device unpairs", async () => {
    const f = await fixture();
    await f.request("/v1/calls", create());
    const unpaired = await worker.fetch(new Request("https://relay/v1/device", {
      method: "DELETE", headers: { Origin: "https://relay", Cookie: `__Host-callpilot-device=${device}.secret` }
    }), f.env);
    expect(unpaired.status).toBe(200);
    f.relay.mockClear();
    expect((await f.request("/v1/calls", create())).status).toBe(401);
    expect((await f.request("/v1/calls", create("another-idempotency-key"))).status).toBe(401);
    expect(f.relay.mock.calls.every(([url]) => url.endsWith("/presence"))).toBe(true);
    expect(f.commands).toHaveLength(1);
  });

  it("keeps pairing and validation errors ahead of replay and presence", async () => {
    const f = await fixture();
    await f.request("/v1/calls", create());
    f.relay.mockClear();
    expect((await f.request("/v1/calls", { ...create(), edgeId: "edge_otherabcdefg" })).status).toBe(403);
    expect((await f.request("/v1/calls", {})).status).toBe(400);
    expect(f.relay.mock.calls.every(([url]) => url.endsWith("/presence"))).toBe(true);
  });

  it.each([
    { connected: false },
    { connected: true, modemOnline: false },
    { connected: true, lineBusy: true }
  ])("does not insert a call when presence rejects admission: %j", async state => {
    const f = await fixture();
    f.relay.mockResolvedValue(Response.json(state));
    expect((await f.request("/v1/calls", create())).status).toBe(409);
    expect(f.db.prepare("SELECT COUNT(*) AS count FROM calls").get()).toMatchObject({ count: 0 });
    expect(f.commands).toHaveLength(0);
  });
  it("returns an idempotent replay without waiting for stalled speculative presence", async () => {
    const f = await fixture();
    await f.request("/v1/calls", create());
    f.relay.mockClear();
    let release!: (response: Response) => void;
    const presence = new Promise<Response>(resolve => { release = resolve; });
    f.relay.mockImplementationOnce(() => presence);
    try {
      expect((await f.request("/v1/calls", create())).status).toBe(200);
      expect(f.relay).toHaveBeenCalledExactlyOnceWith("https://edge-room/presence");
    } finally { release(Response.json({ connected: false })); }
    expect(f.commands).toHaveLength(1);
    await Promise.all(f.background);
  });
  it("uses two D1 batches and preserves a concurrent terminal status during answer finalization", async () => {
    const f = await fixture();
    f.setEdgeReply(command => {
      f.db.prepare("UPDATE calls SET status = 'ended' WHERE call_id = ?").run(command.callId);
      return Response.json({ v: 1, type: "call.answer", commandId: command.commandId,
        callId: command.callId, status: "accepted", sdpAnswer: answerSdp });
    });
    const response = await f.request("/v1/calls", create());
    expect(await response.json()).toMatchObject({ status: "ended" });
    expect(f.env.DB.batch).toHaveBeenCalledTimes(2);
    expect(row(f).status).toBe("ended");
    await Promise.all(f.background);
  });

  it("keeps INSERT OR IGNORE races idempotent with only one Edge command", async () => {
    const f = await fixture();
    const responses = await Promise.all(Array.from({ length: 8 }, () => f.request("/v1/calls", create())));
    expect(responses.map(response => response.status).sort()).toEqual([200, 200, 200, 200, 200, 200, 200, 202]);
    const bodies = await Promise.all(responses.map(response => response.json() as Promise<{ callId: string }>));
    expect(new Set(bodies.map(body => body.callId)).size).toBe(1);
    expect(f.db.prepare("SELECT COUNT(*) AS count FROM calls").get()).toMatchObject({ count: 1 });
    expect(f.commands).toHaveLength(1);
    await Promise.all(f.background);
  });

  it("returns before a blocked audit and only logs a sanitized background failure", async () => {
    let release!: () => void;
    const auditGate = new Promise<void>(resolve => { release = resolve; });
    const f = await fixture({ auditGate, failAudit: true });
    const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
    const response = await f.request("/v1/calls", create());
    expect(response.status).toBe(202);
    expect(f.background).toHaveLength(1);
    expect(warning).not.toHaveBeenCalled();
    release();
    await Promise.all(f.background);
    expect(warning).toHaveBeenCalledWith(expect.stringContaining('"event":"outbound_audit_failed"'));
    expect(JSON.stringify(warning.mock.calls)).not.toContain("private audit failure");
  });

  it("starts presence and auth/checks concurrently while both results are blocked", async () => {
    let releaseAuth!: () => void;
    let releasePresence!: (response: Response) => void;
    const rateGate = new Promise<void>(resolve => { releaseAuth = resolve; });
    const presenceGate = new Promise<Response>(resolve => { releasePresence = resolve; });
    const f = await fixture({ rateGate });
    f.relay.mockImplementationOnce(() => presenceGate);
    const pending = f.request("/v1/calls", create());
    try {
      await vi.waitFor(() => {
        expect(f.env.DB.batch).toHaveBeenCalledOnce();
        expect(f.relay).toHaveBeenCalledExactlyOnceWith("https://edge-room/presence");
      });
      expect(f.commands).toHaveLength(0);
    } finally {
      releaseAuth();
      releasePresence(Response.json({ connected: true, modemOnline: true, outboundDirectV1: true }));
      await pending;
    }
    expect((await pending).status).toBe(202);
  });

  it("starts TURN issuance before the idempotency INSERT completes", async () => {
    let releaseInsert!: () => void;
    const insertGate = new Promise<void>(resolve => { releaseInsert = resolve; });
    const f = await fixture({ insertGate });
    const pending = f.request("/v1/calls", create());
    try {
      await vi.waitFor(() => expect(f.turn).toHaveBeenCalledOnce());
      expect(f.env.DB.batch).toHaveBeenCalledTimes(1);
      expect(f.relay).toHaveBeenCalledExactlyOnceWith("https://edge-room/presence");
      expect(f.commands).toHaveLength(0);
    } finally {
      releaseInsert();
      await pending;
    }
    expect((await pending).status).toBe(202);
  });

  it.each(["invalid", "revoked", "wrong-edge"])("discards rejected speculative presence for %s authorization", async kind => {
    let release!: () => void;
    const rateGate = new Promise<void>(resolve => { release = resolve; });
    const f = await fixture({ rateGate });
    const errorLog = vi.spyOn(console, "error").mockImplementation(() => {});
    if (kind === "revoked") f.db.prepare("UPDATE devices SET revoked_at = 1").run();
    f.relay.mockRejectedValue(new Error("private speculative presence failure"));
    const body = kind === "wrong-edge" ? { ...create(), edgeId: "edge_otherabcdefg" } : create();
    const pending = f.request("/v1/calls", body, `${device}.${kind === "invalid" ? "wrong" : "secret"}`);
    try {
      await vi.waitFor(() => {
        expect(f.env.DB.batch).toHaveBeenCalledOnce();
        expect(f.relay).toHaveBeenCalledOnce();
      });
    } finally { release(); await pending; }
    const response = await pending;
    expect(response.status).toBe(kind === "wrong-edge" ? 403 : 401);
    expect(await response.json()).toEqual({ error: {
      code: kind === "wrong-edge" ? "FORBIDDEN" : "UNAUTHORIZED",
      message: kind === "wrong-edge" ? "Device is not paired to this Edge" : "Credential is missing, invalid, or revoked",
      requestId: expect.stringMatching(/^req_/)
    } });
    expect(f.commands).toHaveLength(0);
    expect(f.db.prepare("SELECT COUNT(*) AS count FROM calls").get()).toMatchObject({ count: 0 });
    expect(errorLog).not.toHaveBeenCalled();
  });

  it("preserves rate-limit precedence with failed presence and returns replay despite that failure", async () => {
    const f = await fixture();
    for (let i = 0; i < 10; i++) await f.request("/v1/calls", create(`idempotency-key-${i.toString().padStart(4, "0")}`));
    f.relay.mockRejectedValue(new Error("synthetic presence failure"));
    const blocked = await f.request("/v1/calls", create("idempotency-key-next"));
    expect(blocked.status).toBe(429);
    expect(await blocked.json()).toMatchObject({ error: { code: "RATE_LIMITED" } });
    const replay = await f.request("/v1/calls", create("idempotency-key-0000"));
    expect(replay.status).toBe(200);
    expect(f.commands).toHaveLength(10);
  });

  it("emits only stage timings and opaque correlation IDs, including failed requests", async () => {
    const f = await fixture();
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    f.setEdgeReply(() => Response.json({ error: "timeout" }, { status: 504 }));
    const response = await f.request("/v1/calls", create());
    expect(response.headers.get("Server-Timing")).toMatch(/sdp_relay;dur=/);
    expect(response.headers.get("Server-Timing")).toMatch(/auth_checks;dur=/);
    const summary = log.mock.calls.map(args => JSON.parse(args[0] as string) as Record<string, unknown>)
      .find(entry => entry.event === "outbound_create");
    expect(summary).toMatchObject({ status: 504 });
    expect(summary!.auth_checks).toBeTypeOf("number");
    expect(summary!.callId).toMatch(/^call_/);
    expect(JSON.stringify(summary)).not.toContain("temporary-password");
    expect(JSON.stringify(summary)).not.toContain("v=0");
  });

  it("relays the offer inside session.start and returns the Edge answer in one request", async () => {
    const f = await fixture();
    const response = await f.request("/v1/calls", create());
    expect(response.status).toBe(202);
    expect(response.headers.get("Server-Timing")).toMatch(/turn;dur=/);
    expect(response.headers.get("Server-Timing")).toMatch(/call_finalize_read;dur=/);
    const body = await response.json() as Record<string, unknown>;
    expect(body).toMatchObject({ status: "ready", directAttempted: true, transports: [{ kind: "webrtc", sdpAnswer: answerSdp }] });
    expect(f.relay.mock.calls.some(([url]) => url.endsWith("/command"))).toBe(false);
    const start = f.commands[0]!;
    expect(start.type).toBe("session.start");
    expect(start.direct).toEqual({ sdpOffer: sdp, iceServers });
    expect(callMediaCommandSchema.safeParse(start).success).toBe(true);
    expect(row(f)).toMatchObject({ status: "ready", media_transport: "direct" });
    // A direct call never hands out the LiveKit credential through polling.
    const polled = await (await f.request(`/v1/calls/${body.callId as string}`)).json() as Record<string, unknown>;
    expect(polled).toMatchObject({ status: "ready", directAttempted: true });
    expect(polled.session).toBeUndefined();
  });

  it("validates dialNumber and forwards it on the direct session.start", async () => {
    const f = await fixture();
    const response = await f.request("/v1/calls", create("idempotency-key-dial", {
      mediaTransport: "webrtc", sdpOffer: sdp, dialNumber: "+8610010086"
    }));
    expect(response.status).toBe(202);
    const start = f.commands[0]!;
    expect(start.type).toBe("session.start");
    expect((start as unknown as { dialNumber?: string }).dialNumber).toBe("+8610010086");
    expect(callMediaCommandSchema.safeParse(start).success).toBe(true);

    const invalid = await f.request("/v1/calls", create("idempotency-key-bad", {
      mediaTransport: "webrtc", sdpOffer: sdp, dialNumber: "10086-"
    }));
    expect(invalid.status).toBe(400);
    expect(await invalid.json()).toMatchObject({ error: { code: "VALIDATION_ERROR" } });
  });

  it("forwards dialNumber on a plain LiveKit session.start as well", async () => {
    const f = await fixture({ edgeDirect: false });
    const body = await (await f.request("/v1/calls", create("idempotency-key-lk", {
      mediaTransport: "webrtc", sdpOffer: sdp, dialNumber: "10086"
    }))).json() as Record<string, unknown>;
    expect(body.directAttempted).toBeUndefined();
    expect(f.commands[0]!.direct).toBeUndefined();
    expect((f.commands[0] as unknown as { dialNumber?: string }).dialNumber).toBe("10086");
  });

  it("starts a plain LiveKit session when the Edge does not advertise outbound direct", async () => {
    const f = await fixture({ edgeDirect: false });
    const body = await (await f.request("/v1/calls", create())).json() as Record<string, unknown>;
    expect(body.transports).toBeUndefined();
    expect(body.directAttempted).toBeUndefined();
    expect(f.relay.mock.calls.some(([url]) => url.endsWith("/call-media-relay"))).toBe(false);
    expect(f.commands[0]!.direct).toBeUndefined();
    expect(row(f).media_transport).toBeNull();
  });

  it("falls back to a plain LiveKit session when Edge TURN credentials are unavailable", async () => {
    const f = await fixture();
    f.turn.mockResolvedValue(new Response("down", { status: 503 }));
    const body = await (await f.request("/v1/calls", create())).json() as Record<string, unknown>;
    expect(body.transports).toBeUndefined();
    expect(f.relay.mock.calls.some(([url]) => url.endsWith("/command"))).toBe(true);
    expect(f.commands[0]!.direct).toBeUndefined();
    expect(row(f)).toMatchObject({ status: "pending", media_transport: null });
  });

  it("keeps a live session for the LiveKit fallback when the Edge could not set up direct media", async () => {
    const f = await fixture();
    f.setEdgeReply(command => Response.json({ v: 1, type: "call.answer", commandId: command.commandId,
      callId: command.callId, status: "rejected", errorCode: "DIRECT_SETUP_FAILED" }, { status: 409 }));
    const body = await (await f.request("/v1/calls", create())).json() as Record<string, unknown>;
    expect(body).toMatchObject({ status: "ready", directAttempted: true, transports: [] });
    expect(row(f)).toMatchObject({ status: "ready", media_transport: "direct_pending" });
  });

  it("reports an Edge rejection as a terminal call", async () => {
    const f = await fixture();
    f.setEdgeReply(command => Response.json({ v: 1, type: "call.answer", commandId: command.commandId,
      callId: command.callId, status: "rejected", errorCode: "LINE_BUSY" }, { status: 409 }));
    const body = await (await f.request("/v1/calls", create())).json() as Record<string, unknown>;
    expect(body).toMatchObject({ status: "failed", errorCode: "LINE_BUSY" });
    expect(body.transports).toBeUndefined();
  });

  it.each([[504, "EDGE_TIMEOUT"], [503, "EDGE_OFFLINE"]])("fails the call when the relay returns %s", async (status, code) => {
    const f = await fixture();
    f.setEdgeReply(() => Response.json({ error: "x" }, { status }));
    const response = await f.request("/v1/calls", create());
    expect(response.status).toBe(status === 504 ? 504 : 409);
    expect(await response.json()).toMatchObject({ error: { code } });
    expect(row(f)).toMatchObject({ status: "failed", error_code: code });
  });

  it("ignores an answer for another call", async () => {
    const f = await fixture();
    f.setEdgeReply(command => Response.json({ v: 1, type: "call.answer", commandId: command.commandId,
      callId: "call_otherabcdefg", status: "accepted", sdpAnswer: answerSdp }));
    expect((await f.request("/v1/calls", create())).status).toBe(409);
    expect(row(f).status).toBe("failed");
  });

  it("answers a replayed create without a second SDP answer, so the phone falls back", async () => {
    const f = await fixture();
    await f.request("/v1/calls", create());
    const replay = await f.request("/v1/calls", create());
    expect(replay.status).toBe(200);
    const body = await replay.json() as Record<string, unknown>;
    expect(body).toMatchObject({ directAttempted: true });
    expect(body.transports).toBeUndefined();
    expect(f.commands).toHaveLength(1);
  });

  it("rejects an SDP offer without the webrtc transport", () => {
    expect(createCallSchema.safeParse({ edgeId: edge, idempotencyKey: "idempotency-key-0001", sdpOffer: sdp }).success).toBe(false);
    expect(createCallSchema.safeParse({ edgeId: edge, idempotencyKey: "idempotency-key-0001" }).success).toBe(true);
  });
});

describe("outbound LiveKit fallback", () => {
  async function directCall() {
    const f = await fixture();
    const body = await (await f.request("/v1/calls", create())).json() as { callId: string };
    return { f, callId: body.callId };
  }

  it("switches the Edge to the existing room once and hands the phone its LiveKit credential", async () => {
    const { f, callId } = await directCall();
    const response = await f.request(`/v1/calls/${callId}/media/livekit`, {});
    expect(response.status).toBe(202);
    const body = await response.json() as { session: { livekitUrl: string; token: string; expiresAt: number } };
    expect(body.session.livekitUrl).toBe("wss://media.test");
    const switchCommand = f.commands[1]!;
    expect(switchCommand.type).toBe("call.media.livekit");
    expect(callMediaCommandSchema.safeParse(switchCommand).success).toBe(true);
    const start = f.commands[0]!;
    expect(switchCommand.session).toMatchObject({ roomName: start.session.roomName,
      browserIdentity: start.session.browserIdentity, edgeIdentity: start.session.edgeIdentity });
    expect(row(f).media_transport).toBe("livekit");
    expect((await f.request(`/v1/calls/${callId}/media/livekit`, {})).status).toBe(202);
    expect(f.commands).toHaveLength(2);
    const polled = await (await f.request(`/v1/calls/${callId}`)).json() as Record<string, unknown>;
    expect(polled.session).toBeDefined();
  });

  it.each(["MEDIA_FALLBACK_REJECTED", "PRIVATE_DEVICE_VALUE"])("logs only an allowlisted rejection category for %s", async errorCode => {
    const { f, callId } = await directCall();
    f.setEdgeReply(command => Response.json({ v: 1, type: "call.answer", commandId: command.commandId,
      callId: command.callId, status: "rejected", errorCode }, { status: 409 }));
    const log = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect((await f.request(`/v1/calls/${callId}/media/livekit`, {})).status).toBe(409);
    expect(log).toHaveBeenCalledWith(JSON.stringify({ event: "outbound_livekit_fallback_rejected",
      reason: errorCode === "PRIVATE_DEVICE_VALUE" ? "other" : errorCode }));
  });

  it("refuses non-direct calls and a disabled relay", async () => {
    const f = await fixture({ edgeDirect: false });
    const body = await (await f.request("/v1/calls", create())).json() as { callId: string };
    f.db.prepare("UPDATE calls SET status = 'ready'").run();
    expect((await f.request(`/v1/calls/${body.callId}/media/livekit`, {})).status).toBe(409);
    f.env.OUTBOUND_DIRECT_ENABLED = "false";
    expect((await f.request(`/v1/calls/${body.callId}/media/livekit`, {})).status).toBe(403);
  });
});

describe("outbound direct DO relay", () => {
  function harness(env: Env) {
    vi.stubGlobal("WebSocketRequestResponsePair", class {});
    const socket = { readyState: WebSocket.OPEN as number, send: vi.fn(), close: vi.fn(),
      deserializeAttachment: () => ({ edgeId: edge, connectedAt: 1 }) };
    const state = { getWebSockets: () => [socket], setWebSocketAutoResponse: vi.fn(),
      storage: { get: vi.fn(), put: vi.fn(), list: vi.fn().mockResolvedValue(new Map()),
        setAlarm: vi.fn(), deleteAlarm: vi.fn() }, waitUntil: vi.fn() };
    return { room: new EdgeRoom(state as unknown as DurableObjectState, env), socket };
  }
  const start = { v: 1, type: "session.start", commandId: "command_abcdefghijkl", callId: "call_abcdefghijkl",
    expiresAt: new Date(Date.now() + 300_000).toISOString(),
    session: { sessionId: "session_abcdefghijkl", roomName: "callpilot_abcdefghijkl", browserIdentity: "web_abcdefghijkl",
      edgeIdentity: "edgepart_abcdefghijkl", livekitUrl: "wss://media.test", token: "synthetic" },
    direct: { sdpOffer: sdp, iceServers } };
  const relayRequest = (body: object) => new Request("https://edge-room/call-media-relay", { method: "POST", body: JSON.stringify(body) });

  it("resolves only on the correlated answer carrying an SDP answer", async () => {
    const f = await fixture();
    const { room, socket } = harness(f.env);
    const pending = room.fetch(relayRequest(start));
    await vi.waitFor(() => expect(socket.send).toHaveBeenCalledOnce());
    for (const wrong of [{ callId: "call_otherabcdefg", sdpAnswer: answerSdp }, { callId: start.callId }]) {
      await room.webSocketMessage(socket as unknown as WebSocket, JSON.stringify({ v: 1, type: "call.answer",
        commandId: start.commandId, status: "accepted", ...wrong }));
    }
    await room.webSocketMessage(socket as unknown as WebSocket, JSON.stringify({ v: 1, type: "call.answer",
      commandId: start.commandId, callId: start.callId, status: "accepted", sdpAnswer: answerSdp }));
    const response = await pending;
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ sdpAnswer: answerSdp });
    expect(Number(response.headers.get("X-CallPilot-edge_wait"))).toBeGreaterThanOrEqual(0);
    expect(response.headers.has("X-CallPilot-do_total")).toBe(true);
  });

  it("times out a silent Edge and rejects a start without direct media", async () => {
    const f = await fixture();
    const { room, socket } = harness(f.env);
    expect((await room.fetch(relayRequest({ ...start, direct: undefined }))).status).toBe(400);
    vi.useFakeTimers();
    let response: Response | undefined;
    const pending = room.fetch(relayRequest(start)).then(value => { response = value; });
    await vi.advanceTimersByTimeAsync(0);
    expect(socket.send).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(OUTBOUND_DIRECT_START_MAX_MS - 1);
    expect(response).toBeUndefined();
    await vi.advanceTimersByTimeAsync(1);
    await pending;
    expect(response?.status).toBe(504);
    expect(vi.getTimerCount()).toBe(0);
  });
});
