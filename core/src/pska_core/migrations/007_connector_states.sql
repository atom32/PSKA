CREATE TABLE IF NOT EXISTS connector_states (
  connector_state_id text PRIMARY KEY,
  connector_id text NOT NULL,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  enabled boolean NOT NULL DEFAULT true,
  scan_cursor text,
  sync_status text NOT NULL DEFAULT 'idle',
  last_success_at timestamptz,
  last_error_at timestamptz,
  last_error text,
  permission_scope jsonb NOT NULL DEFAULT '{}',
  config jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (connector_id, owner_user_id)
);

CREATE INDEX IF NOT EXISTS connector_states_owner_idx ON connector_states(owner_user_id, enabled);
CREATE INDEX IF NOT EXISTS connector_states_connector_idx ON connector_states(connector_id, sync_status);
CREATE INDEX IF NOT EXISTS connector_states_permission_scope_idx ON connector_states USING gin(permission_scope);
