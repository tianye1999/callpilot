ALTER TABLE inbound_preparations ADD COLUMN cleanup_queued INTEGER NOT NULL DEFAULT 0;

UPDATE inbound_preparations SET cleanup_queued = 1
WHERE state = 'invalid' AND EXISTS (
  SELECT 1 FROM inbound_participant_cleanup c
  WHERE c.room_name = inbound_preparations.room_name
    AND c.participant_identity = inbound_preparations.participant_identity
);

CREATE INDEX inbound_preparations_live ON inbound_preparations(offer_id)
  WHERE state != 'invalid';
CREATE INDEX inbound_preparations_unqueued ON inbound_preparations(offer_id)
  WHERE state = 'invalid' AND cleanup_queued = 0;
CREATE INDEX inbound_cleanup_pending ON inbound_participant_cleanup(edge_id)
  WHERE completed = 0;
CREATE INDEX inbound_offers_preconnect_updated ON inbound_offers(edge_id, updated_at)
  WHERE preconnect_enabled = 1;
