-- PSKA durable worker metadata and external run tracking.

ALTER TABLE jobs
  ADD COLUMN IF NOT EXISTS worker_id text,
  ADD COLUMN IF NOT EXISTS leased_until timestamptz,
  ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz,
  ADD COLUMN IF NOT EXISTS external_run_id text,
  ADD COLUMN IF NOT EXISTS source_refs jsonb NOT NULL DEFAULT '[]';

CREATE INDEX IF NOT EXISTS jobs_worker_lease_idx
ON jobs(status, leased_until, worker_id);

CREATE INDEX IF NOT EXISTS jobs_external_run_idx
ON jobs(external_run_id)
WHERE external_run_id IS NOT NULL;
