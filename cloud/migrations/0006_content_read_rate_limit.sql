-- Short-lived authorization/rate metadata only. Rows are pruned against the
-- one-minute rolling window before each content read.
CREATE TABLE content_read_rate_events (
  event_id TEXT PRIMARY KEY,
  scope_key TEXT NOT NULL,
  occurred_at INTEGER NOT NULL
);

CREATE INDEX content_read_rate_scope_time
  ON content_read_rate_events(scope_key, occurred_at);

CREATE INDEX content_read_rate_time
  ON content_read_rate_events(occurred_at);
