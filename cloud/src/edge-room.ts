import { DurableObject } from "cloudflare:workers";

import { edgeMessageSchema } from "./schemas";
import type { EdgePresence, Env } from "./types";

interface SocketAttachment {
  edgeId: string;
}

export class EdgeRoom extends DurableObject<Env> {
  constructor(state: DurableObjectState, env: Env) {
    super(state, env);
    this.ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  override async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/connect") return this.acceptConnection(request);
    if (url.pathname === "/presence" && request.method === "GET") return this.presence();
    if (url.pathname === "/command" && request.method === "POST") return this.sendCommand(request);
    return new Response("Not found", { status: 404 });
  }

  private async acceptConnection(request: Request): Promise<Response> {
    if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
      return new Response("WebSocket upgrade required", { status: 426 });
    }
    const edgeId = request.headers.get("X-CallPilot-Edge-Id") ?? "";
    if (!/^edge_[A-Za-z0-9_-]{12,80}$/.test(edgeId)) return new Response("Forbidden", { status: 403 });

    for (const existing of this.ctx.getWebSockets("edge")) existing.close(4001, "Replaced by a newer connection");
    const pair = new WebSocketPair();
    const client = pair[0];
    const server = pair[1];
    server.serializeAttachment({ edgeId } satisfies SocketAttachment);
    this.ctx.acceptWebSocket(server, ["edge"]);
    const now = Date.now();
    await this.ctx.storage.put<EdgePresence>("presence", { connected: true, lastSeenAt: now });
    await this.env.DB.prepare("UPDATE edges SET last_seen_at = ?1 WHERE edge_id = ?2")
      .bind(now, edgeId).run();
    return new Response(null, { status: 101, webSocket: client });
  }

  private async presence(): Promise<Response> {
    const stored = await this.ctx.storage.get<EdgePresence>("presence");
    const connected = this.ctx.getWebSockets("edge").some((socket) => socket.readyState === WebSocket.OPEN);
    return Response.json({ ...(stored ?? {}), connected });
  }

  private async sendCommand(request: Request): Promise<Response> {
    const sockets = this.ctx.getWebSockets("edge").filter((socket) => socket.readyState === WebSocket.OPEN);
    if (sockets.length !== 1) return Response.json({ error: "EDGE_OFFLINE" }, { status: 503 });
    const text = await request.text();
    if (text.length > 16 * 1024) return Response.json({ error: "PAYLOAD_TOO_LARGE" }, { status: 413 });
    sockets[0]?.send(text);
    return Response.json({ accepted: true }, { status: 202 });
  }

  override async webSocketMessage(ws: WebSocket, message: ArrayBuffer | string): Promise<void> {
    if (typeof message !== "string" || message.length > 16 * 1024) {
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
    const attachment = ws.deserializeAttachment() as SocketAttachment;
    const now = Date.now();
    if (result.data.type === "heartbeat") {
      const presence: EdgePresence = {
        connected: true,
        lastSeenAt: now,
        modemOnline: result.data.status.modemOnline,
        lineBusy: result.data.status.lineBusy,
        version: result.data.status.version
      };
      await Promise.all([
        this.ctx.storage.put("presence", presence),
        this.env.DB.prepare("UPDATE edges SET last_seen_at = ?1 WHERE edge_id = ?2")
          .bind(now, attachment.edgeId).run()
      ]);
      return;
    }
    if (result.data.type === "command.ack") {
      const errorCode = result.data.status === "rejected" ? (result.data.errorCode ?? null) : null;
      if (result.data.offerId) {
        // Ack for an inbound takeover claim command (#95).
        const offerStatus = result.data.status === "accepted" ? "edge_ready" : "failed";
        await this.env.DB.prepare(
          "UPDATE inbound_offers SET status = ?1, error_code = ?2, updated_at = ?3 WHERE offer_id = ?4 AND edge_id = ?5 AND status = 'claimed'"
        ).bind(offerStatus, errorCode, now, result.data.offerId, attachment.edgeId).run();
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
      await this.env.DB.prepare(
        "INSERT OR IGNORE INTO inbound_offers(offer_id, edge_id, call_id, generation, nonce, status, created_at, expires_at, updated_at) VALUES (?1, ?2, ?3, ?4, ?5, 'offered', ?6, ?7, ?6)"
      ).bind(
        result.data.offerId, attachment.edgeId, result.data.callId,
        result.data.generation, result.data.nonce, now, result.data.expiresAtUnixMs
      ).run();
      return;
    }
    if (result.data.type === "inbound.offer.revoke") {
      await this.env.DB.prepare(
        "UPDATE inbound_offers SET status = 'revoked', error_code = ?1, updated_at = ?2 WHERE offer_id = ?3 AND edge_id = ?4 AND status IN ('offered', 'claimed', 'edge_ready')"
      ).bind(result.data.reason, now, result.data.offerId, attachment.edgeId).run();
      return;
    }
    await this.env.DB.prepare(
      "UPDATE calls SET status = ?1, updated_at = ?2 WHERE call_id = ?3 AND edge_id = ?4"
    ).bind(result.data.status, now, result.data.callId, attachment.edgeId).run();
  }

  override async webSocketClose(ws: WebSocket, code: number, reason: string): Promise<void> {
    const attachment = ws.deserializeAttachment() as SocketAttachment | null;
    await this.ctx.storage.put<EdgePresence>("presence", {
      ...(await this.ctx.storage.get<EdgePresence>("presence")),
      connected: false,
      lastSeenAt: Date.now()
    });
    ws.close(code, reason);
    if (attachment) {
      await this.env.DB.prepare("UPDATE edges SET last_seen_at = ?1 WHERE edge_id = ?2")
        .bind(Date.now(), attachment.edgeId).run();
    }
  }

  override async webSocketError(ws: WebSocket): Promise<void> {
    ws.close(1011, "WebSocket error");
  }
}
