CREATE TABLE IF NOT EXISTS workspace_activity_events (
  workspace_activity_event_id text PRIMARY KEY,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  actor_user_id text NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
  activity_type text NOT NULL,
  target_type text NOT NULL,
  target_id text NOT NULL,
  surface text NOT NULL,
  title text NOT NULL DEFAULT '',
  summary text NOT NULL DEFAULT '',
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (activity_type IN ('opened', 'edited', 'viewed', 'pinned'))
);

CREATE INDEX IF NOT EXISTS workspace_activity_owner_created_idx
  ON workspace_activity_events(owner_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS workspace_activity_target_idx
  ON workspace_activity_events(owner_user_id, target_type, target_id, created_at DESC);

CREATE INDEX IF NOT EXISTS workspace_activity_type_idx
  ON workspace_activity_events(owner_user_id, activity_type, created_at DESC);
