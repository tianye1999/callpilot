ALTER TABLE inbound_offers ADD COLUMN call_uuid TEXT;

CREATE TABLE device_push_tokens (
  device_id TEXT PRIMARY KEY,
  token_ciphertext TEXT NOT NULL,
  token_nonce TEXT NOT NULL,
  environment TEXT NOT NULL CHECK (environment IN ('sandbox', 'production')),
  updated_at INTEGER NOT NULL,
  FOREIGN KEY (device_id) REFERENCES devices(device_id) ON DELETE CASCADE
);
