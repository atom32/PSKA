CREATE TABLE IF NOT EXISTS knowledge_sources (
  knowledge_source_id text PRIMARY KEY,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  name text NOT NULL,
  source_type text NOT NULL,
  uri text NOT NULL,
  mode text NOT NULL DEFAULT 'manual',
  status text NOT NULL DEFAULT 'authorized',
  connector_id text NOT NULL DEFAULT 'files',
  space_id text NOT NULL REFERENCES spaces(space_id) ON DELETE RESTRICT,
  visibility pska_visibility NOT NULL DEFAULT 'private',
  visible_team_ids text[] NOT NULL DEFAULT '{}',
  permission_scope jsonb NOT NULL DEFAULT '{}',
  config jsonb NOT NULL DEFAULT '{}',
  last_sync_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, uri)
);

CREATE INDEX IF NOT EXISTS knowledge_sources_owner_idx
  ON knowledge_sources(owner_user_id, status, mode);
CREATE INDEX IF NOT EXISTS knowledge_sources_connector_idx
  ON knowledge_sources(connector_id, source_type, status);
CREATE INDEX IF NOT EXISTS knowledge_sources_permission_scope_idx
  ON knowledge_sources USING gin(permission_scope);

CREATE TABLE IF NOT EXISTS sync_runs (
  sync_run_id text PRIMARY KEY,
  knowledge_source_id text NOT NULL REFERENCES knowledge_sources(knowledge_source_id) ON DELETE CASCADE,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  connector_id text NOT NULL,
  status text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  scanned integer NOT NULL DEFAULT 0,
  ingested integer NOT NULL DEFAULT 0,
  new_files integer NOT NULL DEFAULT 0,
  changed_files integer NOT NULL DEFAULT 0,
  unchanged_files integer NOT NULL DEFAULT 0,
  moved_files integer NOT NULL DEFAULT 0,
  missing_files integer NOT NULL DEFAULT 0,
  skipped integer NOT NULL DEFAULT 0,
  failed integer NOT NULL DEFAULT 0,
  error text,
  report jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS sync_runs_source_idx
  ON sync_runs(knowledge_source_id, started_at DESC);
CREATE INDEX IF NOT EXISTS sync_runs_owner_idx
  ON sync_runs(owner_user_id, status, started_at DESC);
