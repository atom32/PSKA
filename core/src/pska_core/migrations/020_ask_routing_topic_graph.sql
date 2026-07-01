-- Ask routing/evidence columns and PSKA-native topic/fact support graph.

ALTER TABLE IF EXISTS ask_runs
  ADD COLUMN IF NOT EXISTS route jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS evidence_check jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS knowledge_topics (
  topic_id text PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE DEFAULT 'tenant_default',
  owner_user_id text NOT NULL,
  label text NOT NULL,
  normalized_label text NOT NULL,
  topic_type text NOT NULL DEFAULT 'topic',
  description text NOT NULL DEFAULT '',
  confidence double precision NOT NULL DEFAULT 0,
  producer text NOT NULL DEFAULT 'pska.topic_linker',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS knowledge_topics_owner_norm_idx
  ON knowledge_topics(tenant_id, owner_user_id, normalized_label, topic_type);

CREATE INDEX IF NOT EXISTS knowledge_topics_query_idx
  ON knowledge_topics(tenant_id, owner_user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS topic_mentions (
  topic_mention_id text PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE DEFAULT 'tenant_default',
  owner_user_id text NOT NULL,
  topic_id text NOT NULL REFERENCES knowledge_topics(topic_id) ON DELETE CASCADE,
  source_item_id text NOT NULL,
  document_id text,
  chunk_id text,
  artifact_type text NOT NULL DEFAULT 'chunk',
  artifact_id text NOT NULL DEFAULT '',
  mention_text text NOT NULL DEFAULT '',
  confidence double precision NOT NULL DEFAULT 0,
  producer text NOT NULL DEFAULT 'pska.topic_linker',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS topic_mentions_unique_artifact_idx
  ON topic_mentions(tenant_id, owner_user_id, topic_id, artifact_type, artifact_id, source_item_id);

CREATE INDEX IF NOT EXISTS topic_mentions_topic_idx
  ON topic_mentions(tenant_id, owner_user_id, topic_id, created_at DESC);

CREATE INDEX IF NOT EXISTS topic_mentions_source_idx
  ON topic_mentions(tenant_id, owner_user_id, source_item_id, created_at DESC);

CREATE TABLE IF NOT EXISTS artifact_supports (
  artifact_support_id text PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE DEFAULT 'tenant_default',
  owner_user_id text NOT NULL,
  artifact_type text NOT NULL,
  artifact_id text NOT NULL,
  support_type text NOT NULL,
  source_item_id text NOT NULL,
  document_id text,
  chunk_id text,
  topic_id text REFERENCES knowledge_topics(topic_id) ON DELETE SET NULL,
  status text NOT NULL DEFAULT 'active',
  confidence double precision NOT NULL DEFAULT 0,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS artifact_supports_unique_idx
  ON artifact_supports(tenant_id, owner_user_id, artifact_type, artifact_id, support_type, source_item_id, coalesce(chunk_id, ''), coalesce(topic_id, ''));

CREATE INDEX IF NOT EXISTS artifact_supports_artifact_idx
  ON artifact_supports(tenant_id, owner_user_id, artifact_type, artifact_id, status);

CREATE INDEX IF NOT EXISTS artifact_supports_source_idx
  ON artifact_supports(tenant_id, owner_user_id, source_item_id, status);
