"""Esquema SQLite y consultas compartidas por captura, web y worker."""
import json
import logging
import os
import sqlite3
import time

log = logging.getLogger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    uid               TEXT    NOT NULL UNIQUE,
    started_at        REAL    NOT NULL,              -- epoch (s), incluye pre-roll
    ended_at          REAL,                          -- epoch (s), incluye post-roll
    status            TEXT    NOT NULL DEFAULT 'recording', -- recording|processing|ready|error
    category          TEXT    NOT NULL DEFAULT 'sonido',    -- voz|movimiento_sonido|movimiento|impulsivo|sonido
    labels            TEXT    NOT NULL DEFAULT '[]', -- JSON: [{name, conf, source, note}]
    has_voice         INTEGER NOT NULL DEFAULT 0,
    has_sound         INTEGER NOT NULL DEFAULT 0,
    has_motion        INTEGER NOT NULL DEFAULT 0,
    impulsive         INTEGER NOT NULL DEFAULT 0,
    light_change      INTEGER NOT NULL DEFAULT 0,
    peak_db           REAL,                          -- dBFS, máximo RMS en ventanas de 30 ms
    mean_db           REAL,                          -- dBFS, RMS de todo el evento
    noise_floor_db    REAL,                          -- piso de ruido estimado al iniciar
    voice_ratio       REAL    NOT NULL DEFAULT 0,    -- proporción VAD (no es probabilidad)
    motion_max        REAL    NOT NULL DEFAULT 0,    -- fracción máxima de imagen con cambio
    motion_ratio      REAL    NOT NULL DEFAULT 0,    -- fracción del tiempo con movimiento
    transcript        TEXT,
    transcript_status TEXT    NOT NULL DEFAULT 'none', -- none|pending|processing|done|empty|error
    transcript_lang   TEXT,
    starred           INTEGER NOT NULL DEFAULT 0,
    continuation_of   INTEGER REFERENCES events(id) ON DELETE SET NULL,
    error             TEXT,
    updated_at        REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_started ON events(started_at);
CREATE INDEX IF NOT EXISTS ix_events_updated ON events(updated_at);
CREATE INDEX IF NOT EXISTS ix_events_tstatus ON events(transcript_status);

CREATE TABLE IF NOT EXISTS media (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL CHECK (kind IN ('audio', 'video', 'thumb')),
    relpath    TEXT    NOT NULL UNIQUE,              -- relativo al directorio de datos
    mime       TEXT    NOT NULL,
    bytes      INTEGER NOT NULL,
    duration   REAL,
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_media_event ON media(event_id);

CREATE TABLE IF NOT EXISTS status (
    component  TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1');
"""

TYPE_FILTERS = {
    "voz": "has_voice = 1",
    "sonido": "has_sound = 1",
    "movimiento": "has_motion = 1",
    "impulsivo": "impulsive = 1",
    "favoritos": "starred = 1",
}


def ai_enabled(conn):
    """Interruptor de IA local (Whisper) que se controla desde el dashboard."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'ai_enabled'").fetchone()
    return row is None or row[0] != "0"


def set_ai_enabled(conn, enabled):
    conn.execute("INSERT INTO meta(key, value) VALUES ('ai_enabled', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", ("1" if enabled else "0",))


def connect(path):
    conn = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init(path):
    conn = connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def insert_event(conn, uid, started_at, continuation_of=None):
    cur = conn.execute(
        "INSERT INTO events(uid, started_at, status, continuation_of, updated_at) VALUES (?,?,?,?,?)",
        (uid, started_at, "recording", continuation_of, time.time()),
    )
    return cur.lastrowid


def update_event(conn, event_id, **fields):
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE events SET {cols} WHERE id = ?", (*fields.values(), event_id))


def add_media(conn, event_id, kind, relpath, mime, nbytes, duration=None):
    conn.execute(
        "INSERT INTO media(event_id, kind, relpath, mime, bytes, duration, created_at) VALUES (?,?,?,?,?,?,?)",
        (event_id, kind, relpath, mime, nbytes, duration, time.time()),
    )


def set_status(conn, component, state, detail=None):
    conn.execute(
        "INSERT INTO status(component, state, detail, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(component) DO UPDATE SET state=excluded.state, detail=excluded.detail, updated_at=excluded.updated_at",
        (component, state, json.dumps(detail or {}), time.time()),
    )


def recover_interrupted(conn):
    """Eventos que quedaron abiertos por un corte: se marcan como error, sin borrar nada."""
    n = conn.execute(
        "UPDATE events SET status='error', error='Interrumpido por reinicio de la captura', "
        "ended_at=COALESCE(ended_at, started_at), updated_at=? WHERE status IN ('recording','processing')",
        (time.time(),),
    ).rowcount
    if n:
        log.warning("%d evento(s) interrumpidos marcados como error", n)


def media_for(conn, event_ids):
    out = {i: {} for i in event_ids}
    if not event_ids:
        return out
    q = ",".join("?" * len(event_ids))
    for m in conn.execute(f"SELECT * FROM media WHERE event_id IN ({q})", list(event_ids)):
        out[m["event_id"]][m["kind"]] = {
            "id": m["id"], "url": f"/api/media/{m['id']}", "mime": m["mime"],
            "bytes": m["bytes"], "duration": m["duration"],
        }
    return out


def event_dict(row, media):
    d = dict(row)
    d["labels"] = json.loads(d["labels"] or "[]")
    d["duration"] = (d["ended_at"] - d["started_at"]) if d["ended_at"] else None
    d["media"] = media
    for k in ("has_voice", "has_sound", "has_motion", "impulsive", "light_change", "starred"):
        d[k] = bool(d[k])
    return d


def get_event(conn, event_id):
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        return None
    return event_dict(row, media_for(conn, [event_id])[event_id])


def list_events(conn, before_id=None, limit=30, etype="", date_from=None, date_to=None, q="", since_updated=None):
    where, args = [], []
    if before_id:
        where.append("id < ?"); args.append(int(before_id))
    if etype:
        if etype not in TYPE_FILTERS:
            raise ValueError("tipo inválido")
        where.append(TYPE_FILTERS[etype])
    if date_from is not None:
        where.append("started_at >= ?"); args.append(date_from)
    if date_to is not None:
        where.append("started_at < ?"); args.append(date_to)
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("transcript LIKE ? ESCAPE '\\'"); args.append(f"%{esc}%")
    if since_updated is not None:
        where.append("updated_at > ?"); args.append(since_updated)
    sql = "SELECT * FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    rows = conn.execute(sql, args).fetchall()
    media = media_for(conn, [r["id"] for r in rows])
    return [event_dict(r, media[r["id"]]) for r in rows]


def delete_event(conn, paths, event_id):
    """Borra archivos (validando rutas) y luego la fila. Devuelve bytes liberados."""
    freed = 0
    for m in conn.execute("SELECT relpath FROM media WHERE event_id = ?", (event_id,)).fetchall():
        try:
            p = paths.abs(m["relpath"])
            freed += os.path.getsize(p)
            os.remove(p)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.error("No se pudo borrar %s: %s", m["relpath"], e)
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    return freed
