-- PSKA Core durable local jobs.

CREATE TABLE IF NOT EXISTS jobs (
  job_id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  job_type text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'queued',
  attempts integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 3,
  error text,
  result jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'canceled')),
  CHECK (attempts >= 0),
  CHECK (max_attempts > 0)
);

CREATE INDEX IF NOT EXISTS jobs_status_created_idx
ON jobs(status, created_at, job_id);

CREATE INDEX IF NOT EXISTS jobs_type_created_idx
ON jobs(job_type, created_at, job_id);

CREATE TABLE IF NOT EXISTS job_events (
  job_event_id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  job_id text NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  event_type text NOT NULL,
  message text NOT NULL,
  detail jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS job_events_job_created_idx
ON job_events(job_id, created_at, job_event_id);
