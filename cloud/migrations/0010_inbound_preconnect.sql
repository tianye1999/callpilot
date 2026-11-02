ALTER TABLE inbound_offers ADD COLUMN preconnect_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE inbound_offers ADD COLUMN preconnect_room TEXT;

CREATE TABLE inbound_preparations (
  prepare_id TEXT PRIMARY KEY,
  offer_id TEXT NOT NULL REFERENCES inbound_offers(offer_id),
  device_id TEXT NOT NULL REFERENCES devices(device_id),
  room_name TEXT NOT NULL,
  participant_identity TEXT NOT NULL UNIQUE,
  expires_at INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'prepared' CHECK (state IN ('prepared', 'winner', 'invalid')),
  UNIQUE(offer_id, device_id)
);

CREATE TABLE inbound_participant_cleanup (
  edge_id TEXT NOT NULL,
  offer_id TEXT NOT NULL REFERENCES inbound_offers(offer_id),
  room_name TEXT NOT NULL,
  participant_identity TEXT NOT NULL,
  retry_until INTEGER NOT NULL,
  completed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(room_name, participant_identity)
);
CREATE INDEX inbound_cleanup_edge ON inbound_participant_cleanup(edge_id);
