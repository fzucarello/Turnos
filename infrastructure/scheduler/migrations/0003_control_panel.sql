-- OSEP Panel: configuración operativa y cola manual (sin secretos).
-- No modifica ni elimina datos de dispatches.
CREATE TABLE IF NOT EXISTS control_config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  config_json TEXT NOT NULL,
  schedule_json TEXT NOT NULL,
  paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_queue (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  submitted_by TEXT NOT NULL,
  dry_run INTEGER NOT NULL CHECK (dry_run IN (0,1)),
  state TEXT NOT NULL CHECK (state IN ('pending','dispatching','accepted','busy','rejected','uncertain','error')),
  config_json TEXT NOT NULL,
  run_id INTEGER,
  error_code TEXT
);
CREATE INDEX IF NOT EXISTS manual_queue_state_date ON manual_queue(state,created_at);
