-- Add tenant/org boundaries while preserving the local single-tenant default.

CREATE TABLE IF NOT EXISTS tenants (
  tenant_id text PRIMARY KEY,
  slug text NOT NULL UNIQUE,
  name text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orgs (
  org_id text PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
  slug text NOT NULL,
  name text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, slug)
);

INSERT INTO tenants(tenant_id, slug, name)
VALUES ('tenant_default', 'default', 'Default Tenant')
ON CONFLICT (tenant_id) DO NOTHING;

INSERT INTO orgs(org_id, tenant_id, slug, name)
VALUES ('org_default', 'tenant_default', 'default', 'Default Org')
ON CONFLICT (org_id) DO NOTHING;

DO $$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'users',
    'teams',
    'team_memberships',
    'spaces',
    'source_items',
    'documents',
    'chunks',
    'memories',
    'agent_memories',
    'user_profile_cards',
    'entities',
    'hyperedges',
    'hyperedge_members',
    'review_items',
    'audit_events',
    'jobs',
    'job_events',
    'connector_states',
    'offline_index_states',
    'workspace_activity_events',
    'discovery_items',
    'knowledge_sources',
    'sync_runs',
    'passage_windows',
    'graph_nodes',
    'graph_edges',
    'knowledge_claims',
    'digest_notes',
    'knowledge_claim_links',
    'digest_note_links'
  ]
  LOOP
    EXECUTE format('ALTER TABLE IF EXISTS %I ADD COLUMN IF NOT EXISTS tenant_id text', table_name);
    EXECUTE format('UPDATE %I SET tenant_id = %L WHERE tenant_id IS NULL', table_name, 'tenant_default');
    EXECUTE format('ALTER TABLE %I ALTER COLUMN tenant_id SET DEFAULT %L', table_name, 'tenant_default');
    EXECUTE format('ALTER TABLE %I ALTER COLUMN tenant_id SET NOT NULL', table_name);
  END LOOP;
END $$;

ALTER TABLE IF EXISTS jobs
  ADD COLUMN IF NOT EXISTS owner_user_id text NOT NULL DEFAULT 'user_primary';

UPDATE jobs
SET owner_user_id = coalesce(nullif(payload->>'owner_user_id', ''), owner_user_id, 'user_primary')
WHERE owner_user_id IS NULL OR owner_user_id = 'user_primary';

ALTER TABLE IF EXISTS users DROP CONSTRAINT IF EXISTS users_handle_key;
ALTER TABLE IF EXISTS teams DROP CONSTRAINT IF EXISTS teams_slug_key;
ALTER TABLE IF EXISTS spaces DROP CONSTRAINT IF EXISTS spaces_slug_key;
ALTER TABLE IF EXISTS source_items DROP CONSTRAINT IF EXISTS source_items_content_hash_key;
ALTER TABLE IF EXISTS connector_states DROP CONSTRAINT IF EXISTS connector_states_connector_id_owner_user_id_key;
ALTER TABLE IF EXISTS knowledge_sources DROP CONSTRAINT IF EXISTS knowledge_sources_owner_user_id_uri_key;

ALTER TABLE IF EXISTS users DROP CONSTRAINT IF EXISTS users_tenant_handle_key;
ALTER TABLE IF EXISTS teams DROP CONSTRAINT IF EXISTS teams_tenant_slug_key;
ALTER TABLE IF EXISTS spaces DROP CONSTRAINT IF EXISTS spaces_tenant_slug_key;
ALTER TABLE IF EXISTS source_items DROP CONSTRAINT IF EXISTS source_items_tenant_content_hash_key;
ALTER TABLE IF EXISTS connector_states DROP CONSTRAINT IF EXISTS connector_states_tenant_connector_owner_key;
ALTER TABLE IF EXISTS knowledge_sources DROP CONSTRAINT IF EXISTS knowledge_sources_tenant_owner_uri_key;

ALTER TABLE IF EXISTS users ADD CONSTRAINT users_tenant_handle_key UNIQUE (tenant_id, handle);
ALTER TABLE IF EXISTS teams ADD CONSTRAINT teams_tenant_slug_key UNIQUE (tenant_id, slug);
ALTER TABLE IF EXISTS spaces ADD CONSTRAINT spaces_tenant_slug_key UNIQUE (tenant_id, slug);
ALTER TABLE IF EXISTS source_items ADD CONSTRAINT source_items_tenant_content_hash_key UNIQUE (tenant_id, content_hash);
ALTER TABLE IF EXISTS connector_states ADD CONSTRAINT connector_states_tenant_connector_owner_key UNIQUE (tenant_id, connector_id, owner_user_id);
ALTER TABLE IF EXISTS knowledge_sources ADD CONSTRAINT knowledge_sources_tenant_owner_uri_key UNIQUE (tenant_id, owner_user_id, uri);

CREATE INDEX IF NOT EXISTS users_tenant_idx ON users(tenant_id, user_id);
CREATE INDEX IF NOT EXISTS teams_tenant_idx ON teams(tenant_id, team_id);
CREATE INDEX IF NOT EXISTS team_memberships_tenant_user_idx ON team_memberships(tenant_id, user_id);
CREATE INDEX IF NOT EXISTS spaces_tenant_idx ON spaces(tenant_id, space_id);
CREATE INDEX IF NOT EXISTS source_items_tenant_acl_idx ON source_items(tenant_id, owner_user_id, visibility, space_id);
CREATE INDEX IF NOT EXISTS documents_tenant_source_idx ON documents(tenant_id, source_item_id);
CREATE INDEX IF NOT EXISTS chunks_tenant_source_idx ON chunks(tenant_id, source_item_id);
CREATE INDEX IF NOT EXISTS chunks_tenant_embedding_idx ON chunks(tenant_id, source_item_id) WHERE embedding IS NOT NULL;
CREATE INDEX IF NOT EXISTS entities_tenant_acl_idx ON entities(tenant_id, owner_user_id, visibility, space_id);
CREATE INDEX IF NOT EXISTS hyperedges_tenant_acl_idx ON hyperedges(tenant_id, owner_user_id, visibility, space_id);
CREATE INDEX IF NOT EXISTS review_items_tenant_owner_idx ON review_items(tenant_id, owner_user_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_tenant_status_idx ON jobs(tenant_id, status, run_after, priority DESC, created_at, job_id);
CREATE INDEX IF NOT EXISTS job_events_tenant_job_idx ON job_events(tenant_id, job_id, created_at, job_event_id);
CREATE INDEX IF NOT EXISTS knowledge_sources_tenant_owner_idx ON knowledge_sources(tenant_id, owner_user_id, status, mode);
CREATE INDEX IF NOT EXISTS sync_runs_tenant_owner_idx ON sync_runs(tenant_id, owner_user_id, status, started_at DESC);
CREATE INDEX IF NOT EXISTS connector_states_tenant_owner_idx ON connector_states(tenant_id, owner_user_id, enabled);
CREATE INDEX IF NOT EXISTS offline_index_states_tenant_owner_idx ON offline_index_states(tenant_id, owner_user_id, status);
CREATE INDEX IF NOT EXISTS workspace_activity_events_tenant_owner_idx ON workspace_activity_events(tenant_id, owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS discovery_items_tenant_owner_idx ON discovery_items(tenant_id, owner_user_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS passage_windows_tenant_source_idx ON passage_windows(tenant_id, source_item_id, document_id, ordinal);
CREATE INDEX IF NOT EXISTS graph_nodes_tenant_owner_idx ON graph_nodes(tenant_id, owner_user_id, node_type, updated_at DESC);
CREATE INDEX IF NOT EXISTS graph_edges_tenant_owner_idx ON graph_edges(tenant_id, owner_user_id, edge_type, updated_at DESC);
CREATE INDEX IF NOT EXISTS knowledge_claims_tenant_owner_idx ON knowledge_claims(tenant_id, owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS digest_notes_tenant_owner_idx ON digest_notes(tenant_id, owner_user_id, created_at DESC);
