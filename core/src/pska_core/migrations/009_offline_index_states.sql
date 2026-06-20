CREATE TABLE IF NOT EXISTS offline_index_states (
  object_type text NOT NULL,
  object_id text NOT NULL,
  owner_user_id text NOT NULL,
  source_item_id text,
  content_hash text,
  mtime text,
  visibility_version text,
  embedding_provider text,
  embedding_model text,
  index_version text NOT NULL DEFAULT 'hipporag_offline.v1',
  status text NOT NULL DEFAULT 'dirty',
  dirty_reason text,
  last_indexed_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (object_type, object_id)
);

CREATE INDEX IF NOT EXISTS offline_index_states_status_idx
  ON offline_index_states(status, object_type, updated_at);

CREATE INDEX IF NOT EXISTS offline_index_states_source_idx
  ON offline_index_states(source_item_id, status);

CREATE INDEX IF NOT EXISTS offline_index_states_owner_idx
  ON offline_index_states(owner_user_id, status);
