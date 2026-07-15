-- #95 inbound takeover: offers from an Edge for an in-progress inbound call.
-- Minimal, privacy-preserving state: opaque ids only — no caller number,
-- transcript or preference text ever reaches the cloud.
CREATE TABLE inbound_offers (
  offer_id TEXT PRIMARY KEY,
  edge_id TEXT NOT NULL,
  call_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  nonce TEXT NOT NULL,
  status TEXT NOT NULL, -- offered | claimed | edge_ready | failed | revoked
  claim_id TEXT,
  claimed_device_id TEXT,
  room_name TEXT,
  phone_identity TEXT,
  edge_identity TEXT,
  error_code TEXT,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX idx_inbound_offers_edge_status ON inbound_offers(edge_id, status, expires_at);
