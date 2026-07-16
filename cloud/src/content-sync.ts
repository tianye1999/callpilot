import { z } from "zod";

export const CONTENT_WIRE_LIMIT_BYTES = 16 * 1024;
export const CONTENT_CAPABILITIES = ["messages:read", "call_records:read"] as const;

const safeOpaque = (prefix: string) =>
  z.string().regex(new RegExp(`^${prefix}_[A-Za-z0-9_-]{12,80}$`));
// Cursors carry a restart-stable, resource-bound pagination envelope. Unlike
// public entity ids they are not limited to 80 characters, but remain bounded
// and safe to echo in a query string and WSS JSON payload.
export const contentCursorSchema = z.string()
  .min(8)
  .max(2_048)
  .regex(/^cursor_[A-Za-z0-9_-]+$/);
const cursor = contentCursorSchema.nullable();
const revision = safeOpaque("revision");
const unixMillis = z.number().int().nonnegative();
const productStatus = z.string().regex(/^[A-Z][A-Z0-9_]{2,63}$/);

export const contentResourceSchema = z.enum([
  "messages.list",
  "call_records.list",
  "call_records.get",
  "call_timeline.list"
]);
export type ContentResource = z.infer<typeof contentResourceSchema>;

const pageFields = {
  v: z.literal(1),
  nextCursor: cursor,
  hasMore: z.boolean(),
  collectionRevision: revision,
  oldestAvailableAt: unixMillis.nullable()
};

const messageSchema = z.object({
  messageId: safeOpaque("msg"),
  revision,
  direction: z.enum(["INBOUND", "OUTBOUND"]),
  address: z.string(),
  text: z.string(),
  occurredAt: unixMillis,
  recordedAt: unixMillis,
  status: z.enum(["RECEIVED", "SENT", "FAILED", "ERROR"])
}).passthrough().refine(
  (message) => message.status !== "RECEIVED" || message.direction === "INBOUND",
  { path: ["status"], message: "RECEIVED status is inbound only" }
);

const callRecordSchema = z.object({
  callId: safeOpaque("call"),
  revision,
  direction: z.enum(["INBOUND", "OUTBOUND"]),
  address: z.string().nullable(),
  startedAt: unixMillis,
  endedAt: unixMillis.nullable(),
  durationMs: unixMillis.nullable(),
  status: productStatus,
  answered: z.boolean(),
  source: z.enum(["AGENT", "REMOTE_HANDSET", "UNKNOWN"]),
  summaryState: z.enum(["PENDING", "READY", "FAILED", "UNAVAILABLE"]),
  summaryPreview: z.string().nullable(),
  hasTranscript: z.boolean(),
  triageOutcome: z.enum(["AI_HANDLED", "REJECTED", "TRANSFERRED", "UNKNOWN"]).nullable()
}).passthrough();

const summarySchema = z.object({
  ok: z.boolean(),
  text: z.string().nullable(),
  callerIdentity: z.string().nullable(),
  intent: z.string().nullable(),
  urgency: z.string().nullable(),
  callbackNeeded: z.boolean().nullable(),
  errorCode: z.string().nullable(),
  resultSource: z.string().nullable(),
  resultVerification: z.string().nullable()
}).passthrough();

const knownTimelineItemSchema = z.discriminatedUnion("type", [
  z.object({
    timelineItemId: safeOpaque("item"),
    occurredAt: unixMillis,
    type: z.literal("TRANSCRIPT"),
    role: z.enum(["AGENT", "CALLER"]),
    text: z.string()
  }).passthrough(),
  z.object({
    timelineItemId: safeOpaque("item"),
    occurredAt: unixMillis,
    type: z.literal("RESULT"),
    status: productStatus,
    summary: z.string().nullable()
  }).passthrough(),
  z.object({
    timelineItemId: safeOpaque("item"),
    occurredAt: unixMillis,
    type: z.literal("TRIAGE"),
    category: z.enum(["MARKETING", "PERSONAL", "NEEDS_OWNER", "UNKNOWN"]),
    action: z.enum(["CLARIFY", "CONTINUE_AI", "REJECT", "TRANSFER"]),
    confidence: z.number().min(0).max(1),
    reasonCode: z.string()
  }).passthrough(),
  z.object({
    timelineItemId: safeOpaque("item"),
    occurredAt: unixMillis,
    type: z.literal("TAKEOVER"),
    state: z.enum(["REQUESTED", "COMMITTED", "OWNER_HANGUP", "FAILED"]),
    reasonCode: z.string().nullable()
  }).passthrough()
]);

const knownTimelineTypes = new Set(["TRANSCRIPT", "RESULT", "TRIAGE", "TAKEOVER"]);
const futureTimelineItemSchema = z.object({
  timelineItemId: safeOpaque("item"),
  occurredAt: unixMillis,
  type: z.string().regex(/^[A-Z][A-Z0-9_]{2,63}$/)
}).passthrough().refine((item) => !knownTimelineTypes.has(item.type), {
  message: "known timeline types must satisfy their complete schema"
});
const timelineItemSchema = z.union([knownTimelineItemSchema, futureTimelineItemSchema]);

function pageOf<T extends z.ZodType>(item: T) {
  return z.object({ ...pageFields, items: z.array(item).max(100) })
    .passthrough()
    .refine((page) => page.hasMore === (page.nextCursor !== null), {
      message: "nextCursor must be present exactly when hasMore is true"
    });
}

export const messagesPageSchema = pageOf(messageSchema);
export const callRecordsPageSchema = pageOf(callRecordSchema);
export const callTimelinePageSchema = pageOf(timelineItemSchema);
export const callRecordDetailSchema = z.object({
  v: z.literal(1),
  record: callRecordSchema,
  summary: summarySchema.nullable(),
  timelineRevision: revision
}).passthrough().superRefine((detail, context) => {
  const summaryMustBeNull = ["PENDING", "UNAVAILABLE"].includes(detail.record.summaryState);
  if (summaryMustBeNull !== (detail.summary === null)) {
    context.addIssue({
      code: "custom",
      path: ["summary"],
      message: "summary must match record.summaryState"
    });
  }
});

const listParams = z.object({
  limit: z.number().int().min(1).max(100),
  cursor
}).strict();

export const dataRequestSchema = z.discriminatedUnion("resource", [
  z.object({
    v: z.literal(1),
    type: z.literal("data.request"),
    requestId: safeOpaque("request"),
    deviceId: safeOpaque("device"),
    resource: z.literal("messages.list"),
    params: listParams,
    issuedAtUnixMs: unixMillis,
    expiresAtUnixMs: unixMillis
  }).strict(),
  z.object({
    v: z.literal(1),
    type: z.literal("data.request"),
    requestId: safeOpaque("request"),
    deviceId: safeOpaque("device"),
    resource: z.literal("call_records.list"),
    params: listParams,
    issuedAtUnixMs: unixMillis,
    expiresAtUnixMs: unixMillis
  }).strict(),
  z.object({
    v: z.literal(1),
    type: z.literal("data.request"),
    requestId: safeOpaque("request"),
    deviceId: safeOpaque("device"),
    resource: z.literal("call_records.get"),
    params: z.object({ callId: safeOpaque("call") }).strict(),
    issuedAtUnixMs: unixMillis,
    expiresAtUnixMs: unixMillis
  }).strict(),
  z.object({
    v: z.literal(1),
    type: z.literal("data.request"),
    requestId: safeOpaque("request"),
    deviceId: safeOpaque("device"),
    resource: z.literal("call_timeline.list"),
    params: z.object({ callId: safeOpaque("call"), limit: listParams.shape.limit, cursor }).strict(),
    issuedAtUnixMs: unixMillis,
    expiresAtUnixMs: unixMillis
  }).strict()
]).refine((request) => (
  request.expiresAtUnixMs > request.issuedAtUnixMs
  && request.expiresAtUnixMs - request.issuedAtUnixMs <= 10_000
), { message: "request expiry must be within 10 seconds after issue" });

export type DataRequest = z.infer<typeof dataRequestSchema>;

export const contentErrorCodeSchema = z.enum([
  "INVALID_REQUEST",
  "CURSOR_INVALID",
  "FORBIDDEN",
  "FEATURE_DISABLED",
  "NOT_FOUND",
  "RATE_LIMITED",
  "PAYLOAD_TOO_LARGE",
  "EDGE_OFFLINE",
  "TIMEOUT",
  "INTERNAL_ERROR"
]);
export type ContentErrorCode = z.infer<typeof contentErrorCodeSchema>;

const responseBase = {
  v: z.literal(1),
  type: z.literal("data.response"),
  requestId: safeOpaque("request")
};

export const dataResponseSchema = z.union([
  z.object({ ...responseBase, resource: z.literal("messages.list"), status: z.literal("ok"), body: messagesPageSchema }).strict(),
  z.object({ ...responseBase, resource: z.literal("call_records.list"), status: z.literal("ok"), body: callRecordsPageSchema }).strict(),
  z.object({ ...responseBase, resource: z.literal("call_records.get"), status: z.literal("ok"), body: callRecordDetailSchema }).strict(),
  z.object({ ...responseBase, resource: z.literal("call_timeline.list"), status: z.literal("ok"), body: callTimelinePageSchema }).strict(),
  z.object({
    ...responseBase,
    resource: contentResourceSchema,
    status: z.literal("error"),
    error: z.object({ code: contentErrorCodeSchema }).strict()
  }).strict()
]);

export type DataResponse = z.infer<typeof dataResponseSchema>;

export function serializedByteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

export function isBeforeRelayDeadline(deadline: number, now: number): boolean {
  return now < deadline;
}

export function responseMatchesRequest(response: DataResponse, request: DataRequest): boolean {
  if (response.requestId !== request.requestId || response.resource !== request.resource) return false;
  if (response.status === "error") return true;
  switch (request.resource) {
    case "messages.list":
      return response.resource === "messages.list" && response.body.items.length <= request.params.limit;
    case "call_records.list":
      return response.resource === "call_records.list" && response.body.items.length <= request.params.limit;
    case "call_timeline.list":
      return response.resource === "call_timeline.list" && response.body.items.length <= request.params.limit;
    case "call_records.get":
      return response.resource === "call_records.get" && response.body.record.callId === request.params.callId;
  }
}
