# CallPilot Content Sync Protocol v1

Issue #99 defines the read-only contract that moves SMS and normalized call
history from an owner's Edge to paired native apps. This document is the single
source of truth for the four content endpoints, their DTOs, opaque cursors and the
Cloud-to-Edge relay. Enrollment, pairing and call control remain in
[`remote-cloud-protocol.md`](remote-cloud-protocol.md).

## 1. Scope and invariants

Version 1 supports only:

```text
GET /v1/messages
GET /v1/call-records
GET /v1/call-records/{callId}
GET /v1/call-records/{callId}/timeline
```

It does not support SMS sending, deletion, recordings, raw event JSON, arbitrary
Edge HTTP proxying, Cloud content caching, cross-device read state or push.

The following invariants are contractual:

1. Edge is the only durable source of message and call content.
2. Cloud authenticates and authorizes; it relays one bounded response and does not
   persist content. Worker/DO memory can see plaintext while the request is active.
3. The relevant `messages:read` or `call_records:read` capability and both the
   Cloud and Edge feature gates are required. Gates default off.
4. A device can read only its paired Edge. There is no client-supplied `edgeId` on
   content endpoints and no generic resource path in relay messages.
5. HTTP and WSS payloads are UTF-8 JSON. The serialized Edge WSS message, including
   its envelope, must not exceed 16,384 bytes.
6. The App ignores unknown object fields and unknown timeline item types. Cloud and
   Edge reject unknown WSS message types, invalid required fields and unsupported
   protocol versions.
7. All content responses carry `Cache-Control: no-store`. Logs, audit details,
   traces, crash reports and metric labels contain no message body, transcript,
   summary, cursor, token or raw relay payload.

## 2. Authentication, capabilities and revocation

The App uses the existing `__Host-callpilot-device` credential. Cloud derives
`deviceId` and `edgeId` from that authenticated credential; request bodies and
query strings cannot override either identity.

Content capabilities are additive values in the device response:

```json
{"capabilities":["messages:read","call_records:read"]}
```

The field is optional for backward compatibility; absence means no content-read
capability. `messages:read` covers only `/v1/messages`. `call_records:read` covers
the three call-record endpoints. Capabilities never imply SMS send, deletion,
recording access, settings access or modem commands.

Capabilities are server-managed authorization metadata; a phone cannot request or
self-assert them. Pairing alone grants none. During the closed Beta, an operator
may grant both capabilities only to devices on an allowlisted Edge after the owner
enables content sync. A future owner-facing grant UI may replace that provisioning
step without changing this read contract. The Cloud binding is
`CONTENT_READ_ENABLED`; the Edge setting is
`REMOTE_CONTENT_READ_ENABLED`. Both parse to false when absent or invalid.

Cloud checks active pairing, ownership, capability, global gate and rate limit
before dispatch. Edge checks its local gate, fixed resource allowlist, expiry,
cursor and size. A device revocation has these effects:

- new HTTP reads fail authorization without dispatch;
- a pending response is re-authorized before Cloud returns it and is discarded if
  the device was revoked after dispatch;
- a native client receiving that authorization failure erases its content cache
  and local unread watermark;
- revocation does not delete the owner's source data on Edge.

## 3. Wire conventions

- Public DTO fields are camelCase and product enum values/error codes are
  `UPPER_SNAKE_CASE`. WSS message `type`/`resource` discriminators and transport
  `status` values use the exact lower-case spellings documented in section 5.
- IDs are opaque strings with a typed prefix and 12-80 safe characters, for
  example `msg_...`, `call_...`, `item_...`, `request_...` and `revision_...`.
- Times are Unix epoch milliseconds encoded as JSON integers. Durations are
  integer milliseconds.
- Nullable product fields are present with `null`. Unknown added fields must be
  ignored. A missing required v1 field is invalid.
- Clients branch on stable codes and enums, never English `message` text.
- Phone numbers/addresses are display strings from Edge. Clients do not infer
  carrier or identity from their shape.

### 3.1 Collection pagination

`/v1/messages` and `/v1/call-records` accept:

| Query | Default | Limit | Meaning |
| --- | ---: | ---: | --- |
| `limit` | 25 | 1-100 | Maximum item count before the byte budget is applied |
| `cursor` | absent | opaque | Continue the anchored newest-first traversal |

`cursor` is an opaque, resource-bound continuation token. Clients store and echo
it without decoding or editing it. Edge validates version, resource, anchor and
position; invalid or cross-resource cursors fail with `CURSOR_INVALID`.

The first page anchors the traversal at the newest `(sort timestamp, stable ID)`.
Later inserts sort above that anchor and therefore do not duplicate, skip or push
items between pages. Existing items may still gain a new `revision` (for example a
late call summary); clients merge by stable ID and replace only with a different
revision. A fresh first-page request obtains current revisions.

Every page has this envelope:

```json
{
  "v": 1,
  "items": [],
  "nextCursor": null,
  "hasMore": false,
  "collectionRevision": "revision_opaque_value",
  "oldestAvailableAt": 1784160000000
}
```

- `nextCursor` is non-null exactly when `hasMore` is true.
- `collectionRevision` changes whenever an item is added or its public DTO changes.
  It is a change detector, not a database snapshot or an ordering key.
- `oldestAvailableAt` is the oldest retained item timestamp, or null for an empty
  collection. After a successful complete traversal, an App may remove cached
  entries older than this floor. Version 1 has no deletion tombstone stream.
- Edge may return fewer than `limit` items to stay within the WSS byte limit. It
  must still return a cursor when more items remain.
- Timeline uses the same envelope but its chronological cursor rules are defined
  separately in section 4.4.

## 4. HTTP resources and DTOs

### 4.1 `GET /v1/messages`

Requires `messages:read`. Items are newest first and deliberately remain a flat
send/receive history, matching the desktop MVP. Carrier multipart fragments remain
separate items; v1 does not guess linkage after the modem has discarded UDH
metadata.

`Message` fields:

| Field | Type | Contract |
| --- | --- | --- |
| `messageId` | string | Stable opaque ID |
| `revision` | string | Changes only if this public item changes |
| `direction` | `INBOUND \| OUTBOUND` | Direction relative to the Edge SIM |
| `address` | string | Sender for inbound, destination for outbound |
| `text` | string | Exact stored fragment text; never silently truncated |
| `occurredAt` | integer | SMS timestamp when available, otherwise recorded time |
| `recordedAt` | integer | Edge ingestion/send-result time |
| `status` | `RECEIVED \| SENT \| FAILED \| ERROR` | `RECEIVED` is inbound only |

The App owns its unread watermark locally. Opening a stale/offline page or a
failed refresh does not advance it. Version 1 does not synchronize read state.

New messages receive and persist an opaque ID at ingestion. A one-time legacy
migration assigns IDs before serving v1 and atomically writes them back. Its
canonical input includes the stored event plus an occurrence ordinal so two
otherwise identical stored entries remain distinct; the ordinal is not recomputed
after the 500-entry retention window advances.

### 4.2 `GET /v1/call-records`

Requires `call_records:read`. Items are newest first.

`CallRecord` fields:

| Field | Type | Contract |
| --- | --- | --- |
| `callId` | string | Stable opaque public ID; never the local directory name |
| `revision` | string | Changes for late summary or other public DTO update |
| `direction` | `INBOUND \| OUTBOUND` | Direction relative to Edge |
| `address` | string or null | Remote party; null for unavailable/hidden identity |
| `startedAt` | integer | Unix milliseconds |
| `endedAt` | integer or null | Null only while an eligible record is unfinished |
| `durationMs` | integer or null | Non-negative; null when no end time |
| `status` | `COMPLETED \| NOT_CONNECTED \| FAILED \| UNKNOWN` | Product-normalized outcome; clients handle added values |
| `answered` | boolean | Whether an answered event was recorded |
| `source` | `AGENT \| REMOTE_HANDSET \| UNKNOWN` | Product-normalized owner of the call |
| `summaryState` | `PENDING \| READY \| FAILED \| UNAVAILABLE` | Summary lifecycle |
| `summaryPreview` | string or null | Short Edge-produced preview, not client truncation |
| `hasTranscript` | boolean | Timeline has at least one public transcript item |
| `triageOutcome` | `AI_HANDLED \| REJECTED \| TRANSFERRED \| UNKNOWN` or null | Inbound normalized result |

`PENDING` means an Edge summary worker was actually scheduled and has not reached
a terminal result. Calls for which summary generation is disabled or ineligible
(for example, no caller transcript) are `UNAVAILABLE`, not permanently pending.

Internal paths, byte counts, recording flags and raw source/event values are not
public fields. Version 1 exposes no recording capability.

Existing directory names contain timestamps, direction and sometimes a number, so
they must never be reused as public `callId`. Edge derives or persists a stable
non-PII public ID and keeps the filesystem key internal.

### 4.3 `GET /v1/call-records/{callId}`

Requires `call_records:read`. `callId` must belong to the authenticated device's
paired Edge. The response contains:

```json
{
  "v": 1,
  "record": {"callId": "call_...", "revision": "revision_..."},
  "summary": null,
  "timelineRevision": "revision_..."
}
```

`record` is the complete `CallRecord` DTO. `summary` is null for `PENDING` or
`UNAVAILABLE`, otherwise:

| Field | Type | Contract |
| --- | --- | --- |
| `ok` | boolean | Whether summary generation produced a usable result |
| `text` | string or null | Product summary |
| `callerIdentity` | string or null | Model-extracted identity, not verified identity |
| `intent` | string or null | Model-extracted intent |
| `urgency` | string or null | Model-extracted urgency |
| `callbackNeeded` | boolean or null | Null when unknown |
| `errorCode` | string or null | Stable, backend-neutral failure code |
| `resultSource` | string or null | Optional normalized evidence source |
| `resultVerification` | string or null | Optional normalized verification state |

Raw verification evidence and SMS bodies are not embedded in call detail. A late
summary changes `record.revision`, `summaryState`, `summary`,
`timelineRevision` and `collectionRevision`; the `callId` remains stable.

### 4.4 `GET /v1/call-records/{callId}/timeline`

Requires `call_records:read`. It accepts `limit` (default 50, range 1-100) and an
opaque cursor. The page envelope matches section 3.1, but items are ordered oldest
to newest for transcript reading. The cursor advances after the last returned
`(occurredAt, timelineItemId)`; newly appended items cannot shift or duplicate
earlier pages. A fresh request without a cursor begins at the oldest public item.

Each item has `timelineItemId`, `occurredAt`, `type` and type-specific fields.
Version 1 emits only these product events:

| `type` | Additional fields | Source mapping |
| --- | --- | --- |
| `TRANSCRIPT` | `role: AGENT \| CALLER`, `text` | public transcript events |
| `RESULT` | `status: COMPLETED \| NOT_CONNECTED \| FAILED \| UNKNOWN`, `summary` (nullable) | terminal product outcome |
| `TRIAGE` | `category: MARKETING \| PERSONAL \| NEEDS_OWNER \| UNKNOWN`, `action: CLARIFY \| CONTINUE_AI \| REJECT \| TRANSFER`, `confidence` (0-1), `reasonCode` | consumed triage verdict; no model reasoning |
| `TAKEOVER` | `state: REQUESTED \| COMMITTED \| OWNER_HANGUP \| FAILED`, `reasonCode` (nullable) | normalized takeover lifecycle |

Debug latency, prompts, tool arguments, DTMF digits, raw model reasoning and
unknown internal events are not mapped. Clients ignore unknown future timeline
types so the union can grow additively.

## 5. Cloud-to-Edge WebSocket relay

Cloud sends one request over the authenticated Edge's existing Durable Object
WebSocket. `deviceId` is injected by Cloud after authentication, never copied from
phone input.

### 5.1 `data.request`

```json
{
  "v": 1,
  "type": "data.request",
  "requestId": "request_opaque_value",
  "deviceId": "device_opaque_value",
  "resource": "messages.list",
  "params": {"limit": 25, "cursor": null},
  "issuedAtUnixMs": 1784160000000,
  "expiresAtUnixMs": 1784160005000
}
```

`resource` and `params` are a closed discriminated union:

| Resource | Params |
| --- | --- |
| `messages.list` | `limit`, `cursor` |
| `call_records.list` | `limit`, `cursor` |
| `call_records.get` | `callId` |
| `call_timeline.list` | `callId`, `limit`, `cursor` |

Request IDs are one-time within an Edge Durable Object. The expiry must be after
issue time and no more than 10 seconds later. Edge rejects expired, repeated,
unknown-resource or gate-disabled requests without reading data.

### 5.2 `data.response`

Success:

```json
{
  "v": 1,
  "type": "data.response",
  "requestId": "request_opaque_value",
  "resource": "messages.list",
  "status": "ok",
  "body": {
    "v": 1,
    "items": [],
    "nextCursor": null,
    "hasMore": false,
    "collectionRevision": "revision_opaque_value",
    "oldestAvailableAt": null
  }
}
```

Failure:

```json
{
  "v": 1,
  "type": "data.response",
  "requestId": "request_opaque_value",
  "resource": "messages.list",
  "status": "error",
  "error": {"code": "CURSOR_INVALID"}
}
```

Exactly one of `body` or `error` is present. Cloud accepts a response only when
the Edge connection, pending `requestId`, resource and deadline all match. A
duplicate, unsolicited, mismatched or late response is discarded without content
logging. Cloud re-authorizes the device immediately before returning success.

There is no generic chunk type in v1. Edge reduces item count until the full
serialized response fits 16,384 bytes. If a single exact item cannot fit, it
returns `PAYLOAD_TOO_LARGE`; it never silently truncates content. A later protocol
version may add item/chunk retrieval after a separate review.

## 6. Error contract

HTTP errors use the existing shape:

```json
{"error":{"code":"EDGE_OFFLINE","message":"Edge is offline","requestId":"request_..."}}
```

| Code | HTTP | Meaning |
| --- | ---: | --- |
| `INVALID_REQUEST` | 400 | Query/path/schema is invalid |
| `CURSOR_INVALID` | 400 | Cursor malformed, stale, or bound to another resource |
| `UNAUTHORIZED` | 401 | Device credential absent, invalid or revoked |
| `FORBIDDEN` | 403 | Device not paired to this Edge or lacks capability |
| `FEATURE_DISABLED` | 403 | Cloud or Edge content-read gate is off |
| `NOT_FOUND` | 404 | Call record does not exist on the paired Edge |
| `RATE_LIMITED` | 429 | Per-device/Edge content-read limit exceeded |
| `PAYLOAD_TOO_LARGE` | 413 | Request, response or one exact item cannot fit v1 limit |
| `EDGE_OFFLINE` | 503 | No current authenticated Edge WebSocket |
| `TIMEOUT` | 504 | Edge did not return an accepted response before deadline |
| `INTERNAL_ERROR` | 500 | Request failed without exposing internal details |

Cloud maps Edge error codes to this table and supplies client-safe text. It never
returns raw exceptions, filesystem paths or parser details.

## 7. Rate, timeout and lifecycle bounds

- Cloud applies a per-device and per-Edge sliding-window read limit before WSS
  dispatch. Exact numeric limits are deployment configuration, not a client
  contract; `429` and Retry-After behavior are stable.
- One relay request expires no later than 10 seconds after issue. Cloud may use a
  shorter wait and then discards the pending correlation. Edge work is read-only
  and cancellation-safe.
- A response has at most 100 items and at most 16,384 serialized UTF-8 bytes. The
  recommended defaults are 25 message/call items and 50 timeline items.
- Retention remains owned by Edge. Cloud cannot promise availability older than
  `oldestAvailableAt` and never extends Edge retention.
- App refreshes on Tab entry, foreground resume and explicit pull-to-refresh. The
  initial foreground polling recommendation is 30-60 seconds; it is not a wire
  guarantee and may be replaced by privacy-safe push later.

## 8. Local cache and UI contract

- Native clients store only bounded recent pages in platform-protected storage.
- Cached data is labeled stale/offline when a live refresh fails; it is not shown
  as current.
- Unpair, remote revocation observed as authorization failure, or explicit local
  clear erases message/call/timeline content and the local unread watermark.
- The SMS MVP is a flat list; no conversation grouping or guessed multipart merge.
- A remote-handset call with no AI transcript/summary is a normal empty state.
- Issue #99 adds localized strings only for its new screens. Existing App string
  migration remains UX-D4 scope.

## 9. Shared fixtures and compatibility

Machine-readable, synthetic fixtures live in
`docs/fixtures/content-sync/v1/`. They contain no real phone number, SMS, transcript,
credential or token. Edge, Cloud, iOS and Android tests must load the same files.

Version 1 is additive to the existing call-control protocol. Old Edge versions do
not advertise the capability and reject/never receive content messages; old Apps
ignore the optional capability field and never call the new endpoints. Disabling
either feature gate restores the pre-#99 surface without changing pairing, calls,
inbound takeover or the desktop Web UI.

References: #99, #42, #56, #27,
[`ADR-002`](decisions/002-cloud-control-plane.md).
