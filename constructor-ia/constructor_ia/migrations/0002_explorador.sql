-- Explorador: mapas persistentes por conexión (la base explorada nunca se modifica).
CREATE TABLE explore_snapshots (
    id          INTEGER PRIMARY KEY,
    conn        TEXT NOT NULL,
    mission_id  TEXT,
    fingerprint TEXT NOT NULL,
    schema_json TEXT NOT NULL,
    changes_json TEXT,
    observed_at REAL NOT NULL
);
CREATE INDEX explore_snapshots_conn ON explore_snapshots(conn, id);

CREATE TABLE explore_queries (
    id          INTEGER PRIMARY KEY,
    conn        TEXT NOT NULL,
    mission_id  TEXT,
    step        INTEGER,
    origin      TEXT NOT NULL,
    tool        TEXT NOT NULL,
    question    TEXT,
    rationale   TEXT,
    sql_text    TEXT NOT NULL,
    status      TEXT NOT NULL,
    row_count   INTEGER,
    bytes       INTEGER,
    ms          INTEGER,
    truncated   INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    error       TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX explore_queries_conn ON explore_queries(conn, id);
CREATE INDEX explore_queries_mission ON explore_queries(mission_id, id);

CREATE TABLE explore_facts (
    id          INTEGER PRIMARY KEY,
    conn        TEXT NOT NULL,
    mission_id  TEXT,
    kind        TEXT NOT NULL,
    certainty   TEXT NOT NULL,
    subject     TEXT NOT NULL,
    text        TEXT NOT NULL,
    data_json   TEXT,
    evidence_json TEXT,
    status      TEXT NOT NULL DEFAULT 'vigente',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX explore_facts_conn ON explore_facts(conn, kind, status);

CREATE TABLE explore_steps (
    id          INTEGER PRIMARY KEY,
    mission_id  TEXT NOT NULL,
    conn        TEXT NOT NULL,
    n           INTEGER NOT NULL,
    question    TEXT,
    rationale   TEXT,
    evidence_json TEXT,
    tool        TEXT,
    args_json   TEXT,
    query_id    INTEGER,
    analysis    TEXT,
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX explore_steps_mission ON explore_steps(mission_id, n);
