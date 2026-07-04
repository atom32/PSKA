-- First-class knowledge bases and corpus membership.

CREATE TABLE IF NOT EXISTS knowledge_bases (
  knowledge_base_id text PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE DEFAULT 'tenant_default',
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  created_by_user_id text NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
  slug text NOT NULL,
  name text NOT NULL,
  description text NOT NULL DEFAULT '',
  kb_type text NOT NULL DEFAULT 'document',
  status text NOT NULL DEFAULT 'active',
  visibility pska_visibility NOT NULL DEFAULT 'private',
  visible_team_ids text[] NOT NULL DEFAULT '{}',
  default_space_id text REFERENCES spaces(space_id) ON DELETE RESTRICT,
  is_default boolean NOT NULL DEFAULT false,
  pinned_at timestamptz,
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  readiness jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  UNIQUE (tenant_id, owner_user_id, slug)
);

CREATE INDEX IF NOT EXISTS knowledge_bases_owner_idx
  ON knowledge_bases(tenant_id, owner_user_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS knowledge_bases_visibility_idx
  ON knowledge_bases(tenant_id, visibility, status);
CREATE INDEX IF NOT EXISTS knowledge_bases_default_idx
  ON knowledge_bases(tenant_id, owner_user_id, is_default)
  WHERE is_default = true;

CREATE TABLE IF NOT EXISTS knowledge_base_sources (
  knowledge_base_id text NOT NULL REFERENCES knowledge_bases(knowledge_base_id) ON DELETE CASCADE,
  knowledge_source_id text NOT NULL REFERENCES knowledge_sources(knowledge_source_id) ON DELETE CASCADE,
  tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE DEFAULT 'tenant_default',
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  membership_status text NOT NULL DEFAULT 'active',
  added_by_user_id text NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
  added_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (knowledge_base_id, knowledge_source_id)
);

CREATE INDEX IF NOT EXISTS knowledge_base_sources_source_idx
  ON knowledge_base_sources(tenant_id, knowledge_source_id, membership_status);
CREATE INDEX IF NOT EXISTS knowledge_base_sources_kb_idx
  ON knowledge_base_sources(tenant_id, knowledge_base_id, membership_status);

CREATE TABLE IF NOT EXISTS knowledge_base_source_items (
  knowledge_base_id text NOT NULL REFERENCES knowledge_bases(knowledge_base_id) ON DELETE CASCADE,
  source_item_id text NOT NULL REFERENCES source_items(source_item_id) ON DELETE CASCADE,
  tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE DEFAULT 'tenant_default',
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  membership_type text NOT NULL DEFAULT 'manual',
  membership_status text NOT NULL DEFAULT 'active',
  added_by_user_id text NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
  added_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (knowledge_base_id, source_item_id)
);

CREATE INDEX IF NOT EXISTS knowledge_base_source_items_source_idx
  ON knowledge_base_source_items(tenant_id, source_item_id, membership_status);
CREATE INDEX IF NOT EXISTS knowledge_base_source_items_kb_idx
  ON knowledge_base_source_items(tenant_id, knowledge_base_id, membership_status);

WITH default_spaces AS (
  SELECT DISTINCT ON (tenant_id, owner_user_id)
    tenant_id,
    owner_user_id,
    space_id
  FROM spaces
  WHERE kind = 'private'
  ORDER BY tenant_id, owner_user_id, updated_at DESC, space_id
)
INSERT INTO knowledge_bases(
  knowledge_base_id,
  tenant_id,
  owner_user_id,
  created_by_user_id,
  slug,
  name,
  description,
  kb_type,
  status,
  visibility,
  default_space_id,
  is_default,
  config,
  readiness
)
SELECT
  'kb_default_' || substr(md5(u.tenant_id || ':' || u.user_id), 1, 24),
  u.tenant_id,
  u.user_id,
  u.user_id,
  'default',
  '默认资料库',
  '迁移创建的默认资料库。',
  'document',
  'active',
  'private'::pska_visibility,
  ds.space_id,
  true,
  '{}'::jsonb,
  '{}'::jsonb
FROM users u
LEFT JOIN default_spaces ds
  ON ds.tenant_id = u.tenant_id
 AND ds.owner_user_id = u.user_id
ON CONFLICT (tenant_id, owner_user_id, slug) DO NOTHING;

INSERT INTO knowledge_base_sources(
  knowledge_base_id,
  knowledge_source_id,
  tenant_id,
  owner_user_id,
  added_by_user_id,
  membership_status,
  metadata
)
SELECT
  kb.knowledge_base_id,
  ks.knowledge_source_id,
  ks.tenant_id,
  ks.owner_user_id,
  ks.owner_user_id,
  CASE WHEN ks.status = 'deleted' THEN 'archived' ELSE 'active' END,
  '{"backfilled": true}'::jsonb
FROM knowledge_sources ks
JOIN knowledge_bases kb
  ON kb.tenant_id = ks.tenant_id
 AND kb.owner_user_id = ks.owner_user_id
 AND kb.is_default = true
ON CONFLICT (knowledge_base_id, knowledge_source_id) DO NOTHING;

INSERT INTO knowledge_base_source_items(
  knowledge_base_id,
  source_item_id,
  tenant_id,
  owner_user_id,
  added_by_user_id,
  membership_type,
  membership_status,
  metadata
)
SELECT
  kb.knowledge_base_id,
  si.source_item_id,
  si.tenant_id,
  si.owner_user_id,
  si.owner_user_id,
  'backfill',
  CASE WHEN coalesce(si.lifecycle_status, 'active') = 'active' THEN 'active' ELSE 'archived' END,
  '{"backfilled": true}'::jsonb
FROM source_items si
JOIN knowledge_bases kb
  ON kb.tenant_id = si.tenant_id
 AND kb.owner_user_id = si.owner_user_id
 AND kb.is_default = true
ON CONFLICT (knowledge_base_id, source_item_id) DO NOTHING;
