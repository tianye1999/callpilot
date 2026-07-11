# CallPilot Cloud Protocol v1

Issue #42 defines the Beta contract between the hosted control plane, Edge, and
remote handset. JSON requests and WebSocket messages are limited to 16 KiB.
Unknown fields may be ignored; unknown message types are rejected.

## Authentication

- Edge HTTP/WSS: `Authorization: Bearer <edge_id>.<secret>`.
- Phone web client: `__Host-callpilot-device` Secure, HttpOnly, SameSite=Strict
  cookie set by the control plane.
- Administrative Beta invite creation: `Authorization: Bearer <admin token>`.

Long-lived secrets are shown once. The server stores only their SHA-256 hashes.

## Error shape

```json
{"error":{"code":"EDGE_OFFLINE","message":"Edge is offline","requestId":"request-id"}}
```

Error text is informational. Clients branch only on `error.code`.

## Edge WebSocket

Connect to `GET /v1/edges/connect` with the Edge bearer credential. The server
accepts these messages:

```json
{"v":1,"type":"heartbeat","occurredAt":"2026-07-11T00:00:00Z","status":{"modemOnline":true,"lineBusy":false}}
{"v":1,"type":"command.ack","commandId":"cmd_...","status":"accepted"}
{"v":1,"type":"call.status","callId":"call_...","status":"media_ready"}
```

The Edge receives:

```json
{"v":1,"type":"session.start","commandId":"command_...","callId":"call_...","expiresAt":"...","session":{"sessionId":"session_...","roomName":"callpilot_...","browserIdentity":"web_...","edgeIdentity":"edgepart_...","livekitUrl":"wss://...","token":"..."}}
```

`session.start` creates one remote worker but never dials by itself. The handset
joins the room and the existing reliable LiveKit control topic carries dial,
DTMF, and hangup. Edge validates every action locally.

## HTTP resources

```text
POST /v1/admin/enrollment-invites
POST /v1/edge-enrollments/claim
GET  /v1/edges/connect
GET  /v1/edges/{edgeId}/presence
GET  /v1/edges/{edgeId}/devices
POST /v1/edges/{edgeId}/pairing-sessions
POST /v1/pairing-sessions/claim
DELETE /v1/devices/{deviceId}
POST /v1/calls
GET  /v1/calls/{callId}
```

Calls are asynchronous. `POST /v1/calls` returns `202` while the command is sent
to Edge. After Edge accepts it, `GET /v1/calls/{callId}` returns a fresh,
short-lived handset LiveKit credential. Tokens are never persisted.
