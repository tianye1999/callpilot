import { DurableObject } from "cloudflare:workers";
import { dispatchAlertPushes, reserveAlertEvent } from "./alert-push";
import { ContentReadRateLimiter } from "./content-access";

import {
  CONTENT_WIRE_LIMIT_BYTES,
  dataRequestSchema,
  isBeforeRelayDeadline,
  serializedByteLength,
  type ContentResource,
  type DataResponse
} from "./content-sync";
import { edgeMessageSchema, inboundAnswerSchema, inboundIceRequestSchema, inboundMediaCommandSchema } from "./schemas";
import type { z } from "zod";
import { HttpError } from "./http";
import type { EdgePresence, Env } from "./types";
import { randomId } from "./security";
import { issueParticipantToken, participantRequest } from "./livekit";
import { invalidateCandidates, PRECONNECT_CLEANUP_INTERVAL_MS, queueParticipantCleanup, sweepPreconnect } from "./inbound-preconnect";
import { dispatchInboundOfferPushes, errorName } from "./voip-push";
import { issueIceServers, type IceCredentials } from "./turn";

interface SocketAttachment {
  edgeId: string;
  connectedAt: number;
  socketId?: string;
  replaced?: boolean;
}

interface LastSeenTiming {
  d1: boolean;
  d1_ms: number | null;
}

interface PendingRelay {
  resource: ContentResource;
  deadline: number;
  socket: WebSocket;
  timeout: ReturnType<typeof setTimeout>;
  resolve: (response: Response) => void;
}

export const INBOUND_MEDIA_RELAY_MAX_MS = 15_000;
// Reservation is a synchronous control acknowledgement, not media setup.
export const INBOUND_CLAIM_RESERVE_MAX_MS = 5_000;
interface PendingMediaRelay {
  offerId: string;
  callId: string;
  claimId: string;
  expectsSdp: boolean;
  socket: WebSocket;
  deadline: number;
  timeout: ReturnType<typeof setTimeout>;
  resolve: (response: Response) => void;
}
interface WarmInboundIce {
  generation: number;
  nonce: string;
  expiresAt: number;
  pending: Promise<IceCredentials>;
  invalidated: boolean;
}

export class EdgeRoom extends DurableObject<Env> {
  private readonly pendingRelays = new Map<string, PendingRelay>();
  private readonly pendingMediaRelays = new Map<string, PendingMediaRelay>();
  private mediaDispatchTail: Promise<void> = Promise.resolve();
  private readonly warmInboundIce = new Map<string, WarmInboundIce>();
  private readonly contentRateLimiter = new ContentReadRateLimiter();
  private lastSeenAttemptAt: number | undefined;

  constructor(state: DurableObjectState, env: Env) {
    super(state, env);
    this.ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  override async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/connect") return this.acceptConnection(request);
    if (url.pathname === "/presence" && request.method === "GET") return this.presence();
    if (url.pathname === "/command" && request.method === "POST") return this.sendCommand(request);
    if (url.pathname === "/inbound-media-relay" && request.method === "POST") return this.relayInboundMedia(request);
    if (url.pathname === "/inbound-ice" && request.method === "POST") return this.inboundIce(request);
    if (url.pathname === "/preconnect-cleanup" && request.method === "POST") {
      await this.alarm();
      return Response.json({ ok: true });
    }
    if (url.pathname === "/content-relay" && request.method === "POST") return this.relayContent(request);
    return new Response("Not found", { status: 404 });
  }

  private async acceptConnection(request: Request): Promise<Response> {
    if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
      return new Response("WebSocket upgrade required", { status: 426 });
    }
    const edgeId = request.headers.get("X-CallPilot-Edge-Id") ?? "";
    if (!/^edge_[A-Za-z0-9_-]{12,80}$/.test(edgeId)) return new Response("Forbidden", { status: 403 });

    for (const existing of this.ctx.getWebSockets("edge")) {
      const attachment = existing.deserializeAttachment() as SocketAttachment;
      existing.serializeAttachment({ ...attachment, replaced: true });
      this.failRelaysForSocket(existing, "EDGE_OFFLINE", 503);
      this.logSocket("edge.replace", existing, 4001, "Replaced by a newer connection");
      existing.close(4001, "Replaced by a newer connection");
    }
    const pair = new WebSocketPair();
    const client = pair[0];
    const server = pair[1];
    const now = Date.now();
    server.serializeAttachment({ edgeId, connectedAt: now, socketId: crypto.randomUUID().slice(0, 8) } satisfies SocketAttachment);
    this.ctx.acceptWebSocket(server, ["edge"]);
    this.logSocket("edge.connect", server, null, null);
    await this.ctx.storage.put<EdgePresence>("presence", { connected: true, lastSeenAt: now });
    await this.updateLastSeen(edgeId, now);
    return new Response(null, { status: 101, webSocket: client });
  }

  private currentConnection(): WebSocket | undefined {
    const sockets = this.ctx.getWebSockets("edge")
      .filter((socket) => socket.readyState === WebSocket.OPEN)
      .map((socket) => ({ socket, attachment: socket.deserializeAttachment() as SocketAttachment }));
    let current: WebSocket | undefined;
    let connectedAt = 0;
    for (const { socket, attachment } of sockets) {
      // Attachments from before connection fencing have no timestamp.
      const timestamp = attachment?.connectedAt ?? 0;
      if (!attachment?.replaced && (!current || timestamp > connectedAt)) {
        current = socket;
        connectedAt = timestamp;
      }
    }
    for (const { socket, attachment } of sockets) {
      if (socket === current) continue;
      // Persist the fence even if closing leaves the socket reporting OPEN.
      socket.serializeAttachment({ ...attachment, replaced: true });
      this.failRelaysForSocket(socket, "EDGE_OFFLINE", 503);
      this.logSocket("edge.replace", socket, 4001, "Replaced by a newer connection");
      socket.close(4001, "Replaced by a newer connection");
    }
    return current;
  }

  private async presence(): Promise<Response> {
    const stored = await this.ctx.storage.get<EdgePresence>("presence");
    const connected = this.currentConnection() !== undefined;
    return Response.json({ ...(stored ?? {}), connected });
  }

  private async sendCommand(request: Request): Promise<Response> {
    const text = await request.text();
    if (serializedByteLength(text) > CONTENT_WIRE_LIMIT_BYTES) {
      return Response.json({ error: "PAYLOAD_TOO_LARGE" }, { status: 413 });
    }
    const socket = this.currentConnection();
    if (!socket) return Response.json({ error: "EDGE_OFFLINE" }, { status: 503 });
    socket.send(text);
    return Response.json({ accepted: true }, { status: 202 });
  }

  private async relayContent(request: Request): Promise<Response> {
    const text = await request.text();
    if (serializedByteLength(text) > CONTENT_WIRE_LIMIT_BYTES) {
      return Response.json({ error: "PAYLOAD_TOO_LARGE" }, { status: 413 });
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      return Response.json({ error: "INVALID_REQUEST" }, { status: 400 });
    }
    const result = dataRequestSchema.safeParse(parsed);
    if (!result.success || result.data.expiresAtUnixMs <= Date.now()) {
      return Response.json({ error: "INVALID_REQUEST" }, { status: 400 });
    }
    if (this.pendingRelays.has(result.data.requestId)) {
      return Response.json({ error: "INVALID_REQUEST" }, { status: 400 });
    }
    try {
      this.contentRateLimiter.enforce(this.env, result.data.deviceId);
    } catch (caught) {
      if (!(caught instanceof HttpError)) throw caught;
      return Response.json({ error: caught.code }, { status: caught.status, headers: caught.headers });
    }
    const socket = this.currentConnection();
    if (!socket) return Response.json({ error: "EDGE_OFFLINE" }, { status: 503 });

    return new Promise<Response>((resolve) => {
      const requestId = result.data.requestId;
      const timeout = setTimeout(() => {
        const pending = this.pendingRelays.get(requestId);
        if (!pending) return;
        this.pendingRelays.delete(requestId);
        pending.resolve(Response.json({ error: "TIMEOUT" }, { status: 504 }));
      }, Math.max(1, result.data.expiresAtUnixMs - Date.now()));
      this.pendingRelays.set(requestId, {
        resource: result.data.resource,
        deadline: result.data.expiresAtUnixMs,
        socket,
        timeout,
        resolve
      });
      try {
        socket.send(text);
      } catch {
        clearTimeout(timeout);
        this.pendingRelays.delete(requestId);
        resolve(Response.json({ error: "EDGE_OFFLINE" }, { status: 503 }));
      }
    });
  }

  private async relayInboundMedia(request: Request): Promise<Response> {
    const previous = this.mediaDispatchTail;
    let dispatched!: () => void;
    this.mediaDispatchTail = new Promise<void>(resolve => { dispatched = resolve; });
    await previous;
    try {
      // Serialize only parsing and WebSocket send, never the Edge reply.
      return this.dispatchInboundMedia(await request.text());
    } finally {
      dispatched();
    }
  }

  private dispatchInboundMedia(text: string): Response | Promise<Response> {
    if (serializedByteLength(text) > CONTENT_WIRE_LIMIT_BYTES) return Response.json({ error: "PAYLOAD_TOO_LARGE" }, { status: 413 });
    let parsed: unknown;
    try { parsed = JSON.parse(text); } catch { return Response.json({ error: "INVALID_REQUEST" }, { status: 400 }); }
    const command = inboundMediaCommandSchema.safeParse(parsed);
    if (!command.success || this.pendingMediaRelays.has(command.data.commandId)) {
      return Response.json({ error: "INVALID_REQUEST" }, { status: 400 });
    }
    if (this.pendingMediaRelays.size >= 4) return Response.json({ error: "BUSY" }, { status: 429 });
    const socket = this.currentConnection();
    if (!socket) return Response.json({ error: "EDGE_OFFLINE" }, { status: 503 });
    return new Promise<Response>(resolve => {
      const { commandId, offerId, callId, claimId, direct } = command.data;
      const maxMs = command.data.type === "inbound.claim" ? INBOUND_CLAIM_RESERVE_MAX_MS : INBOUND_MEDIA_RELAY_MAX_MS;
      const timeout = setTimeout(() => {
        this.pendingMediaRelays.delete(commandId);
        resolve(Response.json({ error: "TIMEOUT" }, { status: 504 }));
      }, maxMs);
      this.pendingMediaRelays.set(commandId, { offerId, callId, claimId, expectsSdp: direct !== undefined && "sdpOffer" in direct,
        socket, deadline: Date.now() + maxMs, timeout, resolve });
      try { socket.send(text); }
      catch {
        clearTimeout(timeout);
        this.pendingMediaRelays.delete(commandId);
        resolve(Response.json({ error: "EDGE_OFFLINE" }, { status: 503 }));
      }
    });
  }

  private async inboundIce(request: Request): Promise<Response> {
    const input = inboundIceRequestSchema.safeParse(await request.json().catch(() => null));
    // A winner may finish gathering after the ringing deadline. This bounded
    // retention is independent of Edge's shorter media lease and cannot extend it.
    if (!input.success || input.data.expiresAt + (input.data.mode === "get" ? 60_000 : 0) <= Date.now()
      || input.data.expiresAt > Date.now() + 60_000) {
      return Response.json({ error: "OFFER_UNAVAILABLE" }, { status: 409 });
    }
    const { offerId, generation, nonce, expiresAt, mode } = input.data;
    for (const [id, cached] of this.warmInboundIce) if (cached.expiresAt + 60_000 <= Date.now()) this.warmInboundIce.delete(id);
    let cached = this.warmInboundIce.get(offerId);
    if (cached && (cached.invalidated || cached.generation !== generation || cached.nonce !== nonce || cached.expiresAt !== expiresAt)) {
      return Response.json({ error: "OFFER_UNAVAILABLE" }, { status: 409 });
    }
    if (!cached) {
      if (this.warmInboundIce.size >= 8) return Response.json({ error: "BUSY" }, { status: 429 });
      const pending = issueIceServers(this.env);
      cached = { generation, nonce, expiresAt, pending, invalidated: false };
      this.warmInboundIce.set(offerId, cached);
      this.ctx.waitUntil(pending.catch(() => {
        const current = this.warmInboundIce.get(offerId);
        if (current?.pending === pending && !current.invalidated) this.warmInboundIce.delete(offerId);
      }));
    }
    if (mode === "warm") return Response.json({ accepted: true }, { status: 202 });
    try {
      const credentials = await cached.pending;
      if (cached.invalidated) return Response.json({ error: "OFFER_UNAVAILABLE" }, { status: 409 });
      return Response.json(credentials);
    } catch { return Response.json({ error: "TURN_UNAVAILABLE" }, { status: 503 }); }
  }

  override async webSocketMessage(ws: WebSocket, message: ArrayBuffer | string): Promise<void> {
    if (typeof message !== "string") {
      this.failRelaysForSocket(ws, "EDGE_OFFLINE", 503);
      ws.close(1003, "Text JSON required");
      return;
    }
    if (serializedByteLength(message) > CONTENT_WIRE_LIMIT_BYTES) {
      this.failRelaysForSocket(ws, "PAYLOAD_TOO_LARGE", 413);
      ws.close(1009, "Invalid message");
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(message);
    } catch {
      ws.close(1007, "Invalid JSON");
      return;
    }
    const result = edgeMessageSchema.safeParse(parsed);
    if (!result.success) {
      ws.send(JSON.stringify({ v: 1, type: "error", code: "INVALID_MESSAGE" }));
      return;
    }
    if (result.data.type === "data.response") {
      this.acceptRelayResponse(ws, result.data);
      return;
    }
    const attachment = ws.deserializeAttachment() as SocketAttachment;
    if (this.currentConnection() !== ws) return;
    if (result.data.type === "inbound.answer") {
      this.acceptMediaAnswer(ws, result.data);
      return;
    }
    const now = Date.now();
    if (result.data.type === "device.notify") {
      // Identity comes only from the authenticated connection, never the payload.
      if (!attachment?.edgeId || this.currentConnection() !== ws) return;
      const { kind, eventId } = result.data;
      try {
        if (await reserveAlertEvent(this.env, attachment.edgeId, eventId, now)) {
          this.ctx.waitUntil(dispatchAlertPushes(this.env, attachment.edgeId, kind)
            .catch(() => { console.warn("Alert push delivery failed"); }));
        }
      } catch {
        console.warn("Alert push reservation failed");
      }
      return;
    }
    if (result.data.type === "heartbeat") {
      const startedAt = Date.now();
      const newWake = this.lastSeenAttemptAt === undefined;
      let storageMs: number | null = null;
      let lastSeen: LastSeenTiming | undefined;
      const presence: EdgePresence = {
        connected: true,
        lastSeenAt: now,
        modemOnline: result.data.status.modemOnline,
        lineBusy: result.data.status.lineBusy,
        version: result.data.status.version,
        inboundPreconnectV1: result.data.status.inboundPreconnectV1 === true,
        inboundFastpathV2: result.data.status.inboundFastpathV2 === true
      };
      try {
        await Promise.all([
          this.ctx.storage.put("presence", presence).finally(() => { storageMs = Date.now() - startedAt; }),
          this.updateLastSeen(attachment.edgeId, now, true).then(timing => { lastSeen = timing; })
        ]);
      } finally {
        console.log(JSON.stringify({ event: "edge.heartbeat", sid: attachment.socketId ?? null,
          storage_ms: storageMs, d1: lastSeen?.d1 ?? null, d1_ms: lastSeen?.d1_ms ?? null,
          new_wake: newWake, total_ms: Date.now() - startedAt }));
      }
      return;
    }
    if (result.data.type === "command.ack") {
      // Direct SDP is complete only after a correlated inbound.answer.
      if (this.pendingMediaRelays.has(result.data.commandId)) return;
      const errorCode = result.data.status === "rejected" ? (result.data.errorCode ?? null) : null;
      if (result.data.offerId) {
        // Ack for an inbound takeover claim command (#95).
        const offerStatus = result.data.status === "accepted" ? "edge_ready" : "failed";
        await this.env.DB.prepare(
          "UPDATE inbound_offers SET status = ?1, error_code = ?2, updated_at = ?3 WHERE offer_id = ?4 AND edge_id = ?5 AND status = 'claimed' AND (media_transport IS NULL OR media_transport IN ('direct_eligible', 'livekit'))"
        ).bind(offerStatus, errorCode, now, result.data.offerId, attachment.edgeId).run();
        if (offerStatus === "failed") this.ctx.waitUntil(this.alarm());
        return;
      }
      const status = result.data.status === "accepted" ? "ready" : "failed";
      await this.env.DB.prepare(
        "UPDATE calls SET status = ?1, error_code = ?2, updated_at = ?3 WHERE call_id = ?4 AND edge_id = ?5"
      ).bind(status, errorCode, now, result.data.callId, attachment.edgeId).run();
      return;
    }
    if (result.data.type === "inbound.offer") {
      // Offers are insert-once; a duplicate offerId from a reconnect replay is
      // ignored rather than resurrecting a consumed offer.
      const callUUID = crypto.randomUUID();
      const presence = await this.ctx.storage.get<EdgePresence>("presence");
      const preconnect = this.env.INBOUND_FASTPATH_V2_ENABLED !== "true" && this.env.INBOUND_PRECONNECT_ENABLED === "true"
        && presence?.inboundPreconnectV1 === true && result.data.mode === "phone_first";
      const direct = this.env.INBOUND_FASTPATH_V2_ENABLED === "true"
        && presence?.inboundFastpathV2 === true;
      const roomName = randomId("callpilot");
      const edgeIdentity = randomId("edgepart", 12);
      const inserted = await this.env.DB.prepare(
        "INSERT OR IGNORE INTO inbound_offers(offer_id, edge_id, call_id, generation, nonce, status, created_at, expires_at, updated_at, call_uuid, room_name, edge_identity, preconnect_enabled, preconnect_room, media_transport) VALUES (?1, ?2, ?3, ?4, ?5, 'offered', ?6, ?7, ?6, ?8, ?9, ?10, ?11, ?12, ?13)"
      ).bind(
        result.data.offerId, attachment.edgeId, result.data.callId,
        result.data.generation, result.data.nonce, now, result.data.expiresAtUnixMs,
        callUUID, roomName, edgeIdentity, preconnect ? 1 : 0, preconnect ? roomName : null, direct ? "direct_eligible" : null
      ).run();
      if (inserted.meta.changes) {
        if (direct) {
          const offer = result.data;
          this.ctx.waitUntil((async () => {
            const response = await this.inboundIce(new Request("https://edge-room/inbound-ice", {
              method: "POST", body: JSON.stringify({ offerId: offer.offerId, generation: offer.generation,
                nonce: offer.nonce, expiresAt: offer.expiresAtUnixMs, mode: "get" })
            }));
            const cached = this.warmInboundIce.get(offer.offerId);
            if (!response.ok || !cached || cached.invalidated || offer.expiresAtUnixMs <= Date.now()
              || this.currentConnection() !== ws) return;
            const credentials = await response.json() as IceCredentials;
            ws.send(JSON.stringify({ v: 1, type: "inbound.prepare", commandId: randomId("command"),
              offerId: offer.offerId, callId: offer.callId, generation: offer.generation, nonce: offer.nonce,
              expiresAtUnixMs: offer.expiresAtUnixMs, mode: "phone_first", direct: { iceServers: credentials.iceServers } }));
          })().catch(() => { console.warn("Inbound direct warming unavailable"); }));
        }
        const pushedOffer = result.data;
        this.ctx.waitUntil(dispatchInboundOfferPushes(this.env, {
          edgeId: attachment.edgeId,
          offerId: result.data.offerId,
          callUUID,
          expiresAtUnixMs: result.data.expiresAtUnixMs
        }, fetch, direct ? () => {
          if (this.currentConnection() !== ws || pushedOffer.expiresAtUnixMs <= Date.now()) return;
          ws.send(JSON.stringify({ v: 1, type: "inbound.offer.pushed", offerId: pushedOffer.offerId,
            callId: pushedOffer.callId, generation: pushedOffer.generation, nonce: pushedOffer.nonce }));
        } : undefined).catch((error: unknown) => { console.warn("VoIP push dispatch failed", errorName(error)); }));
        if (preconnect) {
          await this.ctx.storage.put("preconnectEdge", attachment.edgeId);
          await this.ctx.storage.setAlarm(now + 1000);
          const offer = result.data;
          await queueParticipantCleanup(this.env, { edge_id: attachment.edgeId, offer_id: offer.offerId,
            expires_at: offer.expiresAtUnixMs }, roomName, edgeIdentity);
          const token = await issueParticipantToken(this.env, roomName, edgeIdentity,
            Math.max(1, Math.ceil((offer.expiresAtUnixMs - Date.now()) / 1000)));
          // The Edge validates this fence again before allocating any transport.
          ws.send(JSON.stringify({
            v: 1, type: "inbound.prepare", commandId: randomId("command"),
            offerId: offer.offerId, callId: offer.callId, generation: offer.generation,
            nonce: offer.nonce, expiresAtUnixMs: offer.expiresAtUnixMs, mode: "phone_first",
            session: { sessionId: randomId("session"), roomName, edgeIdentity,
              browserIdentity: randomId("web", 12), livekitUrl: this.env.LIVEKIT_URL, token }
          }));
        }
      }
      return;
    }
    if (result.data.type === "inbound.offer.revoke") {
      const room = await this.env.DB.prepare(
        "SELECT room_name FROM inbound_offers WHERE offer_id = ?1 AND edge_id = ?2"
      ).bind(result.data.offerId, attachment.edgeId).first<{ room_name: string }>();
      const cached = this.warmInboundIce.get(result.data.offerId);
      if (cached) cached.invalidated = true;
      await this.env.DB.prepare(
        "UPDATE inbound_offers SET status = 'revoked', error_code = ?1, updated_at = ?2 WHERE offer_id = ?3 AND edge_id = ?4 AND status IN ('offered', 'claimed', 'edge_ready')"
      ).bind(result.data.reason, now, result.data.offerId, attachment.edgeId).run();
      await invalidateCandidates(this.env, result.data.offerId);
      if (room?.room_name && this.env.LIVEKIT_API_KEY && this.env.LIVEKIT_API_SECRET) {
        await this.ctx.storage.put(`ended-room/${room.room_name}`, room.room_name);
      }
      this.ctx.waitUntil(this.alarm());
      return;
    }
    await this.env.DB.prepare(
      "UPDATE calls SET status = ?1, updated_at = ?2 WHERE call_id = ?3 AND edge_id = ?4"
    ).bind(result.data.status, now, result.data.callId, attachment.edgeId).run();
  }

  override async webSocketClose(ws: WebSocket, code: number, reason: string): Promise<void> {
    // These are local status sentinels, never valid codes in an outgoing close frame.
    const replyCode = [1005, 1006, 1015].includes(code) ? 1000 : code;
    this.logSocket("edge.close", ws, code, reason, { reply_code: replyCode });
    this.failRelaysForSocket(ws, "EDGE_OFFLINE", 503);
    const attachment = ws.deserializeAttachment() as SocketAttachment | null;
    const current = this.currentConnection();
    if (attachment?.replaced || (current && current !== ws)) { ws.close(replyCode, reason); return; }
    await this.ctx.storage.put<EdgePresence>("presence", {
      ...(await this.ctx.storage.get<EdgePresence>("presence")),
      connected: false,
      lastSeenAt: Date.now()
    });
    ws.close(replyCode, reason);
    if (attachment) {
      await this.disconnectPreconnect(attachment.edgeId);
      await this.updateLastSeen(attachment.edgeId, Date.now());
    }
  }

  override async webSocketError(ws: WebSocket): Promise<void> {
    this.logSocket("edge.error", ws, 1011, "WebSocket error");
    this.failRelaysForSocket(ws, "EDGE_OFFLINE", 503);
    ws.close(1011, "WebSocket error");
    const attachment = ws.deserializeAttachment() as SocketAttachment | null;
    if (attachment && !attachment.replaced) await this.disconnectPreconnect(attachment.edgeId);
  }

  private logSocket(event: string, ws: WebSocket, code: number | null, reason: string | null,
    extra: Record<string, number> = {}): void {
    const attachment = ws.deserializeAttachment() as SocketAttachment | null;
    console.log(JSON.stringify({ event, sid: attachment?.socketId ?? null, code, reason,
      alive_s: attachment?.connectedAt === undefined ? null : (Date.now() - attachment.connectedAt) / 1000,
      ...extra }));
  }

  private async disconnectPreconnect(edgeId: string) {
    if (await this.ctx.storage.get("preconnectEdge") !== edgeId) return;
    await this.env.DB.prepare(`UPDATE inbound_offers SET status = 'revoked', updated_at = ?1
      WHERE edge_id = ?2 AND preconnect_enabled = 1 AND status IN ('offered', 'claimed', 'edge_ready')`)
      .bind(Date.now(), edgeId).run();
    await this.alarm();
  }

  override async alarm(): Promise<void> {
    const endedRooms = await this.ctx.storage.list<string>({ prefix: "ended-room/", limit: 16 });
    let roomDeletePending = endedRooms.size >= 16;
    if (endedRooms.size) await this.ctx.storage.setAlarm(Date.now() + PRECONNECT_CLEANUP_INTERVAL_MS);
    await Promise.all([...endedRooms].map(async ([key, room]) => {
      try {
        await participantRequest(this.env, "DeleteRoom", room, "");
        await this.ctx.storage.delete(key);
      } catch {
        roomDeletePending = true;
        // Keep the scoped deletion intent across DO eviction and retry alarms.
        console.warn("Ended inbound room cleanup pending");
      }
    }));
    const edgeId = await this.ctx.storage.get<string>("preconnectEdge");
    if (!edgeId || typeof edgeId !== "string") {
      if (!roomDeletePending) await this.ctx.storage.deleteAlarm();
      return;
    }
    // Arm before I/O so a failed D1/admin request is retried after hibernation.
    await this.ctx.storage.setAlarm(Date.now() + PRECONNECT_CLEANUP_INTERVAL_MS);
    const interval = await sweepPreconnect(this.env, edgeId);
    if (interval === null && !roomDeletePending) await this.ctx.storage.deleteAlarm();
    else await this.ctx.storage.setAlarm(Date.now() + (interval ?? PRECONNECT_CLEANUP_INTERVAL_MS));
  }

  private async updateLastSeen(edgeId: string, now: number, heartbeat = false): Promise<LastSeenTiming> {
    const cutoff = now - 5 * 60_000;
    if (heartbeat && this.lastSeenAttemptAt !== undefined && this.lastSeenAttemptAt > cutoff) return { d1: false, d1_ms: null };
    const newWake = this.lastSeenAttemptAt === undefined;
    // Reserve before awaiting D1, including failures, to avoid overlapping retries.
    this.lastSeenAttemptAt = now;
    const startedAt = Date.now();
    let ok = false;
    let durationMs: number;
    try {
      const sql = "UPDATE edges SET last_seen_at = ?1 WHERE edge_id = ?2";
      await (heartbeat
        ? this.env.DB.prepare(`${sql} AND (last_seen_at IS NULL OR last_seen_at <= ?3)`)
          .bind(now, edgeId, cutoff)
        : this.env.DB.prepare(sql).bind(now, edgeId)).run();
      ok = true;
    } catch {
      console.warn("Edge last-seen update failed");
    } finally {
      durationMs = Date.now() - startedAt;
      console.log(JSON.stringify({ event: "edge.last_seen", edge_id: edgeId, at: startedAt, d1_ms: durationMs,
        new_wake: newWake, heartbeat, ok }));
    }
    return { d1: true, d1_ms: durationMs };
  }

  private acceptRelayResponse(ws: WebSocket, response: DataResponse): void {
    const pending = this.pendingRelays.get(response.requestId);
    if (
      !pending
      || pending.socket !== ws
      || pending.resource !== response.resource
      || !isBeforeRelayDeadline(pending.deadline, Date.now())
    ) return;
    clearTimeout(pending.timeout);
    this.pendingRelays.delete(response.requestId);
    pending.resolve(Response.json(response));
  }

  private failRelaysForSocket(ws: WebSocket, code: string, status: number): void {
    for (const [commandId, pending] of this.pendingMediaRelays) {
      if (pending.socket !== ws) continue;
      clearTimeout(pending.timeout);
      this.pendingMediaRelays.delete(commandId);
      pending.resolve(Response.json({ error: code }, { status }));
    }
    for (const [requestId, pending] of this.pendingRelays) {
      if (pending.socket !== ws) continue;
      clearTimeout(pending.timeout);
      this.pendingRelays.delete(requestId);
      pending.resolve(Response.json({ error: code }, { status }));
    }
  }

  private acceptMediaAnswer(ws: WebSocket, answer: z.infer<typeof inboundAnswerSchema>): void {
    const pending = this.pendingMediaRelays.get(answer.commandId);
    if (!pending || pending.socket !== ws || pending.deadline <= Date.now()
        || pending.offerId !== answer.offerId || pending.callId !== answer.callId || pending.claimId !== answer.claimId
        || (answer.status === "accepted" && pending.expectsSdp && !answer.sdpAnswer)) return;
    clearTimeout(pending.timeout);
    this.pendingMediaRelays.delete(answer.commandId);
    pending.resolve(Response.json(answer, { status: answer.status === "accepted" ? 200 : 409 }));
  }
}
