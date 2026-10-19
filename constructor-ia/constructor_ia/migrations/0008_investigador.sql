-- Investigador experimental (0.5): memoria de INVESTIGACIONES, separada del Explorador.
-- Solo agrega tablas: no modifica ni borra hechos, consultas, historial ni reevaluaciones existentes.

-- Línea de investigación: el hilo que conecta preguntas sucesivas (puede cruzar varias misiones).
CREATE TABLE inv_lines (
    id          INTEGER PRIMARY KEY,
    conn        TEXT NOT NULL,
    mission_id  TEXT,                   -- misión que la abrió
    title       TEXT NOT NULL,          -- la primera pregunta de la línea
    status      TEXT NOT NULL DEFAULT 'abierta',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX idx_inv_lines ON inv_lines(conn, id);

-- Ciclo: pregunta → pedido → evidencia → lectura (interpretación y siguiente pregunta), con su costo.
CREATE TABLE inv_cycles (
    id             INTEGER PRIMARY KEY,
    conn           TEXT NOT NULL,
    mission_id     TEXT NOT NULL,
    line_id        INTEGER,
    n              INTEGER NOT NULL,     -- número de ciclo en la misión
    parent_id      INTEGER,              -- ciclo anterior de la misma línea (también entre misiones)
    question       TEXT NOT NULL,
    why            TEXT,
    origin         TEXT NOT NULL,        -- 'pedido' (pregunta inicial) o 'lectura' (formulada tras la evidencia)
    request_json   TEXT NOT NULL,        -- el pedido del modelo, tal cual
    depends_json   TEXT,                 -- referencias declaradas por el modelo
    dependency_json TEXT,                -- evaluación: declarada / comprobable / anclas
    plan_json      TEXT,                 -- operación resuelta por el motor (o aclaración)
    status         TEXT NOT NULL,        -- en_curso, evidencia, aclaracion, completo, fallo_modelo, interrumpido
    query_ids_json TEXT,
    reused_query_id INTEGER,             -- evidencia histórica reutilizada en lugar de repetir
    evidence_json  TEXT,                 -- paquete de evidencia enviado al modelo
    computed_json  TEXT,                 -- hechos calculados por el sistema (F#)
    reading        TEXT,                 -- lectura del modelo
    claims_json    TEXT,                 -- afirmaciones con su verificación (aceptadas y rechazadas)
    doubts_json    TEXT,                 -- dudas abiertas y cerradas en este ciclo
    reuse_json     TEXT,                 -- conclusiones anteriores reutilizadas (declarada / comprobable)
    next_json      TEXT,                 -- siguiente pedido formulado tras la evidencia
    cost_json      TEXT,                 -- llamadas, tokens y segundos
    created_at     REAL NOT NULL,
    finished_at    REAL
);
CREATE INDEX idx_inv_cycles ON inv_cycles(conn, line_id, id);
CREATE INDEX idx_inv_cycles_mission ON inv_cycles(mission_id, n);

-- Dudas: preguntas abiertas que surgieron de la evidencia; se cierran con el ciclo que las responde.
CREATE TABLE inv_doubts (
    id          INTEGER PRIMARY KEY,
    conn        TEXT NOT NULL,
    mission_id  TEXT,
    cycle_id    INTEGER,
    line_id     INTEGER,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'abierta',  -- abierta, cerrada_por_el_modelo, descartada
    closed_by   INTEGER,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX idx_inv_doubts ON inv_doubts(conn, status, id);

-- Sesión: estado mínimo para interrumpir y reanudar sin depender de una conversación.
CREATE TABLE inv_sessions (
    mission_id   TEXT PRIMARY KEY,
    conn         TEXT NOT NULL,
    status       TEXT NOT NULL,          -- en_curso, interrumpida, terminada, reanudada
    state_json   TEXT NOT NULL,          -- línea, último ciclo, pedido pendiente, ciclos hechos
    metrics_json TEXT,
    resumed_from TEXT,
    resumed_by   TEXT,
    updated_at   REAL NOT NULL
);
