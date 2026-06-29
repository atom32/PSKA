CREATE TABLE IF NOT EXISTS processing_spans (
  processing_span_id text PRIMARY KEY,
  knowledge_source_id text NOT NULL REFERENCES knowledge_sources(knowledge_source_id) ON DELETE CASCADE,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
  stage text NOT NULL,
  status text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  sync_run_id text REFERENCES sync_runs(sync_run_id) ON DELETE CASCADE,
  source_item_id text REFERENCES source_items(source_item_id) ON DELETE SET NULL,
  duration_ms integer,
  input_payload jsonb NOT NULL DEFAULT '{}',
  output_payload jsonb NOT NULL DEFAULT '{}',
  metadata jsonb NOT NULL DEFAULT '{}',
  error text,
  tenant_id text NOT NULL DEFAULT 'tenant_default'
);

CREATE INDEX IF NOT EXISTS processing_spans_tenant_idx
  ON processing_spans(tenant_id, started_at DESC);

CREATE INDEX IF NOT EXISTS processing_spans_source_idx
  ON processing_spans(knowledge_source_id, started_at DESC);

CREATE INDEX IF NOT EXISTS processing_spans_sync_idx
  ON processing_spans(sync_run_id, stage);

CREATE INDEX IF NOT EXISTS processing_spans_source_item_idx
  ON processing_spans(source_item_id, stage);
