import { z } from "zod";

import { dataResponseSchema } from "./content-sync";

const opaqueId = z.string().regex(/^[a-z]+_[A-Za-z0-9_-]{12,80}$/);
const sdp = z.string().startsWith("v=0").max(16 * 1024).refine(value => new TextEncoder().encode(value).byteLength <= 16 * 1024);
export const iceServerSchema = z.object({
  urls: z.array(z.string().min(1).max(512)).min(1).max(16),
  username: z.string().max(1024).optional(), credential: z.string().max(1024).optional()
}).strict();
const inboundSessionSchema = z.object({
  sessionId: opaqueId, roomName: z.string().min(1).max(128), browserIdentity: opaqueId,
  edgeIdentity: opaqueId, livekitUrl: z.string().url(), token: z.string().min(1).max(4096)
}).strict();
export const inboundMediaCommandSchema = z.object({
  v: z.literal(1), type: z.enum(["inbound.claim", "inbound.media.sdp", "inbound.media.livekit", "inbound.media.end"]),
  commandId: opaqueId, offerId: opaqueId, callId: opaqueId, claimId: opaqueId,
  generation: z.number().int().nonnegative(), nonce: z.string().min(16).max(128), session: inboundSessionSchema,
  direct: z.union([z.object({ deferred: z.literal(true) }).strict(),
    z.object({ sdpOffer: sdp, iceServers: z.array(iceServerSchema).max(16) }).strict()]).optional()
}).strict().refine(value => value.type === "inbound.claim" ? value.direct !== undefined && "deferred" in value.direct
  : value.type === "inbound.media.sdp" ? value.direct !== undefined && "sdpOffer" in value.direct : value.direct === undefined);
export const inboundAnswerSchema = z.object({
  v: z.literal(1), type: z.literal("inbound.answer"), commandId: opaqueId,
  offerId: opaqueId, callId: opaqueId, claimId: opaqueId,
  status: z.enum(["accepted", "rejected"]), sdpAnswer: sdp.optional(),
  errorCode: z.string().regex(/^[A-Z][A-Z0-9_]{2,63}$/).optional()
}).strict();
export const inboundIceRequestSchema = z.object({
  offerId: opaqueId, generation: z.number().int().nonnegative(), nonce: z.string().min(16).max(128),
  expiresAt: z.number().int().positive(), mode: z.enum(["warm", "get"])
}).strict();

export const createEnrollmentInviteSchema = z.object({
  ttlSeconds: z.number().int().min(60).max(86400).default(3600)
}).strict();

export const claimEnrollmentSchema = z.object({
  code: z.string().min(32).max(256),
  displayName: z.string().trim().min(1).max(64),
  publicKey: z.string().min(32).max(2048)
}).strict();

export const createPairingSchema = z.object({
  ttlSeconds: z.number().int().min(60).max(604800).default(300),
  purpose: z.enum(["standard", "app_review"]).default("standard")
}).strict();

export const claimPairingSchema = z.object({
  code: z.string().regex(/^[23456789A-HJ-NP-Z]{4}-?[23456789A-HJ-NP-Z]{4}$/i),
  displayName: z.string().trim().min(1).max(64)
}).strict();

export const createCallSchema = z.object({
  edgeId: opaqueId,
  idempotencyKey: z.string().min(16).max(128).regex(/^[A-Za-z0-9._:-]+$/)
}).strict();

export const edgeMessageSchema = z.union([
  z.object({
    v: z.literal(1),
    type: z.literal("device.notify"),
    kind: z.enum(["sms", "call_digest", "missed_call"]),
    eventId: z.string().regex(/^event_[a-f0-9]{64}$/)
  }).strict(),
  z.object({
    v: z.literal(1),
    type: z.literal("heartbeat"),
    occurredAt: z.string().datetime(),
    status: z.object({
      modemOnline: z.boolean(),
      lineBusy: z.boolean(),
      inboundPreconnectV1: z.boolean().optional(),
      inboundFastpathV2: z.boolean().optional(),
      version: z.string().max(64).optional()
    }).strict()
  }).strict(),
  z.object({
    v: z.literal(1),
    type: z.literal("command.ack"),
    commandId: opaqueId,
    callId: opaqueId,
    // Present when acknowledging an inbound takeover claim command; routes the
    // ack to inbound_offers instead of the outbound calls table.
    offerId: opaqueId.optional(),
    status: z.enum(["accepted", "rejected"]),
    errorCode: z.string().regex(/^[A-Z][A-Z0-9_]{2,63}$/).optional()
  }).strict(),
  z.object({
    v: z.literal(1),
    type: z.literal("call.status"),
    callId: opaqueId,
    status: z.enum(["media_ready", "dialing", "connected", "ended", "failed"])
  }).strict(),
  // Inbound takeover (#95): Edge offers an in-progress inbound call to paired
  // devices. Deliberately carries no caller number, transcript or preference
  // text — cloud only ever sees opaque ids and lifecycle state.
  z.object({
    v: z.literal(1),
    type: z.literal("inbound.offer"),
    offerId: opaqueId,
    callId: opaqueId,
    generation: z.number().int().min(0),
    nonce: z.string().min(16).max(128),
    expiresAtUnixMs: z.number().int().positive(),
    mode: z.literal("phone_first").optional()
  }).strict(),
  z.object({
    v: z.literal(1),
    type: z.literal("inbound.offer.revoke"),
    offerId: opaqueId,
    callId: opaqueId,
    reason: z.string().regex(/^[A-Z][A-Z0-9_]{2,63}$/)
  }).strict(),
  dataResponseSchema,
  inboundAnswerSchema
]);

export const claimInboundOfferSchema = z.object({
  offerId: opaqueId,
  prepareId: opaqueId.optional(),
  mediaTransport: z.literal("webrtc").optional(),
  sdpOffer: sdp.optional()
}).strict();

export const inboundFallbackSchema = z.object({ claimId: opaqueId }).strict();
export const inboundSdpSchema = z.object({ claimId: opaqueId, sdpOffer: sdp }).strict();

export const prepareInboundOfferSchema = z.object({ offerId: opaqueId }).strict();

export const registerVoipTokenSchema = z.object({
  // Apple explicitly treats device-token length as variable. Keep the wire
  // value even-length hex while bounding it to 32...256 bytes.
  token: z.string().regex(/^(?:[A-Fa-f0-9]{2}){32,256}$/),
  environment: z.enum(["sandbox", "production"])
}).strict();
