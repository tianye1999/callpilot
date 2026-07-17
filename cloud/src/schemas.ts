import { z } from "zod";

import { dataResponseSchema } from "./content-sync";

const opaqueId = z.string().regex(/^[a-z]+_[A-Za-z0-9_-]{12,80}$/);

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
    type: z.literal("heartbeat"),
    occurredAt: z.string().datetime(),
    status: z.object({
      modemOnline: z.boolean(),
      lineBusy: z.boolean(),
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
    expiresAtUnixMs: z.number().int().positive()
  }).strict(),
  z.object({
    v: z.literal(1),
    type: z.literal("inbound.offer.revoke"),
    offerId: opaqueId,
    callId: opaqueId,
    reason: z.string().regex(/^[A-Z][A-Z0-9_]{2,63}$/)
  }).strict(),
  dataResponseSchema
]);

export const claimInboundOfferSchema = z.object({
  offerId: opaqueId
}).strict();

export const registerVoipTokenSchema = z.object({
  // Apple explicitly treats device-token length as variable. Keep the wire
  // value even-length hex while bounding it to 32...256 bytes.
  token: z.string().regex(/^(?:[A-Fa-f0-9]{2}){32,256}$/),
  environment: z.enum(["sandbox", "production"])
}).strict();
