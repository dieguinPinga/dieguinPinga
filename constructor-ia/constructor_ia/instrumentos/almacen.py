"""Almacén de instrumentos y de sus ejecuciones.

* Instrumentos: versiones INMUTABLES (mismo nombre y mismo código = la misma versión; otro código = versión nueva,
  enlazada con la anterior). Se conservan siempre: son la receta para volver a medir.
* Ejecuciones: información TRANSITORIA. Caducan por tiempo y por cuota de almacenamiento (configurables): al caducar se
  descarta la salida y se conservan sus metadatos (receta, parámetros, consultas, huella de los datos, hash de la
  salida). Una ejecución ANCLADA (evidencia indispensable de un conocimiento) no caduca: como MariaDB cambia, el
  programa y sus parámetros no siempre bastan para reproducir lo observado; la salida anclada y las consultas que la
  produjeron (explore_queries) demuestran qué se observó originalmente.
* Huella de los datos: estimación del motor por tabla consultada (filas, última actualización, tamaño). Si cambió,
  la evidencia queda «posiblemente desactualizada» (se comprueba a pedido, no ante cada dato nuevo).
"""
from __future__ import annotations

import hashlib
import json
import re
import time

NAME_RE = re.compile(r"[^a-z0-9_]+")
TABLE_RE = re.compile(r"\b(?:from|join)\s+`?([A-Za-z0-9_$]+)`?(?:\s*\.\s*`?([A-Za-z0-9_$]+)`?)?", re.I)


def clean_name(name: str) -> str:
    n = NAME_RE.sub("_", str(name or "").lower()).strip("_")[:40]
    return n if n and n[0].isalpha() else ("i_" + n)[:40]


def module_name(name: str, version: int) -> str:
    return f"{clean_name(name)}_v{int(version)}"


def _j(v):
    return json.dumps(v, ensure_ascii=False, default=str)


class Almacen:
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg

    # Instrumentos ---------------------------------------------------------------------------------
    def save(self, name: str, code: str, manifest: dict, *, author: str, mission_id: str | None = None,
             parent_id: int | None = None, libraries: list[int] | None = None) -> dict:
        name = clean_name(name)
        sha = hashlib.sha256(code.encode()).hexdigest()
        same = self.db.one("SELECT id, version FROM inst_instruments WHERE name=? AND code_sha256=?", (name, sha))
        if same:
            return {"id": same["id"], "version": same["version"], "nombre": name, "nuevo": False}
        last = self.db.one("SELECT MAX(version) FROM inst_instruments WHERE name=?", (name,))[0] or 0
        if parent_id is None and last:
            parent_id = self.db.one("SELECT id FROM inst_instruments WHERE name=? AND version=?", (name, last))[0]
        cur = self.db.execute(
            "INSERT INTO inst_instruments(name, version, parent_id, code, code_sha256, manifest_json, libraries_json,"
            " author, mission_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, last + 1, parent_id, code, sha, _j(manifest or {}), _j(libraries or []), author, mission_id,
             time.time()))
        return {"id": cur.lastrowid, "version": last + 1, "nombre": name, "nuevo": True}

    def get(self, instrument_id: int) -> dict | None:
        r = self.db.one("SELECT * FROM inst_instruments WHERE id=?", (instrument_id,))
        if r is None:
            return None
        d = dict(r)
        d["manifest"] = json.loads(d.pop("manifest_json") or "{}")
        d["libraries"] = json.loads(d.pop("libraries_json") or "[]")
        d["notes"] = json.loads(d.pop("notes_json") or "[]")
        d["modulo"] = module_name(d["name"], d["version"])
        return d

    def latest(self, name: str) -> dict | None:
        r = self.db.one("SELECT id FROM inst_instruments WHERE name=? ORDER BY version DESC LIMIT 1", (clean_name(name),))
        return self.get(r[0]) if r else None

    def index(self) -> list[dict]:
        """Índice compacto (lo que un modelo necesita para decidir reutilizar): sin el código."""
        out = []
        for r in self.db.all("SELECT id FROM inst_instruments WHERE status!='retirado' ORDER BY name, version"):
            d = self.get(r[0])
            m = d["manifest"]
            out.append({"id": d["id"], "nombre": d["name"], "version": d["version"], "modulo": d["modulo"],
                        "proposito": m.get("proposito"), "parametros": m.get("parametros"),
                        "limitaciones": d["notes"][-3:]})
        return out

    def set_status(self, instrument_id: int, status: str) -> None:
        self.db.execute("UPDATE inst_instruments SET status=? WHERE id=?", (status, instrument_id))

    def add_note(self, instrument_id: int, note: str) -> None:
        d = self.get(instrument_id)
        notes = (d["notes"] if d else []) + [note[:300]]
        self.db.execute("UPDATE inst_instruments SET notes_json=? WHERE id=?", (_j(notes[-20:]), instrument_id))

    def library_files(self, ids: list[int]) -> dict[str, str]:
        """Instrumentos anteriores como módulos importables (bib/<nombre>_v<versión>.py)."""
        files = {}
        for i in ids or []:
            d = self.get(int(i))
            if d:
                files[f"bib/{d['modulo']}.py"] = d["code"]
        return files

    # Ejecuciones ----------------------------------------------------------------------------------
    def record_run(self, instrument_id: int, conn: str, mission_id, params: dict, status: str, *, error=None,
                   output=None, accounting=None, verdict=None, fingerprint=None, cost=None) -> int:
        body = _j(output) if output is not None else None
        now = time.time()
        return self.db.execute(
            "INSERT INTO inst_runs(instrument_id, conn, mission_id, params_json, status, error, output_json,"
            " output_hash, output_bytes, accounting_json, verdict_json, fingerprint_json, cost_json, expires_at,"
            " created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (instrument_id, conn, mission_id, _j(params or {}), status, (error or "")[:3000] or None, body,
             hashlib.sha256(body.encode()).hexdigest() if body else None, len(body.encode()) if body else 0,
             _j(accounting or {}), _j(verdict or {}), _j(fingerprint or {}), _j(cost or {}),
             now + self.cfg.instrumentos_retencion_dias * 86400, now)).lastrowid

    def run(self, run_id: int) -> dict | None:
        r = self.db.one("SELECT * FROM inst_runs WHERE id=?", (run_id,))
        if r is None:
            return None
        d = dict(r)
        for k in ("params", "output", "accounting", "verdict", "fingerprint", "cost"):
            raw = d.pop(f"{k}_json")
            d[k] = json.loads(raw) if raw else None
        return d

    def pin(self, run_id: int, reason: str) -> None:
        """Ancla una ejecución como evidencia indispensable: su salida no caduca."""
        self.db.execute("UPDATE inst_runs SET pinned=1, pin_reason=?, expires_at=NULL WHERE id=?", (reason[:300], run_id))

    def unpin(self, run_id: int) -> None:
        self.db.execute("UPDATE inst_runs SET pinned=0, pin_reason=NULL, expires_at=? WHERE id=?",
                        (time.time() + self.cfg.instrumentos_retencion_dias * 86400, run_id))

    def purge(self, now: float | None = None) -> dict:
        """Caducidad: por tiempo y por cuota. Nunca toca ejecuciones ancladas ni instrumentos."""
        now = time.time() if now is None else now
        by_time = self.db.execute(
            "UPDATE inst_runs SET output_json=NULL, status='resumida' WHERE pinned=0 AND output_json IS NOT NULL AND"
            " expires_at IS NOT NULL AND expires_at < ?", (now,)).rowcount
        quota = self.cfg.instrumentos_cuota_mb * 1024 * 1024
        used = self.db.one("SELECT COALESCE(SUM(output_bytes), 0) FROM inst_runs WHERE pinned=0 AND output_json IS NOT"
                           " NULL")[0]
        by_quota = 0
        if used > quota:
            for r in self.db.all("SELECT id, output_bytes FROM inst_runs WHERE pinned=0 AND output_json IS NOT NULL"
                                 " ORDER BY created_at, id"):
                if used <= quota:
                    break
                self.db.execute("UPDATE inst_runs SET output_json=NULL, status='resumida' WHERE id=?", (r["id"],))
                used -= r["output_bytes"] or 0
                by_quota += 1
        return {"caducadas_por_tiempo": by_time, "caducadas_por_cuota": by_quota}

    # Huella de los datos ----------------------------------------------------------------------------
    @staticmethod
    def tables_in(sqls, known: set) -> list[str]:
        out = []
        for sql in sqls:
            for m in TABLE_RE.finditer(sql or ""):
                t = m.group(2) or m.group(1)
                if t in known and t not in out:
                    out.append(t)
        return out

    @staticmethod
    def fingerprint(broker, tables: list[str], mission_id=None) -> dict:
        """Estimación del motor por tabla (filas, última actualización, tamaño): barata y sin leer los datos."""
        if not tables:
            return {}
        names = ", ".join("'" + t.replace("\\", "\\\\").replace("'", "''") + "'" for t in tables)
        o = broker.run("SELECT TABLE_NAME, TABLE_ROWS, UPDATE_TIME, DATA_LENGTH FROM information_schema.TABLES WHERE "
                       f"TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({names})", mission_id=mission_id,
                       origin="sistema", tool="huella", question="huella de los datos")
        if o.status != "ok":
            return {"error": o.error}
        return {r[0]: {"filas_estimadas": r[1], "actualizada": str(r[2]) if r[2] is not None else None,
                       "bytes": r[3]} for r in o.rows} | {"_tomada": time.time()}

    def staleness(self, run_id: int, broker) -> dict:
        """¿Cambiaron los datos que esta ejecución observó? Compara la huella guardada con la actual."""
        run = self.run(run_id)
        old = {k: v for k, v in (run.get("fingerprint") or {}).items() if not k.startswith("_")}
        if not old or "error" in old:
            return {"estado": "sin_huella"}
        new = self.fingerprint(broker, list(old), run.get("mission_id"))
        changed = [t for t in old if new.get(t) != old[t]]
        return {"estado": "posiblemente_desactualizada" if changed else "sin_cambios_detectados", "tablas": changed,
                "nota": "la huella usa estimaciones del motor: detecta cambios, no los demuestra ni los descarta del "
                        "todo (datos corregidos en el mismo tamaño pueden no verse)"}
