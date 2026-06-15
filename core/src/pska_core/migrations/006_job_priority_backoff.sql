-- PSKA job scheduling controls for digest/background workers.

ALTER TABLE jobs
  ADD COLUMN IF NOT EXISTS priority integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS run_after timestamptz NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS jobs_ready_priority_idx
ON jobs(status, run_after, priority DESC, created_at, job_id)
WHERE status = 'queued';
