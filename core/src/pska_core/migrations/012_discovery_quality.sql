ALTER TABLE discovery_items
  ADD COLUMN IF NOT EXISTS fingerprint text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS evidence_snapshot jsonb NOT NULL DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS discovery_score double precision NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS quality_signals jsonb NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS discovery_items_fingerprint_idx
  ON discovery_items(owner_user_id, producer, fingerprint);

CREATE INDEX IF NOT EXISTS discovery_items_quality_idx
  ON discovery_items(owner_user_id, status, discovery_score DESC, created_at DESC);

ALTER TABLE discovery_items
  DROP CONSTRAINT IF EXISTS discovery_items_discovery_score_check;

ALTER TABLE discovery_items
  ADD CONSTRAINT discovery_items_discovery_score_check
  CHECK (discovery_score >= 0 AND discovery_score <= 1);
