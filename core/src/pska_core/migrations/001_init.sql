-- PSKA Core schema v1.
-- Production target: PostgreSQL 17 + pgvector.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE pska_user_role AS ENUM ('admin', 'user', 'viewer', 'agent_service');
CREATE TYPE pska_user_status AS ENUM ('active', 'disabled');
CREATE TYPE pska_visibility AS ENUM ('private', 'team', 'public', 'system');
CREATE TYPE pska_memory_layer AS ENUM ('working', 'episodic', 'semantic', 'procedural', 'profile');
CREATE TYPE pska_review_type AS ENUM (
  'share_proposal',
  'sensitive_content',
  'profile_update',
  'entity_merge',
  'conflict'
);
CREATE TYPE pska_directionality AS ENUM ('directed', 'undirected', 'ambiguous');

CREATE TABLE users (
  user_id text PRIMARY KEY,
  handle text NOT NULL UNIQUE,
  role pska_user_role NOT NULL DEFAULT 'user',
  status pska_user_status NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE teams (
  team_id text PRIMARY KEY,
  slug text NOT NULL UNIQUE,
  description text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE team_memberships (
  user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  team_id text NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
  role text NOT NULL DEFAULT 'member',
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, team_id)
);

CREATE TABLE spaces (
  space_id text PRIMARY KEY,
  slug text NOT NULL UNIQUE,
  kind text NOT NULL,
  owner_user_id text REFERENCES users(user_id) ON DELETE SET NULL,
  team_id text REFERENCES teams(team_id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE source_items (
  source_item_id text PRIMARY KEY,
  source_channel text NOT NULL,
  record_type text NOT NULL,
  source_id text NOT NULL,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
  space_id text NOT NULL REFERENCES spaces(space_id) ON DELETE RESTRICT,
  visibility pska_visibility NOT NULL DEFAULT 'private',
  visible_team_ids text[] NOT NULL DEFAULT '{}',
  title text NOT NULL DEFAULT '',
  url text,
  content_text text NOT NULL DEFAULT '',
  content_hash text NOT NULL UNIQUE,
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX source_items_acl_idx ON source_items(owner_user_id, visibility, space_id);
CREATE INDEX source_items_visible_team_ids_idx ON source_items USING gin(visible_team_ids);
CREATE INDEX source_items_metadata_idx ON source_items USING gin(metadata);
CREATE INDEX source_items_fts_idx ON source_items
  USING gin(to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(url, '') || ' ' || coalesce(content_text, '')));

CREATE TABLE documents (
  document_id text PRIMARY KEY,
  source_item_id text NOT NULL REFERENCES source_items(source_item_id) ON DELETE CASCADE,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
  space_id text NOT NULL REFERENCES spaces(space_id) ON DELETE RESTRICT,
  visibility pska_visibility NOT NULL DEFAULT 'private',
  visible_team_ids text[] NOT NULL DEFAULT '{}',
  title text NOT NULL DEFAULT '',
  body text NOT NULL DEFAULT '',
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE chunks (
  chunk_id text PRIMARY KEY,
  document_id text NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
  source_item_id text NOT NULL REFERENCES source_items(source_item_id) ON DELETE CASCADE,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
  space_id text NOT NULL REFERENCES spaces(space_id) ON DELETE RESTRICT,
  visibility pska_visibility NOT NULL DEFAULT 'private',
  visible_team_ids text[] NOT NULL DEFAULT '{}',
  ordinal integer NOT NULL DEFAULT 0,
  text text NOT NULL DEFAULT '',
  embedding vector(1024),
  embedding_provider text,
  embedding_model text,
  embedding_created_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX chunks_acl_idx ON chunks(owner_user_id, visibility, space_id);
CREATE INDEX chunks_visible_team_ids_idx ON chunks USING gin(visible_team_ids);
CREATE INDEX chunks_fts_idx ON chunks USING gin(to_tsvector('simple', text));
CREATE INDEX chunks_embedding_idx ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TABLE memories (
  memory_id text PRIMARY KEY,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  space_id text NOT NULL REFERENCES spaces(space_id) ON DELETE RESTRICT,
  visibility pska_visibility NOT NULL DEFAULT 'private',
  visible_team_ids text[] NOT NULL DEFAULT '{}',
  memory_type text NOT NULL,
  text text NOT NULL,
  confidence double precision NOT NULL DEFAULT 0,
  source_refs jsonb NOT NULL DEFAULT '[]',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (confidence >= 0 AND confidence <= 1)
);

CREATE TABLE agent_memories (
  agent_memory_id text PRIMARY KEY,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  created_by_user_id text REFERENCES users(user_id) ON DELETE SET NULL,
  layer pska_memory_layer NOT NULL,
  text text NOT NULL,
  confidence double precision NOT NULL DEFAULT 0,
  source_refs jsonb NOT NULL DEFAULT '[]',
  decay_policy text NOT NULL DEFAULT 'manual',
  last_verified_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (owner_user_id <> 'agent_service'),
  CHECK (confidence >= 0 AND confidence <= 1)
);

CREATE TABLE user_profile_cards (
  profile_card_id text PRIMARY KEY,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  profile jsonb NOT NULL DEFAULT '{}',
  confidence double precision NOT NULL DEFAULT 0,
  source_refs jsonb NOT NULL DEFAULT '[]',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (confidence >= 0 AND confidence <= 1)
);

CREATE TABLE entities (
  entity_id text PRIMARY KEY,
  entity_type text NOT NULL,
  label text NOT NULL,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
  space_id text NOT NULL REFERENCES spaces(space_id) ON DELETE RESTRICT,
  visibility pska_visibility NOT NULL DEFAULT 'private',
  visible_team_ids text[] NOT NULL DEFAULT '{}',
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX entities_label_idx ON entities USING gin(to_tsvector('simple', label));
CREATE INDEX entities_acl_idx ON entities(owner_user_id, visibility, space_id);
CREATE INDEX entities_visible_team_ids_idx ON entities USING gin(visible_team_ids);

CREATE TABLE hyperedges (
  hyperedge_id text PRIMARY KEY,
  relation_type text NOT NULL,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
  space_id text NOT NULL REFERENCES spaces(space_id) ON DELETE RESTRICT,
  visibility pska_visibility NOT NULL DEFAULT 'private',
  visible_team_ids text[] NOT NULL DEFAULT '{}',
  directionality pska_directionality NOT NULL DEFAULT 'ambiguous',
  evidence_text text NOT NULL DEFAULT '',
  source_refs jsonb NOT NULL DEFAULT '[]',
  confidence double precision NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (confidence >= 0 AND confidence <= 1)
);

CREATE INDEX hyperedges_acl_idx ON hyperedges(owner_user_id, visibility, space_id);
CREATE INDEX hyperedges_visible_team_ids_idx ON hyperedges USING gin(visible_team_ids);
CREATE INDEX hyperedges_evidence_fts_idx ON hyperedges USING gin(to_tsvector('simple', evidence_text));

CREATE TABLE hyperedge_members (
  hyperedge_id text NOT NULL REFERENCES hyperedges(hyperedge_id) ON DELETE CASCADE,
  entity_id text NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
  role text NOT NULL,
  ordinal integer NOT NULL DEFAULT 0,
  PRIMARY KEY (hyperedge_id, entity_id, role)
);

CREATE INDEX hyperedge_members_entity_idx ON hyperedge_members(entity_id);

CREATE TABLE review_items (
  review_item_id text PRIMARY KEY,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  review_type pska_review_type NOT NULL,
  title text NOT NULL,
  proposal jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'pending',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_events (
  audit_event_id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  actor_user_id text NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
  action text NOT NULL,
  target_type text NOT NULL,
  target_id text NOT NULL,
  decision text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO users(user_id, handle, role)
VALUES
  ('user_primary', 'primary', 'admin'),
  ('user_secondary', 'secondary', 'user'),
  ('agent_service', 'agent_service', 'agent_service')
ON CONFLICT DO NOTHING;

INSERT INTO teams(team_id, slug)
VALUES ('team_default', 'team_default')
ON CONFLICT DO NOTHING;

INSERT INTO spaces(space_id, slug, kind)
VALUES
  ('private_primary', 'private_primary', 'private'),
  ('private_secondary', 'private_secondary', 'private'),
  ('team_shared_default', 'team_shared_default', 'team'),
  ('system_review', 'system_review', 'system'),
  ('agent_workspace', 'agent_workspace', 'agent')
ON CONFLICT DO NOTHING;
