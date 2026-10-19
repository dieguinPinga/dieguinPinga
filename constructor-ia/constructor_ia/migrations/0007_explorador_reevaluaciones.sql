-- Explorador 0.4.7: reevaluaciones registradas APARTE. No modifica ni borra hechos, consultas ni historial.
-- Cada reevaluación conserva lo anterior y lo nuevo, la regla que la produjo, su evidencia y quién la autorizó.
-- La certeza efectiva de un hecho es la del último evento de su historial (la reevaluación agrega un evento
-- «reevaluado»); la certeza original sigue en explore_facts.certainty.
CREATE TABLE explore_reviews (
    id               INTEGER PRIMARY KEY,
    conn             TEXT NOT NULL,
    objeto           TEXT NOT NULL,          -- 'hecho' o 'consulta'
    objeto_id        INTEGER NOT NULL,
    regla            TEXT NOT NULL,
    version          TEXT NOT NULL,
    plan             TEXT NOT NULL,          -- código del plan simulado y autorizado
    certeza_anterior TEXT,
    certeza_nueva    TEXT,
    veredicto        TEXT,
    detalle          TEXT,
    antes_json       TEXT,
    despues_json     TEXT,
    evidencia_json   TEXT,
    autorizado_por   TEXT,
    created_at       REAL NOT NULL
);
CREATE INDEX idx_explore_reviews ON explore_reviews(conn, objeto, objeto_id);
CREATE INDEX idx_explore_fact_history_last ON explore_fact_history(fact_id, id);
