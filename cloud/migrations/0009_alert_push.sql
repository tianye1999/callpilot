CREATE TABLE device_alert_push_tokens (
  device_id TEXT PRIMARY KEY,
  token_ciphertext TEXT NOT NULL,
  token_nonce TEXT NOT NULL,
  environment TEXT NOT NULL CHECK (environment IN ('sandbox', 'production')),
  updated_at INTEGER NOT NULL,
  FOREIGN KEY (device_id) REFERENCES devices(device_id) ON DELETE CASCADE
);

CREATE TABLE alert_push_events (
  edge_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (edge_id, event_id),
  FOREIGN KEY (edge_id) REFERENCES edges(edge_id) ON DELETE CASCADE
);
CREATE INDEX idx_alert_push_events_created ON alert_push_events(created_at);
CREATE INDEX idx_alert_push_events_edge_created ON alert_push_events(edge_id, created_at);
