-- Esquema inicial de Constructor IA.
CREATE TABLE users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    pw_hash     TEXT NOT NULL,
    disabled    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);

CREATE TABLE sessions (
    id          INTEGER PRIMARY KEY,
    token_hash  TEXT NOT NULL UNIQUE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    expires_at  REAL NOT NULL,
    ip          TEXT,
    user_agent  TEXT
);

CREATE TABLE missions (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    spec_json       TEXT NOT NULL,
    model           TEXT NOT NULL,
    max_attempts    INTEGER NOT NULL,
    state           TEXT NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    exec_authorized INTEGER NOT NULL DEFAULT 0,
    created_by      TEXT NOT NULL,
    conversation_id INTEGER,
    created_at      REAL NOT NULL,
    started_at      REAL,
    finished_at     REAL,
    updated_at      REAL NOT NULL,
    final_attempt   INTEGER,
    error           TEXT,
    claimed_by      TEXT
);
CREATE INDEX missions_state ON missions(state, created_at);

CREATE TABLE attempts (
    id              INTEGER PRIMARY KEY,
    mission_id      TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    n               INTEGER NOT NULL,
    started_at      REAL NOT NULL,
    finished_at     REAL,
    gen_ms          INTEGER,
    prompt_tokens   INTEGER,
    eval_tokens     INTEGER,
    done_reason     TEXT,
    syntax_ok       INTEGER,
    test_status     TEXT,
    passed          INTEGER,
    failed          INTEGER,
    total           INTEGER,
    result_json     TEXT,
    sandbox_json    TEXT,
    error           TEXT,
    UNIQUE (mission_id, n)
);

CREATE TABLE files (
    id          INTEGER PRIMARY KEY,
    mission_id  TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    attempt_n   INTEGER,
    rel_path    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    UNIQUE (mission_id, rel_path)
);

CREATE TABLE events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id      TEXT,
    conversation_id INTEGER,
    kind            TEXT NOT NULL,
    message         TEXT NOT NULL,
    data_json       TEXT,
    created_at      REAL NOT NULL
);
CREATE INDEX events_mission ON events(mission_id, id);

CREATE TABLE conversations (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    mission_id  TEXT,
    created_by  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'texto',
    content         TEXT NOT NULL,
    status          TEXT NOT NULL,
    model           TEXT,
    error           TEXT,
    meta_json       TEXT,
    created_at      REAL NOT NULL,
    finished_at     REAL
);
CREATE INDEX messages_conv ON messages(conversation_id, id);
CREATE INDEX messages_status ON messages(status, id);

CREATE TABLE approvals (
    id           INTEGER PRIMARY KEY,
    mission_id   TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    detail       TEXT NOT NULL,
    state        TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at REAL NOT NULL,
    decided_by   TEXT,
    decided_at   REAL,
    note         TEXT
);

CREATE TABLE audit (
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    username TEXT,
    action  TEXT NOT NULL,
    target  TEXT,
    ip      TEXT,
    detail  TEXT
);

CREATE TABLE kv (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
