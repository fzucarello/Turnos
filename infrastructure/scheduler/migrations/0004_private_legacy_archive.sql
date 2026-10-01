CREATE TABLE legacy_runs (
  run_id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  conclusion TEXT,
  head_sha TEXT,
  job_logs_json TEXT NOT NULL,
  archived_at TEXT NOT NULL,
  deleted_at TEXT
);
