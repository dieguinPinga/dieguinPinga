"""Persistencia y transiciones de estado de las misiones (cola en SQLite)."""
from __future__ import annotations

import json
import secrets
import time

from .db import Database
from .events import EventBus

PENDING = "pendiente"
ACTIVE_STATES = ("preparando", "generando", "probando", "corrigiendo", "explorando")
TERMINAL_STATES = ("aprobada", "completada", "fallida", "cancelada", "interrumpida")
ALL_STATES = (PENDING, *ACTIVE_STATES, *TERMINAL_STATES)


def new_id() -> str:
    return time.strftime("M%Y%m%d-") + secrets.token_hex(3)


class Missions:
    def __init__(self, db: Database, bus: EventBus):
        self.db = db
        self.bus = bus
        self.on_approved = None  # Revisions.on_approved: actualiza la versión vigente del proyecto

    def create(self, m: dict, model: str, user: str, conversation_id: int | None = None,
               lineage: dict | None = None) -> str:
        now = time.time()
        spec = dict(m["spec"])
        spec["tests_py"] = m.get("tests_py", "")
        for _ in range(5):
            mid = new_id()
            try:
                self.db.execute(
                    "INSERT INTO missions(id, title, description, spec_json, model, max_attempts, state,"
                    " exec_authorized, created_by, conversation_id, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, m["title"], m["description"], json.dumps(spec, ensure_ascii=False), model,
                     m["max_attempts"], PENDING, 1, user, conversation_id, now, now))
                if lineage:
                    self.db.execute("UPDATE missions SET project_id=?, revision=?, parent_id=?, base_ref=?, instruction=?"
                                    " WHERE id=?", (lineage["project_id"], int(lineage["revision"]),
                                                    lineage["parent_id"], lineage["base_ref"],
                                                    lineage["instruction"], mid))
                break
            except Exception as e:  # colisión de id improbable
                if "UNIQUE" not in str(e):
                    raise
        else:
            raise RuntimeError("No se pudo generar un id de misión")
        self.bus.publish("mision_creada", f"Misión creada por {user}: {m['title']}", mission_id=mid)
        return mid

    def get(self, mid: str):
        return self.db.one("SELECT * FROM missions WHERE id=?", (mid,))

    def set_state(self, mid: str, state: str, **fields) -> None:
        assert state in ALL_STATES
        cols = ["state=?", "updated_at=?"]
        vals: list = [state, time.time()]
        for k, v in fields.items():
            assert k in ("attempt", "started_at", "finished_at", "final_attempt", "error", "claimed_by")
            cols.append(f"{k}=?")
            vals.append(v)
        vals.append(mid)
        self.db.execute(f"UPDATE missions SET {', '.join(cols)} WHERE id=?", vals)
        if state == "aprobada" and self.on_approved is not None:
            try:
                self.on_approved(mid)
            except Exception:  # noqa: BLE001 - la aprobación ya quedó registrada; no se revierte por esto
                import logging

                logging.getLogger(__name__).exception("No se pudo actualizar la versión vigente de %s", mid)
        self.bus.publish("estado", state, mission_id=mid, data={"state": state, **{
            k: v for k, v in fields.items() if k in ("attempt", "error")}})

    def claim_next(self, worker: str):
        """Toma atómicamente la misión pendiente más antigua (evita ejecuciones duplicadas)."""
        with self.db.tx() as conn:
            row = conn.execute("SELECT id FROM missions WHERE state=? AND cancel_requested=0"
                               " ORDER BY created_at LIMIT 1", (PENDING,)).fetchone()
            if row is None:
                return None
            cur = conn.execute("UPDATE missions SET state='preparando', claimed_by=?, started_at=?, updated_at=?"
                               " WHERE id=? AND state=?", (worker, time.time(), time.time(), row[0], PENDING))
            if cur.rowcount != 1:
                return None
        self.bus.publish("estado", "preparando", mission_id=row[0], data={"state": "preparando"})
        return row[0]

    def request_cancel(self, mid: str) -> str:
        """Devuelve 'cancelada' (estaba en cola), 'solicitada' (en curso) o 'no_aplica'."""
        with self.db.tx() as conn:
            row = conn.execute("SELECT state FROM missions WHERE id=?", (mid,)).fetchone()
            if row is None:
                return "no_existe"
            if row[0] == PENDING:
                conn.execute("UPDATE missions SET state='cancelada', cancel_requested=1, finished_at=?, updated_at=?"
                             " WHERE id=?", (time.time(), time.time(), mid))
                result = "cancelada"
            elif row[0] in ACTIVE_STATES:
                conn.execute("UPDATE missions SET cancel_requested=1, updated_at=? WHERE id=?", (time.time(), mid))
                result = "solicitada"
            else:
                return "no_aplica"
        if result == "cancelada":
            self.bus.publish("estado", "cancelada", mission_id=mid, data={"state": "cancelada"})
        else:
            self.bus.publish("cancelacion", "Cancelación solicitada", mission_id=mid)
        return result

    def cancel_requested(self, mid: str) -> bool:
        row = self.db.one("SELECT cancel_requested FROM missions WHERE id=?", (mid,))
        return bool(row and row[0])

    def recover_interrupted(self) -> list[str]:
        """Al arrancar: las misiones que estaban en curso quedan 'interrumpida' (no se re-ejecutan solas)."""
        rows = self.db.all(f"SELECT id FROM missions WHERE state IN ({','.join('?' * len(ACTIVE_STATES))})",
                           ACTIVE_STATES)
        ids = [r[0] for r in rows]
        for mid in ids:
            self.set_state(mid, "interrumpida", finished_at=time.time(),
                           error="El servidor se detuvo mientras la misión estaba en curso")
            self.bus.publish("error", "Misión interrumpida por reinicio del servidor; puede reencolarse",
                             mission_id=mid)
        return ids

    def requeue(self, mid: str, user: str) -> str:
        """Crea una NUEVA misión con la misma especificación (el historial original no se altera)."""
        row = self.get(mid)
        if row is None:
            raise ValueError("Misión inexistente")
        if row["state"] not in ("fallida", "cancelada", "interrumpida"):
            raise ValueError("Solo se pueden reencolar misiones fallidas, canceladas o interrumpidas")
        spec = json.loads(row["spec_json"])
        tests_py = spec.pop("tests_py", "")
        lineage = None
        if row["project_id"]:  # un reintento de una revisión sigue siendo la misma revisión del proyecto
            lineage = {k: row[k] for k in ("project_id", "revision", "parent_id", "base_ref", "instruction")}
        new = self.create({"title": row["title"][:110] + " (reintento)", "description": row["description"],
                           "spec": spec, "tests_py": tests_py, "max_attempts": row["max_attempts"]},
                          row["model"], user, row["conversation_id"], lineage=lineage)
        self.bus.publish("reencolada", f"Reencolada como {new}", mission_id=mid, data={"nueva": new})
        return new

    def counts(self) -> dict:
        rows = self.db.all("SELECT state, COUNT(*) FROM missions GROUP BY state")
        return {r[0]: r[1] for r in rows}
