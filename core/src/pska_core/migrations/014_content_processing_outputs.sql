CREATE TABLE IF NOT EXISTS knowledge_claims (
  knowledge_claim_id text PRIMARY KEY,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  claim_type text NOT NULL,
  statement text NOT NULL,
  subject text,
  predicate text,
  object text,
  qualifiers jsonb NOT NULL DEFAULT '{}',
  evidence_text text NOT NULL,
  source_refs jsonb NOT NULL DEFAULT '[]',
  confidence double precision NOT NULL DEFAULT 0,
  producer text NOT NULL DEFAULT 'fastreact',
  job_id text,
  request_id text,
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (confidence >= 0 AND confidence <= 1),
  CHECK (statement <> ''),
  CHECK (evidence_text <> ''),
  CHECK (jsonb_array_length(source_refs) > 0)
);

CREATE INDEX IF NOT EXISTS knowledge_claims_owner_created_idx
  ON knowledge_claims(owner_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS knowledge_claims_job_idx
  ON knowledge_claims(job_id);

CREATE INDEX IF NOT EXISTS knowledge_claims_source_refs_idx
  ON knowledge_claims USING gin(source_refs);

CREATE TABLE IF NOT EXISTS digest_notes (
  digest_note_id text PRIMARY KEY,
  owner_user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  title text NOT NULL,
  synopsis text NOT NULL,
  key_points jsonb NOT NULL DEFAULT '[]',
  actions jsonb NOT NULL DEFAULT '[]',
  open_questions jsonb NOT NULL DEFAULT '[]',
  risks jsonb NOT NULL DEFAULT '[]',
  memory_suggestions jsonb NOT NULL DEFAULT '[]',
  relationship_suggestions jsonb NOT NULL DEFAULT '[]',
  source_refs jsonb NOT NULL DEFAULT '[]',
  confidence double precision NOT NULL DEFAULT 0,
  producer text NOT NULL DEFAULT 'fastreact',
  job_id text,
  request_id text,
  metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (confidence >= 0 AND confidence <= 1),
  CHECK (title <> ''),
  CHECK (synopsis <> ''),
  CHECK (jsonb_array_length(source_refs) > 0)
);

CREATE INDEX IF NOT EXISTS digest_notes_owner_created_idx
  ON digest_notes(owner_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS digest_notes_job_idx
  ON digest_notes(job_id);

CREATE INDEX IF NOT EXISTS digest_notes_source_refs_idx
  ON digest_notes USING gin(source_refs);
