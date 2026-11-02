-- No SDP, ICE credentials, or call content is persisted.
ALTER TABLE inbound_offers ADD COLUMN media_transport TEXT
  CHECK (media_transport IS NULL OR media_transport IN ('direct_eligible', 'direct_pending', 'direct', 'livekit_pending', 'livekit'));
