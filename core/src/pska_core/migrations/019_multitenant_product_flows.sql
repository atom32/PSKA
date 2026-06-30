-- Productized multi-tenant workspace flows: document lifecycle, Ask conversations,
-- and versioned prompt profiles.

DO $$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY['source_items', 'documents', 'chunks']
  LOOP
    EXECUTE format('ALTER TABLE IF EXISTS %I ADD COLUMN IF NOT EXISTS lifecycle_status text NOT NULL DEFAULT %L', table_name, 'active');
    EXECUTE format('ALTER TABLE IF EXISTS %I ADD COLUMN IF NOT EXISTS deleted_at timestamptz', table_name);
    EXECUTE format('ALTER TABLE IF EXISTS %I ADD COLUMN IF NOT EXISTS deleted_by text', table_name);
    EXECUTE format('ALTER TABLE IF EXISTS %I ADD COLUMN IF NOT EXISTS delete_reason text', table_name);
  END LOOP;
END $$;

ALTER TABLE IF EXISTS chunks
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS source_items_lifecycle_idx
  ON source_items(tenant_id, owner_user_id, lifecycle_status, updated_at DESC);

CREATE INDEX IF NOT EXISTS documents_lifecycle_idx
  ON documents(tenant_id, owner_user_id, lifecycle_status, source_item_id);

CREATE INDEX IF NOT EXISTS chunks_lifecycle_idx
  ON chunks(tenant_id, owner_user_id, lifecycle_status, source_item_id);

CREATE TABLE IF NOT EXISTS ask_conversations (
  conversation_id text PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE DEFAULT 'tenant_default',
  owner_user_id text NOT NULL,
  title text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'active',
  summary text NOT NULL DEFAULT '',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ask_messages (
  message_id text PRIMARY KEY,
  conversation_id text NOT NULL REFERENCES ask_conversations(conversation_id) ON DELETE CASCADE,
  tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE DEFAULT 'tenant_default',
  owner_user_id text NOT NULL,
  role text NOT NULL,
  content text NOT NULL DEFAULT '',
  run_id text,
  citations jsonb NOT NULL DEFAULT '[]'::jsonb,
  source_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ask_runs (
  run_id text PRIMARY KEY,
  conversation_id text NOT NULL REFERENCES ask_conversations(conversation_id) ON DELETE CASCADE,
  tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE DEFAULT 'tenant_default',
  owner_user_id text NOT NULL,
  query text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'running',
  result jsonb NOT NULL DEFAULT '{}'::jsonb,
  prompt_profile_id text,
  prompt_profile_version integer,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS ask_conversations_owner_idx
  ON ask_conversations(tenant_id, owner_user_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS ask_messages_conversation_idx
  ON ask_messages(tenant_id, owner_user_id, conversation_id, created_at);

CREATE INDEX IF NOT EXISTS ask_runs_conversation_idx
  ON ask_runs(tenant_id, owner_user_id, conversation_id, started_at DESC);

CREATE TABLE IF NOT EXISTS prompt_profiles (
  prompt_profile_id text PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE DEFAULT 'tenant_default',
  owner_user_id text,
  profile_type text NOT NULL,
  scope text NOT NULL,
  name text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'active',
  current_version integer NOT NULL DEFAULT 1,
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS prompt_profile_versions (
  prompt_profile_version_id text PRIMARY KEY,
  prompt_profile_id text NOT NULL REFERENCES prompt_profiles(prompt_profile_id) ON DELETE CASCADE,
  tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE DEFAULT 'tenant_default',
  profile_type text NOT NULL,
  scope text NOT NULL,
  owner_user_id text,
  version integer NOT NULL,
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (prompt_profile_id, version)
);

CREATE INDEX IF NOT EXISTS prompt_profiles_effective_idx
  ON prompt_profiles(tenant_id, profile_type, scope, owner_user_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS prompt_profiles_unique_scope_idx
  ON prompt_profiles(tenant_id, scope, coalesce(owner_user_id, ''), profile_type);

ALTER TABLE IF EXISTS knowledge_claims
  ADD COLUMN IF NOT EXISTS prompt_profile_id text,
  ADD COLUMN IF NOT EXISTS prompt_profile_version integer;

ALTER TABLE IF EXISTS digest_notes
  ADD COLUMN IF NOT EXISTS prompt_profile_id text,
  ADD COLUMN IF NOT EXISTS prompt_profile_version integer;

ALTER TABLE IF EXISTS writing_nodes
  ADD COLUMN IF NOT EXISTS prompt_profile_id text,
  ADD COLUMN IF NOT EXISTS prompt_profile_version integer;
