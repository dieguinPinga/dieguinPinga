-- Explorador 0.4.5: procedencia e historial de cada hecho, memoria de consultas, alcance de la evidencia y
-- aporte de conocimiento. Solo agrega columnas, tablas e índices: no modifica ni borra datos existentes.

-- Procedencia: mission_id queda como la expedición que CREÓ el hecho (ya no se sobrescribe);
-- last_mission_id es la última que lo observó o confirmó.
ALTER TABLE explore_facts ADD COLUMN last_mission_id TEXT;
UPDATE explore_facts SET last_mission_id = mission_id;
-- Clave de conocimiento (tablas | columnas | operación) para comparar afirmaciones entre sí.
ALTER TABLE explore_facts ADD COLUMN clave TEXT;
CREATE INDEX idx_explore_facts_clave ON explore_facts(conn, clave);

-- Historial de cada hecho: quién lo creó, confirmó, reobservó o reafirmó, con el estado anterior y el nuevo.
CREATE TABLE explore_fact_history (
    id               INTEGER PRIMARY KEY,
    fact_id          INTEGER NOT NULL,
    conn             TEXT NOT NULL,
    mission_id       TEXT,
    event            TEXT NOT NULL,
    certainty_before TEXT,
    certainty_after  TEXT,
    text             TEXT,
    evidence_json    TEXT,
    data_json        TEXT,
    created_at       REAL NOT NULL
);
CREATE INDEX idx_explore_fact_history ON explore_fact_history(fact_id);
-- Punto de partida del historial: el estado de cada hecho al migrar (antes de 0.4.5, upsert podía haber
-- sobrescrito mission_id y certeza; esa información anterior no es recuperable y no se inventa).
INSERT INTO explore_fact_history(fact_id, conn, mission_id, event, certainty_before, certainty_after, text,
                                 evidence_json, data_json, created_at)
    SELECT id, conn, mission_id, 'estado_al_migrar_0.4.5', certainty, certainty, text, evidence_json, data_json,
           CAST(strftime('%s', 'now') AS REAL)
    FROM explore_facts;

-- Memoria de consultas: firma (SQL normalizado), firma de forma, qué buscaba la pregunta, si el resultado la
-- respondió, alcance de la evidencia y huella del resultado (para detectar repeticiones y cambios de datos).
ALTER TABLE explore_queries ADD COLUMN firma TEXT;
ALTER TABLE explore_queries ADD COLUMN firma_forma TEXT;
ALTER TABLE explore_queries ADD COLUMN busca TEXT;
ALTER TABLE explore_queries ADD COLUMN respuesta TEXT;
ALTER TABLE explore_queries ADD COLUMN alcance_json TEXT;
ALTER TABLE explore_queries ADD COLUMN resultado_hash TEXT;
CREATE INDEX idx_explore_queries_firma ON explore_queries(conn, firma);

-- Aporte de cada paso: clasificación de sus afirmaciones (nuevo, confirma, difiere, contradice, indeterminado…).
ALTER TABLE explore_steps ADD COLUMN aporte_json TEXT;
