import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("cloudflare:workers", () => ({
  DurableObject: class {
    constructor(protected ctx: DurableObjectState, protected env: Env) {}
  }
}));

import { EdgeRoom } from "../src/edge-room";
import * as alertPush from "../src/alert-push";
import type { Env } from "../src/types";

const edgeId = "edge_abcdefghijkl";

function edgeSocket(connectedAt?: number, replaced?: boolean) {
  let attachment = { edgeId, ...(connectedAt === undefined ? {} : { connectedAt }), replaced };
  return {
    readyState: WebSocket.OPEN as number,
    deserializeAttachment: () => structuredClone(attachment),
    serializeAttachment: vi.fn((value: typeof attachment) => { attachment = structuredClone(value); }),
    send: vi.fn(),
    // A stale socket can remain OPEN even after a close was requested.
    close: vi.fn(() => { expect(attachment.replaced).toBe(true); })
  };
}

function edgeRoom(sockets: ReturnType<typeof edgeSocket>[], vars: Partial<Env> = {}) {
  vi.stubGlobal("WebSocketRequestResponsePair", class {});
  const run = vi.fn().mockResolvedValue({});
  const bind = vi.fn().mockReturnValue({ run });
  const prepare = vi.fn().mockReturnValue({ bind });
  const state = {
    getWebSockets: vi.fn(() => sockets),
    acceptWebSocket: vi.fn((socket: ReturnType<typeof edgeSocket>) => { sockets.push(socket); }),
    setWebSocketAutoResponse: vi.fn(),
    storage: { get: vi.fn().mockResolvedValue({ connected: true }), put: vi.fn().mockResolvedValue(undefined) }
  };
  const room = new EdgeRoom(state as unknown as DurableObjectState, { DB: { prepare }, ...vars } as unknown as Env);
  return { room, state, prepare, bind, run };
}

function command() {
  return new Request("https://edge-room/command", {
    method: "POST", body: JSON.stringify({ v: 1, type: "session.start" })
  });
}

function contentRequest(requestId = "request_abcdefghijkl", deviceId = "device_abcdefghijkl") {
  return new Request("https://edge-room/content-relay", {
    method: "POST",
    body: JSON.stringify({
      v: 1, type: "data.request", requestId,
      deviceId, resource: "messages.list",
      params: { limit: 25, cursor: null },
      issuedAtUnixMs: Date.now(), expiresAtUnixMs: Date.now() + 10_000
    })
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("EdgeRoom current connection", () => {
  it("marks existing attachments before closing and timestamps the accepted connection", async () => {
    const now = 123456;
    vi.spyOn(Date, "now").mockReturnValue(now);
    const old = edgeSocket(now);
    const legacy = edgeSocket();
    const latest = edgeSocket();
    const client = {};
    const { room, state } = edgeRoom([old, legacy]);
    vi.stubGlobal("WebSocketPair", class {
      0 = client;
      1 = latest;
    });
    // Node's Response does not support the Workers-only 101 upgrade response.
    const NativeResponse = Response;
    const upgrade = vi.spyOn(globalThis, "Response").mockImplementationOnce(function () { return new NativeResponse(); });
    await room.fetch(new Request("https://edge-room/connect", {
      headers: { Upgrade: "websocket", "X-CallPilot-Edge-Id": edgeId }
    }));
    expect(upgrade).toHaveBeenCalledWith(null, { status: 101, webSocket: client });
    expect(old.deserializeAttachment()).toMatchObject({ connectedAt: now, replaced: true });
    expect(legacy.deserializeAttachment()).toMatchObject({ edgeId, replaced: true });
    expect(old.close).toHaveBeenCalledWith(4001, "Replaced by a newer connection");
    expect(legacy.close).toHaveBeenCalledWith(4001, "Replaced by a newer connection");
    expect(latest.deserializeAttachment()).toEqual({ edgeId, connectedAt: now });
    expect(state.acceptWebSocket).toHaveBeenCalledWith(latest, ["edge"]);
    upgrade.mockRestore();

    // Simulate a wake-up: only attachments, not instance fields, survive.
    const resumed = edgeRoom([old, legacy, latest]).room;
    expect((await resumed.fetch(command())).status).toBe(202);
    expect(latest.send).toHaveBeenCalledOnce();
    expect(old.send).not.toHaveBeenCalled();
    expect(legacy.send).not.toHaveBeenCalled();
  });

  it("selects the current connection after awaiting the command body", async () => {
    const old = edgeSocket(100);
    const latest = edgeSocket(200);
    const sockets = [old];
    const { room } = edgeRoom(sockets);
    const request = command();
    vi.spyOn(request, "text").mockImplementation(async () => {
      sockets.push(latest);
      return "command-body";
    });
    expect((await room.fetch(request)).status).toBe(202);
    expect(latest.send).toHaveBeenCalledWith("command-body");
    expect(old.send).not.toHaveBeenCalled();
  });

  it.each([false, true])("sends to the newest OPEN socket regardless of enumeration order (%s)", async (newestFirst) => {
    const old = edgeSocket(100);
    const latest = edgeSocket(200);
    const { room } = edgeRoom(newestFirst ? [latest, old] : [old, latest]);

    expect((await room.fetch(command())).status).toBe(202);
    expect(latest.send).toHaveBeenCalledWith(JSON.stringify({ v: 1, type: "session.start" }));
    expect(old.send).not.toHaveBeenCalled();
    expect(old.deserializeAttachment()).toMatchObject({ edgeId, connectedAt: 100, replaced: true });
    expect(old.close).toHaveBeenCalledWith(4001, "Replaced by a newer connection");
    expect(latest.close).not.toHaveBeenCalled();
    expect(await (await room.fetch(new Request("https://edge-room/presence"))).json()).toMatchObject({ connected: true });
  });

  it("presence alone fences an old OPEN socket before a command", async () => {
    const old = edgeSocket(100);
    const latest = edgeSocket(200);
    const { room } = edgeRoom([old, latest]);
    expect(await (await room.fetch(new Request("https://edge-room/presence"))).json()).toMatchObject({ connected: true });
    expect(old.close).toHaveBeenCalledWith(4001, "Replaced by a newer connection");
    expect((await room.fetch(command())).status).toBe(202);
    expect(latest.send).toHaveBeenCalledOnce();
    expect(old.send).not.toHaveBeenCalled();
  });

  it("treats a legacy attachment without connectedAt as zero", async () => {
    const legacy = edgeSocket();
    const latest = edgeSocket(1);
    const { room } = edgeRoom([legacy, latest]);
    expect((await room.fetch(command())).status).toBe(202);
    expect(latest.send).toHaveBeenCalledOnce();
    expect(legacy.send).not.toHaveBeenCalled();
    expect(legacy.deserializeAttachment()).toMatchObject({ edgeId, replaced: true });
    expect(legacy.close).toHaveBeenCalledWith(4001, "Replaced by a newer connection");
  });

  it("still uses a sole legacy connection", async () => {
    const legacy = edgeSocket();
    const { room } = edgeRoom([legacy]);
    expect((await room.fetch(command())).status).toBe(202);
    expect(legacy.send).toHaveBeenCalledOnce();
    expect(legacy.close).not.toHaveBeenCalled();
  });

  it("never selects a replaced OPEN socket even with a larger timestamp", async () => {
    const old = edgeSocket(300, true);
    const latest = edgeSocket(200);
    const { room } = edgeRoom([old, latest]);
    expect((await room.fetch(command())).status).toBe(202);
    expect(latest.send).toHaveBeenCalledOnce();
    expect(old.send).not.toHaveBeenCalled();

    latest.readyState = WebSocket.CLOSED;
    expect(await (await room.fetch(new Request("https://edge-room/presence"))).json()).toMatchObject({ connected: false });
    expect((await room.fetch(command())).status).toBe(503);
    expect((await room.fetch(contentRequest())).status).toBe(503);
    expect(old.send).not.toHaveBeenCalled();
  });

  it("relays content to the newest socket and fails the old socket's pending relay", async () => {
    vi.useFakeTimers();
    const old = edgeSocket(100);
    const latest = edgeSocket(200);
    const sockets = [old];
    const { room } = edgeRoom(sockets);
    const pendingOld = room.fetch(contentRequest());
    await vi.waitFor(() => expect(old.send).toHaveBeenCalledOnce());

    sockets.push(latest);
    const request = contentRequest();
    const body = await request.json() as Record<string, unknown>;
    const pendingLatest = room.fetch(new Request(request.url, {
      method: "POST", body: JSON.stringify({ ...body, requestId: "request_newestabcdef" })
    }));
    await vi.waitFor(() => expect(latest.send).toHaveBeenCalledOnce());
    expect((await pendingOld).status).toBe(503);
    expect(await (await pendingOld).json()).toEqual({ error: "EDGE_OFFLINE" });
    expect(old.close).toHaveBeenCalledWith(4001, "Replaced by a newer connection");

    await room.webSocketMessage(latest as unknown as WebSocket, JSON.stringify({
      v: 1, type: "data.response", requestId: "request_newestabcdef",
      resource: "messages.list", status: "error", error: { code: "NOT_FOUND" }
    }));
    expect((await pendingLatest).status).toBe(200);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("ignores device.notify from an old socket that still reports OPEN", async () => {
    const old = edgeSocket(100);
    const latest = edgeSocket(200);
    const { room } = edgeRoom([old, latest]);
    const reserve = vi.spyOn(alertPush, "reserveAlertEvent").mockResolvedValue(false);
    const event = JSON.stringify({
      v: 1, type: "device.notify", kind: "sms", eventId: `event_${"a".repeat(64)}`
    });
    await room.webSocketMessage(old as unknown as WebSocket, event);
    expect(old.close).toHaveBeenCalledWith(4001, "Replaced by a newer connection");
    expect(reserve).not.toHaveBeenCalled();
    await room.webSocketMessage(latest as unknown as WebSocket, event);
    expect(reserve).toHaveBeenCalledWith(expect.anything(), edgeId, `event_${"a".repeat(64)}`, expect.any(Number));
  });
});

const heartbeat = JSON.stringify({
  v: 1, type: "heartbeat", occurredAt: "2026-11-02T00:00:00.000Z",
  status: { modemOnline: true, lineBusy: false, version: "test" }
});

describe("EdgeRoom last-seen bookkeeping", () => {
  it("still returns a 101 upgrade when D1 rejects the update", async () => {
    const { room, run } = edgeRoom([]);
    run.mockRejectedValue(new Error("D1 write limit"));
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.stubGlobal("WebSocketPair", class { 0 = {}; 1 = edgeSocket(); });
    const NativeResponse = Response;
    vi.spyOn(globalThis, "Response").mockImplementationOnce(function (_body, init) {
      const response = new NativeResponse();
      Object.defineProperty(response, "status", { value: init?.status });
      return response;
    });
    const response = await room.fetch(new Request("https://edge-room/connect", {
      headers: { Upgrade: "websocket", "X-CallPilot-Edge-Id": edgeId }
    }));
    expect(response.status).toBe(101);
  });

  it("throttles heartbeat D1 updates but persists every presence", async () => {
    vi.useFakeTimers();
    const start = Date.now();
    const socket = edgeSocket();
    const { room, run, prepare, bind, state } = edgeRoom([socket]);
    for (const elapsed of [0, 15_000, 299_999]) {
      vi.setSystemTime(start + elapsed);
      await room.webSocketMessage(socket as unknown as WebSocket, heartbeat);
    }
    expect(run).toHaveBeenCalledOnce();
    expect(state.storage.put).toHaveBeenCalledTimes(3);
    vi.setSystemTime(start + 300_000);
    await room.webSocketMessage(socket as unknown as WebSocket, heartbeat);
    expect(run).toHaveBeenCalledTimes(2);
    expect(prepare).toHaveBeenLastCalledWith(
      "UPDATE edges SET last_seen_at = ?1 WHERE edge_id = ?2 AND (last_seen_at IS NULL OR last_seen_at <= ?3)"
    );
    expect(bind).toHaveBeenLastCalledWith(start + 300_000, edgeId, start);
  });

  it("keeps heartbeat and close nonfatal when D1 fails", async () => {
    const socket = edgeSocket();
    socket.close.mockImplementation(() => {});
    const { room, run, state } = edgeRoom([socket]);
    run.mockRejectedValue(new Error("D1 write limit"));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    await expect(room.webSocketMessage(socket as unknown as WebSocket, heartbeat)).resolves.toBeUndefined();
    await expect(room.webSocketClose(socket as unknown as WebSocket, 1000, "Closed")).resolves.toBeUndefined();
    expect(warn).toHaveBeenCalledTimes(2);
    expect(state.storage.put).toHaveBeenCalledTimes(2);
  });
});

describe("EdgeRoom content rate limits", () => {
  it.each(["device", "edge"])("rejects the exhausted %s bucket before forwarding and recovers after the window", async (scope) => {
    vi.useFakeTimers();
    const start = Date.now();
    const clock = vi.spyOn(Date, "now").mockReturnValue(start);
    const socket = edgeSocket();
    const { room, prepare } = edgeRoom([socket], {
      CONTENT_READ_DEVICE_LIMIT: scope === "device" ? "1" : "10",
      CONTENT_READ_EDGE_LIMIT: scope === "edge" ? "1" : "10"
    });
    const send = async (requestId: string, deviceId?: string) => {
      const pending = room.fetch(contentRequest(requestId, deviceId));
      await vi.waitFor(() => expect(socket.send).toHaveBeenCalledWith(expect.stringContaining(requestId)));
      await room.webSocketMessage(socket as unknown as WebSocket, JSON.stringify({
        v: 1, type: "data.response", requestId, resource: "messages.list",
        status: "error", error: { code: "NOT_FOUND" }
      }));
      expect((await pending).status).toBe(200);
    };
    await send("request_firstabcdefg");
    const otherDevice = scope === "edge" ? "device_otherabcdefg" : undefined;
    for (const [elapsed, retryAfter] of [[1, "60"], [59_999, "1"]] as const) {
      clock.mockReturnValue(start + elapsed);
      const limited = await room.fetch(contentRequest("request_limitedabcde", otherDevice));
      expect(limited.status).toBe(429);
      expect(await limited.json()).toEqual({ error: "RATE_LIMITED" });
      expect(limited.headers.get("Retry-After")).toBe(retryAfter);
      expect(socket.send).toHaveBeenCalledOnce();
    }
    clock.mockReturnValue(start + 60_000);
    await send("request_recoveredabc", otherDevice);
    expect(socket.send).toHaveBeenCalledTimes(2);
    expect(prepare).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });
});
