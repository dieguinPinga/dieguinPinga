-- Instrumentos de investigación (0.6): programas Python que las IA locales escriben, versionan y reutilizan.
-- Solo agrega tablas. No modifica hechos, consultas, historial, reevaluaciones ni investigaciones existentes.

-- Instrumento: una versión es INMUTABLE (cambiar el código crea otra versión, con su anterior).
CREATE TABLE inst_instruments (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    version       INTEGER NOT NULL,
    parent_id     INTEGER,                 -- versión anterior (corrección o mejora)
    code          TEXT NOT NULL,
    code_sha256   TEXT NOT NULL,
    manifest_json TEXT NOT NULL,           -- propósito, pregunta, método, muestreo, parámetros, requisitos
    libraries_json TEXT,                   -- instrumentos anteriores que importa (id)
    author        TEXT NOT NULL,           -- modelo (p. ej. qwen3:4b-instruct) o «humano»
    mission_id    TEXT,
    status        TEXT NOT NULL DEFAULT 'borrador',  -- borrador, probado, retirado
    notes_json    TEXT,                    -- limitaciones conocidas que se acumulan
    created_at    REAL NOT NULL,
    UNIQUE(name, version)
);
CREATE INDEX idx_inst_instruments_sha ON inst_instruments(code_sha256);

-- Ejecución: información TRANSITORIA salvo que se ancle como evidencia de conocimiento.
CREATE TABLE inst_runs (
    id             INTEGER PRIMARY KEY,
    instrument_id  INTEGER NOT NULL,
    conn           TEXT NOT NULL,
    mission_id     TEXT,
    params_json    TEXT NOT NULL,
    status         TEXT NOT NULL,          -- ok, error, rechazado, timeout, bloqueado, resumida
    error          TEXT,
    output_json    TEXT,                   -- salida compacta (se descarta al caducar si no está anclada)
    output_hash    TEXT,
    output_bytes   INTEGER,
    accounting_json TEXT,                  -- consultas C#, filas, bytes, truncados, presupuestos excedidos (pedidos)
    verdict_json   TEXT,                   -- contrato, cobertura declarada frente a la real
    fingerprint_json TEXT,                 -- huella de los datos de las tablas consultadas al ejecutar
    cost_json      TEXT,                   -- cálculo, espera de MariaDB, reloj
    pinned         INTEGER NOT NULL DEFAULT 0,
    pin_reason     TEXT,
    expires_at     REAL,
    created_at     REAL NOT NULL
);
CREATE INDEX idx_inst_runs ON inst_runs(instrument_id, id);
CREATE INDEX idx_inst_runs_expiry ON inst_runs(pinned, expires_at);
