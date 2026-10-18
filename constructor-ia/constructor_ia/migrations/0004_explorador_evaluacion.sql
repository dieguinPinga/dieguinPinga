-- Explorador 0.4.1: separar finalización del proceso, calidad del resultado y cumplimiento del objetivo.
CREATE TABLE explore_runs (
    mission_id      TEXT PRIMARY KEY,
    conn            TEXT NOT NULL,
    ended_reason    TEXT NOT NULL,
    quality         TEXT NOT NULL,
    objective_met   INTEGER NOT NULL,
    report_status   TEXT NOT NULL,
    elapsed_s       REAL NOT NULL,
    budget_s        REAL NOT NULL,
    metrics_json    TEXT NOT NULL,
    reasons_json    TEXT NOT NULL,
    created_at      REAL NOT NULL
);
-- Diagnóstico de respuestas del modelo: reparaciones de argumentos y texto crudo de las inválidas.
ALTER TABLE explore_steps ADD COLUMN repairs INTEGER NOT NULL DEFAULT 0;
ALTER TABLE explore_steps ADD COLUMN raw TEXT;
