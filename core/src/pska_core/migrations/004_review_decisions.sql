CREATE INDEX IF NOT EXISTS review_items_status_created_idx ON review_items(status, created_at);
CREATE INDEX IF NOT EXISTS audit_events_target_created_idx ON audit_events(target_type, target_id, created_at);
