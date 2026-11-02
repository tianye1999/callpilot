import { readFileSync, readdirSync } from "node:fs";
import { DatabaseSync, type SQLInputValue } from "node:sqlite";
import { URL } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Env } from "../src/types";

vi.mock("cloudflare:workers", () => ({ DurableObject: class {
  constructor(protected ctx: DurableObjectState, protected env: Env) {}
} }));
import worker from "../src/index";
import { EdgeRoom, INBOUND_MEDIA_RELAY_MAX_MS } from "../src/edge-room";
import { issueIceServers, TURN_REQUEST_MAX_MS } from "../src/turn";
import { sha256 } from "../src/security";
import { inboundMediaCommandSchema } from "../src/schemas";

const databases: DatabaseSync[] = [];
const edge = "edge_abcdefghijkl";
const offer = "offer_abcdefghijkl";
const device = "device_abcdefghijkl";
const other = "device_otherabcdefg";
const sdp = "v=0\r\ns=synthetic\r\n";
const iceServers = [{ urls: ["turn:turn.example:3478?transport=udp", "turn:turn.example:53?transport=udp"],
  username: "temporary", credential: "temporary-password" }];

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

async function fixture() {
  const db = new DatabaseSync(":memory:");
  databases.push(db);
  for (const name of readdirSync(new URL("../migrations/", import.meta.url)).filter(n => n.endsWith(".sql")).sort()) {
    db.exec(readFileSync(new URL(`../migrations/${name}`, import.meta.url), "utf8"));
  }
  db.prepare("INSERT INTO edges VALUES (?, 'Test', 'public', 'hash', 0, NULL, NULL)").run(edge);
  for (const id of [device, other]) db.prepare("INSERT INTO devices VALUES (?, ?, 'Test', ?, 0, ?, NULL)")
    .run(id, edge, await sha256("secret"), Date.now());
  db.prepare(`INSERT INTO inbound_offers(offer_id,edge_id,call_id,generation,nonce,status,created_at,expires_at,updated_at,media_transport)
    VALUES (?,?,'call_abcdefghijkl',1,'abcdefghijklmnop','offered',?,?,?,'direct_eligible')`)
    .run(offer, edge, Date.now(), Date.now() + 60_000, Date.now());
  const background: Promise<unknown>[] = [];
  const queries: string[] = [];
  const env = {
    DB: {
      batch: vi.fn(async (statements: D1PreparedStatement[]) => {
        db.exec("BEGIN");
        try {
          const result = statements.map(statement => (statement as unknown as { execute(): unknown }).execute());
          db.exec("COMMIT");
          return result;
        } catch (error) { db.exec("ROLLBACK"); throw error; }
      }),
      prepare: (sql: string) => ({ bind: (...values: SQLInputValue[]) => {
        queries.push(sql);
        const params = Object.fromEntries(values.map((value, index) => [String(index + 1), value]));
        return {
          execute: () => ({ results: db.prepare(sql).all(params) }),
          first: async () => db.prepare(sql).get(params) ?? null,
          all: async () => ({ results: db.prepare(sql).all(params) }),
          run: async () => ({ meta: { changes: Number(db.prepare(sql).run(params).changes) } })
        };
      } })
    },
    PUBLIC_ORIGIN: "https://relay", INBOUND_FASTPATH_V2_ENABLED: "true",
    LIVEKIT_URL: "wss://media.test", LIVEKIT_API_KEY: "testkey", LIVEKIT_API_SECRET: "testsecret",
    TURN_KEY_ID: "synthetic-key", TURN_API_TOKEN: "synthetic-token"
  } as unknown as Env;
  const relay = vi.fn(async (url: string, init?: RequestInit): Promise<Response> => {
    if (url.endsWith("/presence")) return Response.json({ connected: true, inboundFastpathV2: true });
    if (url.endsWith("/inbound-ice")) {
      if (JSON.parse(init!.body as string).mode === "warm") return Response.json({ accepted: true }, { status: 202 });
      return Response.json(await issueIceServers(env));
    }
    if (url.endsWith("/inbound-media-relay")) {
      const command = JSON.parse(init!.body as string);
      return Response.json({ v: 1, type: "inbound.answer", commandId: command.commandId,
        offerId: command.offerId, callId: command.callId, claimId: command.claimId, status: "accepted",
        ...(command.type === "inbound.media.sdp" ? { sdpAnswer: sdp } : {}) });
    }
    return Response.json({ accepted: true }, { status: 202 });
  });
  env.EDGE_ROOMS = { idFromName: (id: string) => id, get: vi.fn(() => ({ fetch: relay })) } as unknown as DurableObjectNamespace;
  const request = (path: string, body?: object, id = device) => worker.fetch(new Request(`https://relay${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { Origin: "https://relay", "Content-Type": "application/json", Cookie: `__Host-callpilot-device=${id}.secret` },
    ...(body === undefined ? {} : { body: JSON.stringify(body) })
  }), env, { waitUntil: (promise: Promise<unknown>) => background.push(promise) } as unknown as ExecutionContext);
  const turn = vi.fn(async () => Response.json({ iceServers }));
  vi.stubGlobal("fetch", turn);
  return { db, env, request, relay, background, queries, turn };
}

const claimPath = "/v1/inbound-offers/claim";
const icePath = `/v1/inbound-offers/${offer}/ice`;
const fallbackPath = `/v1/inbound-offers/${offer}/media/livekit`;
const sdpPath = `/v1/inbound-offers/${offer}/media/sdp`;
const endPath = `/v1/inbound-offers/${offer}/media/end`;
const clientTiming = [{ type: "client_timing", v: 1, attempt: "497f6eca-6276-4993-bfeb-53cbbbba6f08", seq: 1,
  marks: { answer_action: -5, audio_engine_failed: 8, direct_control_ready: 7 },
  audioEngine: { running: false, recording: false, playing: false, inputEnabled: true, inputRunning: false,
    outputEnabled: true, outputRunning: false, inputMuted: false, muteMode: 0 } }];
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); databases.splice(0).forEach(db => db.close()); });

describe("direct inbound fastpath V2", () => {
  it.each([fallbackPath, endPath])("forwards bounded client timing on %s without persisting telemetry", async path => {
    const f = await fixture();
    const claimed = await (await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc" })).json() as { claimId: string };
    const response = await f.request(path, { claimId: claimed.claimId, clientTiming });
    expect(response.status).toBe(202);
    const type = path === endPath ? "inbound.media.end" : "inbound.media.livekit";
    const command = f.relay.mock.calls.map(([, init]) => init?.body ? JSON.parse(init.body as string) : null)
      .find(value => value?.type === type);
    expect(command?.clientTiming).toEqual(clientTiming);
    expect(inboundMediaCommandSchema.safeParse(command).success).toBe(true);
    expect(inboundMediaCommandSchema.safeParse({ ...command, type: "inbound.media.sdp",
      direct: { sdpOffer: sdp, iceServers: [] } }).success).toBe(false);
    expect(JSON.stringify(f.db.prepare("SELECT * FROM inbound_offers").get())).not.toContain("audio_engine");
    await Promise.all(f.background);
  });

  it.each([fallbackPath, endPath])("enforces the 4 KiB HTTP boundary on %s", async path => {
    const f = await fixture();
    const claimed = await (await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc" })).json() as { claimId: string };
    const before = f.relay.mock.calls.length;
    const response = await f.request(path, { claimId: claimed.claimId, padding: "x".repeat(4096) });
    expect(response.status).toBe(413);
    expect(f.relay.mock.calls).toHaveLength(before);
    await Promise.all(f.background);
  });

  it.each([
    ["too many batches", Array.from({ length: 9 }, () => clientTiming[0])],
    ["invalid attempt", [{ ...clientTiming[0], attempt: "device-name" }]],
    ["invalid sequence", [{ ...clientTiming[0], seq: 65 }]],
    ["empty marks", [{ ...clientTiming[0], marks: {} }]],
    ["too many marks", [{ ...clientTiming[0], marks: Object.fromEntries([
      "answer_action", "dial_start", "claim_start", "claim_response", "configure_start", "configure_end",
      "fulfill", "did_activate", "activate_return", "connect_start", "connect_return", "room_connected",
      "audio_activated", "mic_enable_start", "mic_enable_return", "downlink_subscribed", "downlink_first_frame"
    ].map(name => [name, 1])) }]],
    ["unknown mark", [{ ...clientTiming[0], marks: { private_name: 1 } }]],
    ["out of range", [{ ...clientTiming[0], marks: { answer_action: -60001 } }]],
    ["engine free text", [{ ...clientTiming[0], audioEngine: { ...clientTiming[0]!.audioEngine, deviceName: "private" } }]],
    ["engine invalid enum", [{ ...clientTiming[0], audioEngine: { ...clientTiming[0]!.audioEngine, muteMode: 3 } }]],
    ["non numeric timing", [{ ...clientTiming[0], marks: { audio_engine_failed: "failed" } }]]
  ])("rejects invalid client timing: %s", async (_name, timing) => {
    const f = await fixture();
    const claimed = await (await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc" })).json() as { claimId: string };
    const before = f.relay.mock.calls.length;
    expect((await f.request(fallbackPath, { claimId: claimed.claimId, clientTiming: timing })).status).toBe(400);
    expect(f.relay.mock.calls).toHaveLength(before);
    await Promise.all(f.background);
  });

  it("pipelines inline SDP before the reservation acknowledgement without changing the winning claim", async () => {
    const f = await fixture();
    const original = f.relay.getMockImplementation()!;
    const reserveStarted = deferred<void>();
    const releaseReserve = deferred<void>();
    const sdpSent = deferred<void>();
    let reserveAcknowledged = false;
    f.relay.mockImplementation(async (url, init) => {
      if (url.endsWith("/inbound-media-relay")) {
        const command = JSON.parse(init!.body as string);
        if (command.type === "inbound.claim") {
          reserveStarted.resolve();
          await releaseReserve.promise;
          reserveAcknowledged = true;
        } else if (command.type === "inbound.media.sdp") {
          expect(reserveAcknowledged).toBe(false);
          sdpSent.resolve();
        }
      }
      return original(url, init);
    });
    let completed = false;
    const pending = f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp })
      .then(response => { completed = true; return response; });
    await reserveStarted.promise;
    await sdpSent.promise;
    expect(completed).toBe(false);
    releaseReserve.resolve();
    expect((await pending).status).toBe(202);
    const commands = f.relay.mock.calls.filter(([url]) => url.endsWith("/inbound-media-relay"))
      .map(([, init]) => JSON.parse(init!.body as string));
    expect(commands.map(command => command.type)).toEqual(["inbound.claim", "inbound.media.sdp"]);
    expect(commands[1].claimId).toBe(commands[0].claimId);
    expect(f.env.EDGE_ROOMS.get).toHaveBeenCalledOnce();
    await Promise.all(f.background);
  });

  it.each(["timeout", "offline", "network", "invalid_answer"])("fails the claim with EDGE_OFFLINE when reservation is %s", async reason => {
    const f = await fixture();
    const original = f.relay.getMockImplementation()!;
    f.relay.mockImplementation(async (url, init) => {
      if (url.endsWith("/inbound-media-relay") && JSON.parse(init!.body as string).type === "inbound.claim") {
        if (reason === "network") throw new Error("synthetic offline");
        if (reason === "invalid_answer") return Response.json({ accepted: true });
        return Response.json({ error: reason === "timeout" ? "TIMEOUT" : "EDGE_OFFLINE" }, { status: reason === "timeout" ? 504 : 503 });
      }
      return original(url, init);
    });
    const response = await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp });
    expect(response.status).toBe(409);
    expect(await response.json()).toMatchObject({ error: { code: "EDGE_OFFLINE" } });
    expect(f.db.prepare("SELECT status,claimed_device_id FROM inbound_offers").get())
      .toMatchObject({ status: "failed", claimed_device_id: device });
    expect(f.turn).toHaveBeenCalledOnce();
    expect((await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc" }, other)).status).toBe(409);
    await Promise.all(f.background);
  });

  it("returns reservation failure without awaiting in-flight SDP and observes a late SDP rejection", async () => {
    const f = await fixture();
    const original = f.relay.getMockImplementation()!;
    const releaseReserve = deferred<void>();
    const sdpSent = deferred<void>();
    const releaseSdp = deferred<void>();
    f.relay.mockImplementation(async (url, init) => {
      if (url.endsWith("/inbound-media-relay")) {
        const command = JSON.parse(init!.body as string);
        if (command.type === "inbound.claim") {
          await releaseReserve.promise;
          return Response.json({ error: "EDGE_OFFLINE" }, { status: 503 });
        }
        if (command.type === "inbound.media.sdp") {
          sdpSent.resolve();
          await releaseSdp.promise;
          return Response.json({ error: "OFFER_UNAVAILABLE" }, { status: 409 });
        }
      }
      return original(url, init);
    });
    const pending = f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp });
    await sdpSent.promise;
    releaseReserve.resolve();
    const response = await pending;
    expect(response.status).toBe(409);
    expect(await response.json()).toMatchObject({ error: { code: "EDGE_OFFLINE" } });
    releaseSdp.resolve();
    await Promise.all(f.background);
    expect(f.db.prepare("SELECT status FROM inbound_offers").get()?.status).toBe("failed");
  });

  it("does not send late SDP when TURN finishes after reservation failure", async () => {
    const f = await fixture();
    const original = f.relay.getMockImplementation()!;
    const turnStarted = deferred<void>();
    const releaseTurn = deferred<Response>();
    f.turn.mockImplementation(() => { turnStarted.resolve(); return releaseTurn.promise; });
    f.relay.mockImplementation(async (url, init) => {
      if (url.endsWith("/inbound-media-relay")) return Response.json({ error: "EDGE_OFFLINE" }, { status: 503 });
      return original(url, init);
    });
    const pending = f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp });
    await turnStarted.promise;
    expect((await pending).status).toBe(409);
    releaseTurn.resolve(Response.json({ iceServers }));
    await Promise.all(f.background);
    expect(f.relay.mock.calls.filter(([url]) => url.endsWith("/inbound-media-relay"))).toHaveLength(1);
  });

  it("allows only the winning claim to end, replays successful end, and rejects late SDP", async () => {
    const f = await fixture();
    const claimed = await (await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc" })).json() as { claimId: string };
    expect((await f.request(endPath, { claimId: claimed.claimId }, other)).status).toBe(409);
    expect((await f.request(endPath, { claimId: "claim_wrongabcdefg" })).status).toBe(409);
    const response = await f.request(endPath, { claimId: claimed.claimId });
    expect(response.status).toBe(202);
    expect(await response.json()).toMatchObject({ accepted: true, claimId: claimed.claimId });
    expect(f.db.prepare("SELECT status FROM inbound_offers").get()?.status).toBe("revoked");
    expect((await f.request(endPath, { claimId: claimed.claimId })).status).toBe(202);
    const ends = f.relay.mock.calls.filter(([url, init]) => url.endsWith("/inbound-media-relay")
      && JSON.parse(init!.body as string).type === "inbound.media.end");
    expect(ends).toHaveLength(1);
    expect(JSON.parse(ends[0]![1]!.body as string)).toMatchObject({ claimId: claimed.claimId,
      session: { roomName: expect.any(String) }, generation: 1, nonce: "abcdefghijklmnop" });
    expect(JSON.parse(ends[0]![1]!.body as string)).not.toHaveProperty("direct");
    expect((await f.request(sdpPath, { claimId: claimed.claimId, sdpOffer: sdp })).status).toBe(409);
    expect((await f.request(fallbackPath, { claimId: claimed.claimId })).status).toBe(409);
    await Promise.all(f.background);
  });

  it("keeps end retryable after a lost answer and rejects revoked device credentials", async () => {
    const f = await fixture();
    const claimed = await (await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc" })).json() as { claimId: string };
    const original = f.relay.getMockImplementation()!;
    let attempts = 0;
    f.relay.mockImplementation(async (url, init) => {
      if (url.endsWith("/inbound-media-relay") && JSON.parse(init!.body as string).type === "inbound.media.end" && ++attempts === 1) {
        return Response.json({ error: "TIMEOUT" }, { status: 504 });
      }
      return original(url, init);
    });
    expect((await f.request(endPath, { claimId: claimed.claimId })).status).toBe(503);
    expect(f.db.prepare("SELECT status FROM inbound_offers").get()?.status).toBe("claimed");
    expect((await f.request(endPath, { claimId: claimed.claimId })).status).toBe(202);
    f.db.prepare("UPDATE devices SET revoked_at=1 WHERE device_id=?").run(device);
    expect((await f.request(endPath, { claimId: claimed.claimId })).status).toBe(401);
    expect(attempts).toBe(2);
    await Promise.all(f.background);
  });

  it("does not end when V2 is disabled", async () => {
    const f = await fixture();
    f.env.INBOUND_FASTPATH_V2_ENABLED = "false";
    expect((await f.request(endPath, { claimId: "claim_abcdefghijkl" })).status).toBe(403);
    expect(f.relay).not.toHaveBeenCalled();
  });

  it("reserves without SDP or TURN and lets only the same winner supply SDP later", async () => {
    const f = await fixture();
    const response = await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc" });
    expect(response.status).toBe(202);
    const claimed = await response.json() as { claimId: string };
    expect(claimed).toMatchObject({ directAttempted: true, sdpPending: true });
    expect(f.turn).not.toHaveBeenCalled();
    const commands = f.relay.mock.calls.filter(([url]) => url.endsWith("/inbound-media-relay"));
    expect(JSON.parse(commands[0]![1]!.body as string)).toMatchObject({ type: "inbound.claim", direct: { deferred: true } });
    expect((await f.request(icePath, {})).status).toBe(200);
    expect((await f.request(icePath, {}, other)).status).toBe(409);
    expect((await f.request(sdpPath, { claimId: claimed.claimId, sdpOffer: sdp }, other)).status).toBe(409);
    const answer = await f.request(sdpPath, { claimId: claimed.claimId, sdpOffer: sdp });
    expect(answer.status).toBe(202);
    expect(await answer.json()).toMatchObject({ claimId: claimed.claimId, sdpPending: false,
      transports: [{ kind: "webrtc", sdpAnswer: sdp }, { kind: "livekit" }] });
    expect((await f.request(sdpPath, { claimId: claimed.claimId, sdpOffer: sdp })).status).toBe(202);
    expect((await f.request(fallbackPath, { claimId: claimed.claimId })).status).toBe(202);
    expect((await f.request(sdpPath, { claimId: claimed.claimId, sdpOffer: sdp })).status).toBe(409);
    expect(f.queries.filter(sql => sql.includes("SET status = 'claimed'"))).toHaveLength(1);
    await Promise.all(f.background);
  });

  it("notifies Edge reservation before waiting for cold TURN with an inline SDP", async () => {
    const f = await fixture();
    let release!: (response: Response) => void;
    f.turn.mockImplementation(() => new Promise<Response>(resolve => { release = resolve; }));
    const pending = f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp });
    await vi.waitFor(() => expect(f.turn).toHaveBeenCalledOnce());
    const commands = f.relay.mock.calls.filter(([url]) => url.endsWith("/inbound-media-relay"));
    expect(commands).toHaveLength(1);
    expect(JSON.parse(commands[0]![1]!.body as string)).toMatchObject({ type: "inbound.claim", direct: { deferred: true } });
    release(Response.json({ iceServers }));
    expect((await pending).status).toBe(202);
    await Promise.all(f.background);
  });

  it("has one winner when inline and deferred SDP phones race", async () => {
    const f = await fixture();
    const responses = await Promise.all([
      f.request(claimPath, { offerId: offer, mediaTransport: "webrtc" }),
      f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp }, other)
    ]);
    expect(responses.map(response => response.status).sort()).toEqual([202, 409]);
    const reservations = f.relay.mock.calls.filter(([url, init]) => url.endsWith("/inbound-media-relay")
      && JSON.parse(init!.body as string).type === "inbound.claim");
    expect(reservations).toHaveLength(1);
    await Promise.all(f.background);
  });

  it("retains the reserved winner when TURN fails and permits same-claim LiveKit", async () => {
    const f = await fixture();
    f.turn.mockResolvedValue(Response.json({ error: "unavailable" }, { status: 503 }));
    const response = await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp });
    expect(response.status).toBe(202);
    const claimed = await response.json() as { claimId: string };
    expect(claimed).toMatchObject({ directAttempted: true, sdpPending: false, transports: [{ kind: "livekit" }] });
    expect(f.db.prepare("SELECT status,media_transport FROM inbound_offers").get())
      .toMatchObject({ status: "claimed", media_transport: "direct_pending" });
    expect((await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc" }, other)).status).toBe(409);
    expect((await f.request(fallbackPath, { claimId: claimed.claimId })).status).toBe(202);
    await Promise.all(f.background);
  });

  it("rejects an inline SDP explicitly rejected by Edge rather than treating it as a timeout", async () => {
    const f = await fixture();
    const original = f.relay.getMockImplementation()!;
    f.relay.mockImplementation(async (url, init) => {
      if (url.endsWith("/inbound-media-relay") && JSON.parse(init!.body as string).type === "inbound.media.sdp") {
        return Response.json({ error: "MEDIA_SDP_REJECTED" }, { status: 409 });
      }
      return original(url, init);
    });
    expect((await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp })).status).toBe(409);
    expect(f.db.prepare("SELECT status FROM inbound_offers").get()?.status).toBe("failed");
    expect(f.turn).toHaveBeenCalledOnce();
    await Promise.all(f.background);
  });

  it("rejects a deferred SDP if the device is revoked while the answer is in flight", async () => {
    const f = await fixture();
    const claimed = await (await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc" })).json() as { claimId: string };
    const original = f.relay.getMockImplementation()!;
    f.relay.mockImplementation(async (url, init) => {
      if (url.endsWith("/inbound-media-relay")) f.db.prepare("UPDATE devices SET revoked_at=1 WHERE device_id=?").run(device);
      return original(url, init);
    });
    expect((await f.request(sdpPath, { claimId: claimed.claimId, sdpOffer: sdp })).status).toBe(409);
    await Promise.all(f.background);
  });

  it("keeps an independent credential-free STUN server and sends the required TURN User-Agent", async () => {
    const f = await fixture();
    f.turn.mockResolvedValue(Response.json({ iceServers: [{ urls: ["stun:stun.example:3478"] }, ...iceServers] }));
    const credentials = await issueIceServers(f.env);
    expect(credentials.iceServers[0]).toEqual({ urls: ["stun:stun.example:3478"] });
    expect(credentials.iceServers[1]).toMatchObject({ username: "temporary", credential: "temporary-password" });
    const init = (f.turn.mock.calls as unknown as [string, RequestInit][])[0]![1];
    expect(new Headers(init.headers).get("User-Agent")).toBe("callpilot-sim-relay/1");
  });

  it("does not advertise the feature without both TURN configuration and Edge capability", async () => {
    const f = await fixture();
    expect(await (await f.request("/v1/device")).json()).toMatchObject({ features: { inboundFastpathV2: true } });
    f.env.TURN_API_TOKEN = "";
    expect(await (await f.request("/v1/device")).json()).toMatchObject({ features: { inboundFastpathV2: false } });
    f.env.TURN_API_TOKEN = "synthetic-token";
    f.relay.mockResolvedValue(Response.json({ connected: true, inboundFastpathV2: false }));
    expect(await (await f.request("/v1/device")).json()).toMatchObject({ features: { inboundFastpathV2: false } });
  });

  it("returns scoped short-lived TURN credentials, filtering port 53 without persisting credentials", async () => {
    const f = await fixture();
    const response = await f.request(icePath, {});
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ iceServers: [{ urls: ["turn:turn.example:3478?transport=udp"] }] });
    expect(f.turn).toHaveBeenCalledOnce();
    expect(JSON.stringify(f.db.prepare("SELECT * FROM inbound_offers").all())).not.toMatch(/temporary-password|synthetic-token|v=0/);
    f.db.prepare("UPDATE inbound_offers SET status='revoked'").run();
    expect((await f.request(icePath, {})).status).toBe(409);
    expect(f.turn).toHaveBeenCalledOnce();
  });

  it.each(["missing", "unavailable"])("returns ICE 503 for TURN %s, retaining legacy claim fallback", async reason => {
    const f = await fixture();
    if (reason === "missing") f.env.TURN_KEY_ID = "";
    else f.turn.mockResolvedValue(Response.json({ error: "unavailable" }, { status: 503 }));
    expect((await f.request(icePath, {})).status).toBe(503);
    const response = await f.request(claimPath, { offerId: offer });
    expect(response.status).toBe(202);
    expect(f.relay.mock.calls.some(([url]) => url.endsWith("/inbound-media-relay"))).toBe(false);
    await Promise.all(f.background);
  });

  it("has exactly one CAS winner and sends one SDP, without any LiveKit management calls", async () => {
    const f = await fixture();
    const responses = await Promise.all([
      f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp }),
      f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp }, other)
    ]);
    expect(responses.map(response => response.status).sort()).toEqual([202, 409]);
    const response = responses.find(value => value.status === 202)!;
    expect(await response.json()).toMatchObject({ callId: "call_abcdefghijkl", directAttempted: true,
      transports: [{ kind: "webrtc", sdpAnswer: sdp }, { kind: "livekit", url: "wss://media.test" }] });
    expect(f.relay.mock.calls.filter(([url]) => url.endsWith("/inbound-media-relay"))).toHaveLength(2);
    expect(f.turn).toHaveBeenCalledOnce();
    expect(f.db.prepare("SELECT media_transport FROM inbound_offers").get()?.media_transport).toBe("direct");
    await Promise.all(f.background);
  });

  it("uses one authenticated CAS and one finalization transaction on the successful direct path", async () => {
    const f = await fixture();
    expect((await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp })).status).toBe(202);
    expect(f.env.DB.batch).toHaveBeenCalledOnce();
    expect(f.queries.filter(sql => /^\s*SELECT\b/.test(sql))).toEqual([]);
    expect(f.queries.filter(sql => sql.includes("SET status = 'claimed'"))).toHaveLength(1);
    expect(f.db.prepare("SELECT action,result FROM audit_events").all()).toEqual([
      { action: "inbound_offer.claim", result: "allowed" }
    ]);
    await Promise.all(f.background);
  });

  it("rejects revoke during SDP relay rather than returning usable media credentials", async () => {
    const f = await fixture();
    const original = f.relay.getMockImplementation()!;
    f.relay.mockImplementation(async (url, init) => {
      if (url.endsWith("/inbound-media-relay")) f.db.prepare("UPDATE inbound_offers SET status='revoked'").run();
      return original(url, init);
    });
    expect((await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp })).status).toBe(409);
    expect(f.db.prepare("SELECT status FROM inbound_offers").get()?.status).toBe("revoked");
    expect(f.db.prepare("SELECT * FROM audit_events").all()).toEqual([]);
    await Promise.all(f.background);
  });

  it("rejects device revocation during SDP relay before returning tokens or recording success", async () => {
    const f = await fixture();
    const original = f.relay.getMockImplementation()!;
    f.relay.mockImplementation(async (url, init) => {
      if (url.endsWith("/inbound-media-relay")) f.db.prepare("UPDATE devices SET revoked_at=1 WHERE device_id=?").run(device);
      return original(url, init);
    });
    expect((await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp })).status).toBe(409);
    expect(f.db.prepare("SELECT * FROM audit_events").all()).toEqual([]);
    await Promise.all(f.background);
  });

  it("retains the winner on SDP timeout and allows only that claim to switch once to LiveKit", async () => {
    const f = await fixture();
    const original = f.relay.getMockImplementation()!;
    f.relay.mockImplementation(async (url, init) => {
      if (url.endsWith("/inbound-media-relay") && JSON.parse(init!.body as string).type === "inbound.media.sdp") {
        return Response.json({ error: "TIMEOUT" }, { status: 504 });
      }
      return original(url, init);
    });
    const response = await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp });
    expect(response.status).toBe(202);
    const claimed = await response.json() as { claimId: string; directAttempted: boolean; transports: { kind: string }[] };
    expect(claimed.directAttempted).toBe(true);
    expect(claimed.transports.map(value => value.kind)).toEqual(["livekit"]);
    expect((await f.request(fallbackPath, { claimId: claimed.claimId }, other)).status).toBe(409);
    expect((await f.request(fallbackPath, { claimId: claimed.claimId })).status).toBe(202);
    expect((await f.request(fallbackPath, { claimId: claimed.claimId })).status).toBe(202);
    const fallbacks = f.relay.mock.calls.filter(([url, init]) => url.endsWith("/inbound-media-relay")
      && JSON.parse(init!.body as string).type === "inbound.media.livekit");
    expect(fallbacks).toHaveLength(1);
    expect(f.db.prepare("SELECT media_transport FROM inbound_offers").get()?.media_transport).toBe("livekit");
    await Promise.all(f.background);
  });

  it("does not revive a revoked or already committed call through fallback", async () => {
    const f = await fixture();
    const response = await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp });
    const claimed = await response.json() as { claimId: string };
    f.relay.mockResolvedValue(Response.json({ status: "rejected", errorCode: "INVALID_STATE" }, { status: 409 }));
    expect((await f.request(fallbackPath, { claimId: claimed.claimId })).status).toBe(409);
    f.db.prepare("UPDATE inbound_offers SET status='revoked'").run();
    const before = f.relay.mock.calls.length;
    expect((await f.request(fallbackPath, { claimId: claimed.claimId })).status).toBe(409);
    expect(f.relay.mock.calls).toHaveLength(before);
    await Promise.all(f.background);
  });

  it("retries a lost fallback answer with the same claim and identities, not a new ownership attempt", async () => {
    const f = await fixture();
    const claimed = await (await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp })).json() as { claimId: string };
    const original = f.relay.getMockImplementation()!;
    let attempts = 0;
    f.relay.mockImplementation(async (url, init) => {
      if (url.endsWith("/inbound-media-relay") && ++attempts === 1) return Response.json({ error: "TIMEOUT" }, { status: 504 });
      return original(url, init);
    });
    const first = await f.request(fallbackPath, { claimId: claimed.claimId });
    expect(first.status).toBe(504);
    expect(await first.json()).toMatchObject({ error: { code: "MEDIA_RELAY_TIMEOUT" } });
    expect(f.db.prepare("SELECT media_transport FROM inbound_offers").get()?.media_transport).toBe("livekit_pending");
    expect((await f.request(fallbackPath, { claimId: claimed.claimId })).status).toBe(202);
    expect((await f.request(fallbackPath, { claimId: claimed.claimId })).status).toBe(202);
    const commands = f.relay.mock.calls.filter(([url, init]) => url.endsWith("/inbound-media-relay")
      && JSON.parse(init!.body as string).type === "inbound.media.livekit").map(([, init]) => JSON.parse(init!.body as string));
    expect(commands).toHaveLength(2);
    expect(commands[0].commandId).not.toBe(commands[1].commandId);
    expect(commands[0].claimId).toBe(claimed.claimId);
    expect(commands[1].claimId).toBe(claimed.claimId);
    expect(commands[0].session.roomName).toBe(commands[1].session.roomName);
    expect(commands[0].session.browserIdentity).toBe(commands[1].session.browserIdentity);
    expect(commands[0].session.edgeIdentity).toBe(commands[1].session.edgeIdentity);
    await Promise.all(f.background);
  });

  it("keeps legacy claim and prepare isolated when V2 is enabled", async () => {
    const f = await fixture();
    f.env.INBOUND_PRECONNECT_ENABLED = "true";
    f.env.INBOUND_CLAIM_FAST_PATH_ENABLED = "true";
    expect((await f.request("/v1/inbound-offers/prepare", { offerId: offer })).status).toBe(409);
    const response = await f.request(claimPath, { offerId: offer });
    expect(response.status).toBe(202);
    expect(Object.keys(await response.json()).sort()).toEqual(["claimId", "expiresAt", "offerId", "roomName", "token", "url"]);
    expect(f.turn).not.toHaveBeenCalled();
    await Promise.all(f.background);
  });

  it("falls back to legacy for non-phone-first offers rather than sending SDP", async () => {
    const f = await fixture();
    f.db.prepare("UPDATE inbound_offers SET media_transport=NULL").run();
    expect((await f.request(icePath, {})).status).toBe(503);
    expect((await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp })).status).toBe(202);
    expect(f.relay.mock.calls.filter(([url]) => url.endsWith("/inbound-media-relay"))).toHaveLength(0);
    expect(f.turn).not.toHaveBeenCalled();
    await Promise.all(f.background);
  });

  it("never sends SDP or allocates TURN before a one-second D1 CAS completes", async () => {
    const f = await fixture();
    const original = f.env.DB.prepare.bind(f.env.DB);
    let casStarted = false;
    vi.spyOn(f.env.DB, "prepare").mockImplementation(sql => {
      const statement = original(sql);
      if (!sql.includes("UPDATE inbound_offers SET status = 'claimed'")) return statement;
      const bind = statement.bind.bind(statement);
      statement.bind = (...values) => {
        const bound = bind(...values), first = bound.first.bind(bound);
        bound.first = async <T = Record<string, unknown>>(column?: string): Promise<T | null> => {
          casStarted = true;
          await new Promise(resolve => setTimeout(resolve, 1000));
          return column === undefined ? first<T>() : first<T>(column);
        };
        return bound;
      };
      return statement;
    });
    const pending = f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp });
    await vi.waitFor(() => expect(casStarted).toBe(true));
    expect(f.turn).not.toHaveBeenCalled();
    expect(f.relay).not.toHaveBeenCalled();
    expect((await pending).status).toBe(202);
    await Promise.all(f.background);
  });

  it.each(["revoked", "wrong_secret"])("does not claim with %s credentials", async reason => {
    const f = await fixture();
    if (reason === "revoked") f.db.prepare("UPDATE devices SET revoked_at=1 WHERE device_id=?").run(device);
    else f.db.prepare("UPDATE devices SET secret_hash='wrong' WHERE device_id=?").run(device);
    expect((await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: sdp })).status).toBe(401);
    expect(f.db.prepare("SELECT status FROM inbound_offers").get()?.status).toBe("offered");
    expect(f.relay).not.toHaveBeenCalled();
    expect(f.turn).not.toHaveBeenCalled();
  });

  it("bounds a TURN fetch that ignores cancellation and does not leak upstream errors", async () => {
    const f = await fixture();
    vi.useFakeTimers();
    f.turn.mockImplementation(() => new Promise<Response>(() => {}));
    const pending = issueIceServers(f.env).catch(error => error);
    await vi.advanceTimersByTimeAsync(TURN_REQUEST_MAX_MS);
    const error = await pending;
    expect(error).toMatchObject({ code: "TURN_UNAVAILABLE", status: 503 });
    expect(String(error)).not.toMatch(/synthetic-token|temporary-password/);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("bounds SDP by UTF-8 bytes, not only character count", async () => {
    const f = await fixture();
    expect((await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: "v=0" + "x".repeat(16 * 1024) })).status).toBe(400);
    expect((await f.request(claimPath, { offerId: offer, mediaTransport: "webrtc", sdpOffer: "v=0" + "\u4e2d".repeat(6000) })).status).toBe(400);
    expect(f.db.prepare("SELECT status FROM inbound_offers").get()?.status).toBe("offered");
  });
});

describe("direct SDP DO relay", () => {
  function command() {
    return { v: 1, type: "inbound.media.sdp", commandId: "command_abcdefghijkl", offerId: offer,
      callId: "call_abcdefghijkl", claimId: "claim_abcdefghijkl", generation: 1, nonce: "abcdefghijklmnop",
      session: { sessionId: "session_abcdefghijkl", roomName: "callpilot_abcdefghijkl", browserIdentity: "web_abcdefghijkl",
        edgeIdentity: "edgepart_abcdefghijkl", livekitUrl: "wss://media.test", token: "synthetic" },
      direct: { sdpOffer: sdp, iceServers: [{ ...iceServers[0], urls: ["turn:turn.example:3478?transport=udp"] }] } };
  }

  function harness(env: Env) {
    vi.stubGlobal("WebSocketRequestResponsePair", class {});
    const socket = { readyState: WebSocket.OPEN as number, send: vi.fn(), close: vi.fn(),
      deserializeAttachment: () => ({ edgeId: edge, connectedAt: 1 }) };
    const state = { getWebSockets: () => [socket], setWebSocketAutoResponse: vi.fn(),
      storage: { get: vi.fn(), put: vi.fn(), setAlarm: vi.fn(), deleteAlarm: vi.fn() }, waitUntil: vi.fn() };
    const room = new EdgeRoom(state as unknown as DurableObjectState, env);
    return { room, socket, state };
  }

  it("keeps claim before SDP through delayed body parsing without waiting for the claim answer", async () => {
    const f = await fixture();
    const { socket, room } = harness(f.env);
    const reserve = { ...command(), type: "inbound.claim", direct: { deferred: true } };
    const media = { ...command(), commandId: "command_sdpabcdefghijk" };
    const bodyStarted = deferred<void>();
    const releaseBody = deferred<string>();
    const sentBoth = deferred<void>();
    socket.send.mockImplementation(() => {
      if (socket.send.mock.calls.length === 2) sentBoth.resolve();
    });
    const reserveRequest = new Request("https://edge-room/inbound-media-relay", { method: "POST", body: "delayed" });
    vi.spyOn(reserveRequest, "text").mockImplementation(() => {
      bodyStarted.resolve();
      return releaseBody.promise;
    });
    const reservePending = room.fetch(reserveRequest);
    await bodyStarted.promise;
    const mediaPending = room.fetch(new Request("https://edge-room/inbound-media-relay", { method: "POST", body: JSON.stringify(media) }));
    expect(socket.send).not.toHaveBeenCalled();
    releaseBody.resolve(JSON.stringify(reserve));
    await sentBoth.promise;
    expect(socket.send.mock.calls.map(([text]) => JSON.parse(text).type)).toEqual(["inbound.claim", "inbound.media.sdp"]);
    for (const message of [reserve, media]) await room.webSocketMessage(socket as unknown as WebSocket, JSON.stringify({
      v: 1, type: "inbound.answer", commandId: message.commandId, offerId: offer, callId: message.callId,
      claimId: message.claimId, status: "accepted", ...(message.type === "inbound.media.sdp" ? { sdpAnswer: sdp } : {})
    }));
    expect((await Promise.all([reservePending, mediaPending])).map(response => response.status)).toEqual([200, 200]);
  });

  it("expires reservation at five seconds, ignores its late answer, and leaves SDP pending until fifteen seconds", async () => {
    const f = await fixture();
    const { socket, room } = harness(f.env);
    vi.useFakeTimers();
    const reserve = { ...command(), type: "inbound.claim", direct: { deferred: true } };
    const media = { ...command(), commandId: "command_sdpabcdefghijk" };
    let reserveResponse: Response | undefined, mediaResponse: Response | undefined;
    const reservePending = room.fetch(new Request("https://edge-room/inbound-media-relay", { method: "POST", body: JSON.stringify(reserve) }))
      .then(response => { reserveResponse = response; });
    const mediaPending = room.fetch(new Request("https://edge-room/inbound-media-relay", { method: "POST", body: JSON.stringify(media) }))
      .then(response => { mediaResponse = response; });
    await vi.waitFor(() => expect(socket.send).toHaveBeenCalledTimes(2));
    await vi.advanceTimersByTimeAsync(5000);
    expect(reserveResponse?.status).toBe(504);
    expect(mediaResponse).toBeUndefined();
    await room.webSocketMessage(socket as unknown as WebSocket, JSON.stringify({ v: 1, type: "inbound.answer",
      commandId: reserve.commandId, offerId: offer, callId: reserve.callId, claimId: reserve.claimId, status: "accepted" }));
    expect(reserveResponse?.status).toBe(504);
    expect(mediaResponse).toBeUndefined();
    await vi.advanceTimersByTimeAsync(10_000);
    await Promise.all([reservePending, mediaPending]);
    expect(mediaResponse?.status).toBe(504);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("warms TURN when the offer arrives, then sends a fenced direct prepare without a session", async () => {
    const f = await fixture();
    const { socket, room, state } = harness(f.env);
    state.storage.get.mockResolvedValue({ connected: true, inboundFastpathV2: true });
    let release!: (response: Response) => void;
    f.turn.mockImplementation(() => new Promise<Response>(resolve => { release = resolve; }));
    const newOffer = "offer_newabcdefghijk";
    const expiry = Date.now() + 30_000;
    await room.webSocketMessage(socket as unknown as WebSocket, JSON.stringify({ v: 1, type: "inbound.offer",
      offerId: newOffer, callId: "call_newabcdefghijk", generation: 2, nonce: "abcdefghijklmnop",
      expiresAtUnixMs: expiry, mode: "phone_first" }));
    await vi.waitFor(() => expect(f.turn).toHaveBeenCalledOnce());
    expect(socket.send).not.toHaveBeenCalled();
    release(Response.json({ iceServers }));
    await Promise.all(state.waitUntil.mock.calls.map(([pending]) => pending));
    const prepare = JSON.parse(socket.send.mock.calls[0]![0]);
    expect(prepare).toMatchObject({ type: "inbound.prepare", offerId: newOffer, generation: 2,
      nonce: "abcdefghijklmnop", expiresAtUnixMs: expiry, mode: "phone_first", direct: { iceServers: expect.any(Array) } });
    expect(prepare).not.toHaveProperty("session");
    expect(prepare).not.toHaveProperty("claimId");
    expect((await room.fetch(new Request("https://edge-room/inbound-ice", { method: "POST",
      body: JSON.stringify({ offerId: newOffer, generation: 2, nonce: "abcdefghijklmnop", expiresAt: expiry, mode: "get" }) }))).status).toBe(200);
    expect(f.turn).toHaveBeenCalledOnce();
  });

  it("accepts reservation answers without SDP but still requires SDP for media.sdp", async () => {
    const f = await fixture();
    const { socket, room } = harness(f.env);
    const message = { ...command(), type: "inbound.claim", direct: { deferred: true } };
    const pending = room.fetch(new Request("https://edge-room/inbound-media-relay", { method: "POST", body: JSON.stringify(message) }));
    await vi.waitFor(() => expect(socket.send).toHaveBeenCalledOnce());
    await room.webSocketMessage(socket as unknown as WebSocket, JSON.stringify({ v: 1, type: "inbound.answer",
      commandId: message.commandId, offerId: offer, callId: message.callId, claimId: message.claimId, status: "accepted" }));
    expect((await pending).status).toBe(200);
  });

  it("requires the matching answer, not an ordinary command acknowledgement", async () => {
    const f = await fixture();
    const { socket, room } = harness(f.env);
    const message = command();
    let settled = false;
    const response = room.fetch(new Request("https://edge-room/inbound-media-relay", { method: "POST", body: JSON.stringify(message) }))
      .then(value => { settled = true; return value; });
    await vi.waitFor(() => expect(socket.send).toHaveBeenCalledOnce());
    await room.webSocketMessage(socket as unknown as WebSocket, JSON.stringify({ v: 1, type: "command.ack",
      commandId: message.commandId, offerId: offer, callId: message.callId, status: "accepted" }));
    expect(settled).toBe(false);
    const answer = { v: 1, type: "inbound.answer", commandId: message.commandId, offerId: offer,
      callId: message.callId, claimId: message.claimId, status: "accepted", sdpAnswer: sdp };
    await room.webSocketMessage(socket as unknown as WebSocket, JSON.stringify({ ...answer, claimId: "claim_otherabcdefg" }));
    expect(settled).toBe(false);
    await room.webSocketMessage(socket as unknown as WebSocket, JSON.stringify(answer));
    expect((await response).status).toBe(200);
  });

  it.each(["timeout", "offline"])("ends a pending SDP on %s and ignores late answers", async reason => {
    const f = await fixture();
    const { socket, room } = harness(f.env);
    vi.useFakeTimers();
    const message = command();
    const pending = room.fetch(new Request("https://edge-room/inbound-media-relay", { method: "POST", body: JSON.stringify(message) }));
    await vi.waitFor(() => expect(socket.send).toHaveBeenCalledOnce());
    if (reason === "timeout") await vi.advanceTimersByTimeAsync(INBOUND_MEDIA_RELAY_MAX_MS);
    else {
      socket.readyState = WebSocket.CLOSED;
      await room.webSocketClose(socket as unknown as WebSocket, 1006, "synthetic disconnect");
    }
    expect((await pending).status).toBe(reason === "timeout" ? 504 : 503);
    await room.webSocketMessage(socket as unknown as WebSocket, JSON.stringify({ v: 1, type: "inbound.answer",
      commandId: message.commandId, offerId: offer, callId: message.callId, claimId: message.claimId,
      status: "accepted", sdpAnswer: sdp }));
    expect(vi.getTimerCount()).toBe(0);
    expect(socket.send).toHaveBeenCalledOnce();
  });

  it("bounds pending SDP requests per Edge and releases all of them on disconnect", async () => {
    const f = await fixture();
    const { socket, room } = harness(f.env);
    const pending: Promise<Response>[] = [];
    for (let index = 0; index < 4; index++) {
      pending.push(room.fetch(new Request("https://edge-room/inbound-media-relay", { method: "POST",
        body: JSON.stringify({ ...command(), commandId: `command_abcdefghijkl${index}` }) })));
    }
    await vi.waitFor(() => expect(socket.send).toHaveBeenCalledTimes(4));
    expect((await room.fetch(new Request("https://edge-room/inbound-media-relay", { method: "POST",
      body: JSON.stringify({ ...command(), commandId: "command_abcdefghijkl5" }) }))).status).toBe(429);
    socket.readyState = WebSocket.CLOSED;
    await room.webSocketClose(socket as unknown as WebSocket, 1006, "synthetic disconnect");
    expect((await Promise.all(pending)).map(response => response.status)).toEqual([503, 503, 503, 503]);
  });

  it("shares the ringing TURN promise with claim, fences it, and invalidates it on revoke", async () => {
    const f = await fixture();
    const { socket, room } = harness(f.env);
    let release!: (response: Response) => void;
    f.turn.mockImplementation(() => new Promise<Response>(resolve => { release = resolve; }));
    const fence = { offerId: offer, generation: 1, nonce: "abcdefghijklmnop", expiresAt: Date.now() + 30_000 };
    const send = (mode: "warm" | "get", extra: object = {}) => room.fetch(new Request("https://edge-room/inbound-ice", {
      method: "POST", body: JSON.stringify({ ...fence, mode, ...extra })
    }));
    expect((await send("warm")).status).toBe(202);
    expect(f.turn).toHaveBeenCalledOnce();
    let complete = false;
    const claim = send("get").then(response => { complete = true; return response; });
    await Promise.resolve();
    expect(complete).toBe(false);
    expect((await send("get", { nonce: "different-nonce-abcdef" })).status).toBe(409);
    release(Response.json({ iceServers }));
    expect((await claim).status).toBe(200);
    expect((await send("get")).status).toBe(200);
    expect(f.turn).toHaveBeenCalledOnce();
    await room.webSocketMessage(socket as unknown as WebSocket, JSON.stringify({ v: 1, type: "inbound.offer.revoke",
      offerId: offer, callId: "call_abcdefghijkl", reason: "OWNER_TIMEOUT" }));
    expect((await send("get")).status).toBe(409);
    expect(f.turn).toHaveBeenCalledOnce();
  });

  it("bounds in-memory TURN warming to eight offers and rejects expired fences", async () => {
    const f = await fixture();
    const { room } = harness(f.env);
    const send = (id: number, expiresAt = Date.now() + 30_000) => room.fetch(new Request("https://edge-room/inbound-ice", {
      method: "POST", body: JSON.stringify({ offerId: `offer_abcdefghijkl${id}`, generation: 1,
        nonce: "abcdefghijklmnop", expiresAt, mode: "warm" })
    }));
    for (let index = 0; index < 8; index++) expect((await send(index)).status).toBe(202);
    expect((await send(8)).status).toBe(429);
    expect((await send(9, Date.now() - 1)).status).toBe(409);
    expect(f.turn).toHaveBeenCalledTimes(8);
  });
});
