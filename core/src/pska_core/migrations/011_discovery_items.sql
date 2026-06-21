CREATE TABLE IF NOT EXISTS discovery_items (
  discovery_id text PRIMARY KEY,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  discovery_type text NOT NULL,
  title text NOT NULL,
  evidence jsonb NOT NULL DEFAULT '[]',
  confidence double precision NOT NULL DEFAULT 0,
  producer text NOT NULL,
  status text NOT NULL DEFAULT 'new',
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (confidence >= 0 AND confidence <= 1),
  CHECK (status IN ('new', 'accepted', 'ignored', 'snoozed', 'archived'))
);

CREATE INDEX IF NOT EXISTS discovery_items_owner_created_idx
  ON discovery_items(owner_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS discovery_items_status_created_idx
  ON discovery_items(owner_user_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS discovery_items_producer_idx
  ON discovery_items(owner_user_id, producer, created_at DESC);
