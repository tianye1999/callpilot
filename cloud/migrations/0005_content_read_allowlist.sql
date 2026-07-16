-- Authorization metadata only. Message, transcript, summary and timeline content
-- remain exclusively on Edge and are never persisted in Cloud storage.
CREATE TABLE content_read_edges (
  edge_id TEXT PRIMARY KEY REFERENCES edges(edge_id),
  created_at INTEGER NOT NULL
);
