CREATE TABLE IF NOT EXISTS passage_windows (
  passage_window_id text PRIMARY KEY,
  source_item_id text NOT NULL REFERENCES source_items(source_item_id) ON DELETE CASCADE,
  document_id text NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  ordinal integer NOT NULL DEFAULT 0,
  title text NOT NULL DEFAULT '',
  text text NOT NULL DEFAULT '',
  start_char integer NOT NULL DEFAULT 0,
  end_char integer NOT NULL DEFAULT 0,
  token_estimate integer NOT NULL DEFAULT 0,
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS passage_windows_source_idx
  ON passage_windows(source_item_id, document_id, ordinal);

CREATE INDEX IF NOT EXISTS passage_windows_fts_idx
  ON passage_windows USING gin(to_tsvector('simple', text));

CREATE TABLE IF NOT EXISTS graph_nodes (
  graph_node_id text PRIMARY KEY,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  node_type text NOT NULL,
  object_type text NOT NULL,
  object_id text NOT NULL,
  label text NOT NULL DEFAULT '',
  summary text NOT NULL DEFAULT '',
  source_refs jsonb NOT NULL DEFAULT '[]',
  confidence double precision,
  metadata jsonb NOT NULL DEFAULT '{}',
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS graph_nodes_owner_type_idx
  ON graph_nodes(owner_user_id, node_type, updated_at DESC);

CREATE TABLE IF NOT EXISTS graph_edges (
  graph_edge_id text PRIMARY KEY,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  edge_type text NOT NULL,
  source_graph_node_id text NOT NULL,
  target_graph_node_id text NOT NULL,
  label text NOT NULL DEFAULT '',
  source_refs jsonb NOT NULL DEFAULT '[]',
  confidence double precision,
  metadata jsonb NOT NULL DEFAULT '{}',
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS graph_edges_owner_type_idx
  ON graph_edges(owner_user_id, edge_type, updated_at DESC);

CREATE INDEX IF NOT EXISTS graph_edges_source_target_idx
  ON graph_edges(source_graph_node_id, target_graph_node_id);

CREATE TABLE IF NOT EXISTS knowledge_claim_links (
  knowledge_claim_id text NOT NULL REFERENCES knowledge_claims(knowledge_claim_id) ON DELETE CASCADE,
  target_type text NOT NULL,
  target_id text NOT NULL,
  link_type text NOT NULL,
  source_refs jsonb NOT NULL DEFAULT '[]',
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (knowledge_claim_id, target_type, target_id, link_type)
);

CREATE TABLE IF NOT EXISTS digest_note_links (
  digest_note_id text NOT NULL REFERENCES digest_notes(digest_note_id) ON DELETE CASCADE,
  target_type text NOT NULL,
  target_id text NOT NULL,
  link_type text NOT NULL,
  source_refs jsonb NOT NULL DEFAULT '[]',
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (digest_note_id, target_type, target_id, link_type)
);
