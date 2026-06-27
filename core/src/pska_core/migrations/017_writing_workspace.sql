CREATE TABLE IF NOT EXISTS writing_boards (
  board_id text PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE DEFAULT 'tenant_default',
  owner_user_id text NOT NULL,
  title text NOT NULL DEFAULT '',
  goal text NOT NULL DEFAULT '',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS writing_nodes (
  node_id text PRIMARY KEY,
  board_id text NOT NULL REFERENCES writing_boards(board_id) ON DELETE CASCADE,
  tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE DEFAULT 'tenant_default',
  owner_user_id text NOT NULL,
  node_type text NOT NULL,
  title text NOT NULL DEFAULT '',
  body_markdown text NOT NULL DEFAULT '',
  position jsonb NOT NULL DEFAULT '{}'::jsonb,
  size jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'idle',
  source_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
  citations jsonb NOT NULL DEFAULT '[]'::jsonb,
  quality_signals jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS writing_edges (
  edge_id text PRIMARY KEY,
  board_id text NOT NULL REFERENCES writing_boards(board_id) ON DELETE CASCADE,
  tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE DEFAULT 'tenant_default',
  owner_user_id text NOT NULL,
  source_node_id text NOT NULL REFERENCES writing_nodes(node_id) ON DELETE CASCADE,
  target_node_id text NOT NULL REFERENCES writing_nodes(node_id) ON DELETE CASCADE,
  edge_type text NOT NULL,
  label text NOT NULL DEFAULT '',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS writing_boards_tenant_owner_idx
  ON writing_boards(tenant_id, owner_user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS writing_nodes_board_idx
  ON writing_nodes(tenant_id, owner_user_id, board_id, node_type, updated_at DESC);

CREATE INDEX IF NOT EXISTS writing_edges_board_idx
  ON writing_edges(tenant_id, owner_user_id, board_id, edge_type, updated_at DESC);

CREATE INDEX IF NOT EXISTS writing_edges_source_target_idx
  ON writing_edges(tenant_id, board_id, source_node_id, target_node_id);
