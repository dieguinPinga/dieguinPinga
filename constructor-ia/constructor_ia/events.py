"""Eventos persistentes (consola de progreso) con notificación a clientes SSE."""
from __future__ import annotations

import json
import threading
import time

from .db import Database


class EventBus:
    def __init__(self, db: Database, max_per_mission: int = 2000):
        self.db = db
        self.max_per_mission = max_per_mission
        self._cond = threading.Condition()
        self._last_id = 0

    def publish(self, kind: str, message: str, mission_id: str | None = None,
                conversation_id: int | None = None, data: dict | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO events(mission_id, conversation_id, kind, message, data_json, created_at) VALUES(?,?,?,?,?,?)",
            (mission_id, conversation_id, kind, message[:2000], json.dumps(data) if data else None, time.time()))
        eid = cur.lastrowid
        if mission_id and eid % 50 == 0:
            self._trim(mission_id)
        with self._cond:
            self._last_id = max(self._last_id, eid)
            self._cond.notify_all()
        return eid

    def _trim(self, mission_id: str) -> None:
        self.db.execute(
            "DELETE FROM events WHERE mission_id=? AND id <= (SELECT id FROM events WHERE mission_id=?"
            " ORDER BY id DESC LIMIT 1 OFFSET ?)", (mission_id, mission_id, self.max_per_mission))

    def wait(self, after_id: int, timeout: float) -> None:
        with self._cond:
            if self._last_id <= after_id:
                self._cond.wait(timeout)

    def since(self, after_id: int, limit: int = 200) -> list[dict]:
        rows = self.db.all("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit))
        return [event_dict(r) for r in rows]

    def latest_id(self) -> int:
        row = self.db.one("SELECT MAX(id) FROM events")
        return row[0] or 0

    def purge_older_than(self, days: int) -> int:
        cur = self.db.execute("DELETE FROM events WHERE created_at < ?", (time.time() - days * 86400,))
        return cur.rowcount


def event_dict(r) -> dict:
    return {
        "id": r["id"], "mission_id": r["mission_id"], "conversation_id": r["conversation_id"],
        "kind": r["kind"], "message": r["message"],
        "data": json.loads(r["data_json"]) if r["data_json"] else None, "ts": r["created_at"],
    }
