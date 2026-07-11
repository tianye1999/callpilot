PRAGMA foreign_keys = ON;

CREATE TABLE enrollment_invites (
  invite_hash TEXT PRIMARY KEY,
  expires_at INTEGER NOT NULL,
  used_at INTEGER,
  created_at INTEGER NOT NULL
);

CREATE TABLE edges (
  edge_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  public_key TEXT NOT NULL,
  secret_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  revoked_at INTEGER,
  last_seen_at INTEGER
);

CREATE TABLE pairing_sessions (
  pairing_id TEXT PRIMARY KEY,
  edge_id TEXT NOT NULL REFERENCES edges(edge_id),
  code_hash TEXT NOT NULL UNIQUE,
  expires_at INTEGER NOT NULL,
  claimed_at INTEGER,
  created_at INTEGER NOT NULL
);

CREATE TABLE devices (
  device_id TEXT PRIMARY KEY,
  edge_id TEXT NOT NULL REFERENCES edges(edge_id),
  display_name TEXT NOT NULL,
  secret_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_used_at INTEGER NOT NULL,
  revoked_at INTEGER
);

CREATE TABLE calls (
  call_id TEXT PRIMARY KEY,
  edge_id TEXT NOT NULL REFERENCES edges(edge_id),
  device_id TEXT NOT NULL REFERENCES devices(device_id),
  idempotency_key TEXT NOT NULL,
  room_name TEXT NOT NULL UNIQUE,
  phone_identity TEXT NOT NULL,
  edge_identity TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(device_id, idempotency_key)
);

CREATE TABLE audit_events (
  event_id TEXT PRIMARY KEY,
  actor_type TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  action TEXT NOT NULL,
  resource_id TEXT,
  result TEXT NOT NULL,
  occurred_at INTEGER NOT NULL
);

CREATE INDEX devices_edge_active ON devices(edge_id, revoked_at);
CREATE INDEX calls_edge_created ON calls(edge_id, created_at DESC);
CREATE INDEX audit_occurred ON audit_events(occurred_at DESC);
