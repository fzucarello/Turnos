-- Tablas privadas para descubrimiento y credenciales cifradas.
-- Nunca colocar claves ni contraseñas aquí.
CREATE TABLE IF NOT EXISTS osep_catalog(
 id INTEGER PRIMARY KEY CHECK(id=1),
 catalog_json TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS osep_vault(
 id INTEGER PRIMARY KEY CHECK(id=1),
 iv TEXT NOT NULL,
 payload TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 updated_by TEXT NOT NULL
);
ALTER TABLE manual_queue ADD COLUMN kind TEXT NOT NULL DEFAULT 'appointment';
ALTER TABLE manual_queue ADD COLUMN catalog_status TEXT;

CREATE TABLE IF NOT EXISTS osep_catalog_relations (
 service TEXT NOT NULL, zone TEXT NOT NULL, department TEXT NOT NULL,
 records_json TEXT NOT NULL, record_count INTEGER NOT NULL DEFAULT 0,
 scan_status TEXT NOT NULL DEFAULT 'complete', updated_at TEXT NOT NULL,
 PRIMARY KEY(service,zone,department)
);
CREATE INDEX IF NOT EXISTS ix_osep_catalog_relations_filter ON osep_catalog_relations(service,zone);
