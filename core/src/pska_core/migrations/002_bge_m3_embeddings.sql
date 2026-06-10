-- PSKA Core embedding schema for BGE-M3.
-- BGE-M3 dense embeddings are 1024 dimensions.

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE chunks
  ADD COLUMN IF NOT EXISTS embedding_provider text,
  ADD COLUMN IF NOT EXISTS embedding_model text,
  ADD COLUMN IF NOT EXISTS embedding_created_at timestamptz;

DO $$
DECLARE
  current_dim integer;
BEGIN
  SELECT atttypmod
  INTO current_dim
  FROM pg_attribute
  WHERE attrelid = 'chunks'::regclass
    AND attname = 'embedding';

  IF current_dim IS DISTINCT FROM 1024 THEN
    DROP INDEX IF EXISTS chunks_embedding_idx;
    ALTER TABLE chunks
      ALTER COLUMN embedding TYPE vector(1024)
      USING NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS chunks_embedding_idx
  ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
