ALTER TABLE dispatches ADD COLUMN request_id TEXT;
ALTER TABLE dispatches ADD COLUMN config_json TEXT;
ALTER TABLE dispatches ADD COLUMN bot_status TEXT;
ALTER TABLE dispatches ADD COLUMN bot_exit_code INTEGER;
ALTER TABLE dispatches ADD COLUMN bot_log TEXT;
ALTER TABLE dispatches ADD COLUMN bot_log_truncated INTEGER;
ALTER TABLE dispatches ADD COLUMN bot_completed_at TEXT;
CREATE UNIQUE INDEX dispatches_request_id ON dispatches(request_id);
