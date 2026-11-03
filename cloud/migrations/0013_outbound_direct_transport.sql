-- A8: app-dialed calls may use direct WebRTC with LiveKit kept as the fallback.
-- No SDP, ICE credentials, or call content is persisted.
ALTER TABLE calls ADD COLUMN media_transport TEXT
  CHECK (media_transport IS NULL OR media_transport IN ('direct_pending', 'direct', 'livekit_pending', 'livekit'));
