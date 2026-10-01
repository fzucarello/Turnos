CREATE TABLE IF NOT EXISTS dispatches (
  slot TEXT PRIMARY KEY,
  scheduled_at TEXT NOT NULL,
  cron TEXT NOT NULL,
  mode TEXT NOT NULL,
  state TEXT NOT NULL,
  http_status INTEGER,
  run_id INTEGER,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dispatches_scheduled_at ON dispatches(scheduled_at);
