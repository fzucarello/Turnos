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
