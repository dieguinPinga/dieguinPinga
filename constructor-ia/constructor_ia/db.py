"""Acceso a SQLite: conexiones por hilo, WAL y migraciones numeradas."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


class Database:
    def __init__(self, path: Path, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly  # solo lectura: no crea, no migra ni escribe (p. ej. simulaciones)
        self._local = threading.local()

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None and self.readonly:
            conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            self._local.conn = conn
        if conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # Atajos -------------------------------------------------------------
    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        return self.connect().execute(sql, params)

    def one(self, sql: str, params=()):
        return self.connect().execute(sql, params).fetchone()

    def all(self, sql: str, params=()) -> list:
        return self.connect().execute(sql, params).fetchall()

    def tx(self):
        return _Tx(self.connect())

    # Migraciones --------------------------------------------------------
    def migrate(self) -> int:
        conn = self.connect()
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        for f in files:
            num = int(f.name[:4])
            if num <= current:
                continue
            sql = f.read_text(encoding="utf-8")
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _split_sql(sql):
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {num}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            current = num
        return current

    # Valores sueltos ----------------------------------------------------
    def kv_set(self, key: str, value) -> None:
        self.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))

    def kv_get(self, key: str, default=None):
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row[0]) if row else default

    def audit(self, action: str, username: str | None = None, target: str | None = None,
              ip: str | None = None, detail: str | None = None) -> None:
        self.execute("INSERT INTO audit(ts, username, action, target, ip, detail) VALUES(?,?,?,?,?,?)",
                     (time.time(), username, action, target, ip, (detail or "")[:500]))


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def _split_sql(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
