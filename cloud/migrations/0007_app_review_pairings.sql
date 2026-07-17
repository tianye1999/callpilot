ALTER TABLE pairing_sessions
ADD COLUMN purpose TEXT NOT NULL DEFAULT 'standard';
