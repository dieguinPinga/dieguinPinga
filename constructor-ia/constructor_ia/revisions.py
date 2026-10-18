"""Revisiones de proyecto: continuar una misión terminada con nuevas instrucciones humanas.

Conceptos SEPARADOS:
* Intento   — reparación automática DENTRO de una misión (errores de pruebas → nuevo intento).
* Revisión  — misión NUEVA vinculada a un proyecto, creada por una instrucción humana sobre una
              versión base (la final aprobada o un intento concreto). Tiene sus propios intentos.

Un proyecto se identifica por su misión original (revisión 1). Cada revisión guarda:
project_id, revision (número), parent_id (misión base), base_ref («final» o «intento-N») e instruction.
La versión base se COPIA a la revisión como `base/…` (inmutable); las misiones anteriores nunca se tocan.
La «versión vigente» del proyecto es la última aprobada (o la que el operador elija entre las aprobadas):
si una revisión falla, la vigente no cambia.
"""
from __future__ import annotations

import difflib
import json
import re
import time

from .acceptance import SpecError, validate_mission
from .db import Database
from .events import EventBus
from .files import MISSION_ID_RE, FileStore
from .webproject import valid_path

REF_RE = re.compile(r"^(final|base|intento-([1-9]\d?))$")
REVISABLE_MODES = ("funcion", "script", "web")
TERMINAL = ("aprobada", "fallida", "cancelada", "interrumpida")


class RevisionError(ValueError):
    pass


class Revisions:
    def __init__(self, db: Database, files: FileStore, bus: EventBus):
        self.db, self.files, self.bus = db, files, bus

    # Proyecto ----------------------------------------------------------
    @staticmethod
    def project_of(row) -> str:
        return row["project_id"] or row["id"]

    def _mission(self, mid: str):
        if not isinstance(mid, str) or not MISSION_ID_RE.match(mid):
            raise RevisionError("Misión inexistente")
        row = self.db.one("SELECT * FROM missions WHERE id=?", (mid,))
        if row is None:
            raise RevisionError("Misión inexistente")
        return row

    def ensure_project(self, project_id: str) -> None:
        if self.db.one("SELECT 1 FROM projects WHERE id=?", (project_id,)):
            return
        root = self._mission(project_id)
        now = time.time()
        current = project_id if root["state"] == "aprobada" else None
        self.db.execute("INSERT OR IGNORE INTO projects(id, title, current_mission, created_at, updated_at)"
                        " VALUES(?,?,?,?,?)", (project_id, root["title"], current, root["created_at"], now))

    def log(self, project_id: str, kind: str, detail: str, mission_id: str | None = None, user: str | None = None):
        self.db.execute("INSERT INTO project_events(project_id, mission_id, kind, detail, username, created_at)"
                        " VALUES(?,?,?,?,?,?)", (project_id, mission_id, kind, detail[:1000], user, time.time()))

    def on_approved(self, mid: str) -> None:
        """La última misión aprobada pasa a ser la versión vigente de su proyecto."""
        row = self._mission(mid)
        pid = self.project_of(row)
        self.ensure_project(pid)
        self.db.execute("UPDATE projects SET current_mission=?, updated_at=? WHERE id=?", (mid, time.time(), pid))
        self.log(pid, "vigente", f"r{row['revision']} aprobada y vigente", mid)

    def set_current(self, project_id: str, mid: str, user: str) -> None:
        row = self._mission(mid)
        if self.project_of(row) != project_id:
            raise RevisionError("La misión no pertenece a este proyecto")
        if row["state"] != "aprobada":
            raise RevisionError("Solo una versión aprobada puede ser la vigente")
        self.ensure_project(project_id)
        self.db.execute("UPDATE projects SET current_mission=?, updated_at=? WHERE id=?", (mid, time.time(), project_id))
        self.log(project_id, "vigente", f"el operador eligió r{row['revision']} ({mid}) como versión vigente", mid, user)

    def project(self, project_id: str) -> dict:
        root = self._mission(project_id)
        if root["project_id"] and root["project_id"] != root["id"]:
            raise RevisionError("No es la misión original del proyecto")
        self.ensure_project(project_id)
        p = dict(self.db.one("SELECT * FROM projects WHERE id=?", (project_id,)))
        revs = []
        for r in self.db.all("SELECT id, revision, parent_id, base_ref, instruction, state, created_at, started_at,"
                             " finished_at, final_attempt, attempt, max_attempts, error, created_by,"
                             " json_extract(spec_json, '$.mode') AS mode FROM missions"
                             " WHERE id=? OR project_id=? ORDER BY revision, created_at", (project_id, project_id)):
            d = dict(r)
            d["vigente"] = d["id"] == p["current_mission"]
            revs.append(d)
        p["revisions"] = revs
        p["events"] = [dict(r) for r in self.db.all(
            "SELECT * FROM project_events WHERE project_id=? ORDER BY id DESC LIMIT 100", (project_id,))]
        return p

    # Versiones -----------------------------------------------------------
    def version_files(self, mid: str, ref: str) -> dict[str, str]:
        """Archivos de una versión: «final», «base» o «intento-N» de una misión."""
        if not REF_RE.match(ref or ""):
            raise RevisionError("Versión inválida")
        row = self._mission(mid)
        mode = json.loads(row["spec_json"]).get("mode", "funcion")
        out: dict[str, str] = {}
        if mode == "web":
            prefix = f"{ref}/web/"
            for r in self.db.all("SELECT rel_path FROM files WHERE mission_id=? AND substr(rel_path, 1, ?)=?",
                                 (mid, len(prefix), prefix)):
                rel = r[0][len(prefix):]
                if valid_path(rel):
                    out[rel] = self.files.read_rel(mid, r[0])
        else:
            code = self.files.read_rel(mid, f"{ref}/solucion.py")
            if code is not None:
                out["solucion.py"] = code
        return out

    def default_base(self, row) -> str:
        if row["final_attempt"]:
            return "final"
        for r in self.db.all("SELECT n FROM attempts WHERE mission_id=? ORDER BY n DESC", (row["id"],)):
            if self.version_files(row["id"], f"intento-{r[0]}"):
                return f"intento-{r[0]}"
        raise RevisionError("La misión no tiene ninguna versión con archivos sobre la cual trabajar")

    def history(self, row, limit: int = 8) -> list[dict]:
        """Instrucciones de las revisiones anteriores en la línea de esta versión (para dar contexto)."""
        out, cur, seen = [], row, set()
        while cur is not None and cur["id"] not in seen and len(out) < limit:
            seen.add(cur["id"])
            if cur["instruction"]:
                out.append({"revision": cur["revision"], "instruccion": cur["instruction"][:400]})
            cur = self.db.one("SELECT * FROM missions WHERE id=?", (cur["parent_id"],)) if cur["parent_id"] else None
        return list(reversed(out))

    # Crear una revisión ------------------------------------------------------
    def prepare(self, base_mid: str, body: dict, max_attempts_limit: int, default_attempts: int) -> dict:
        """Valida y arma la nueva revisión (spec heredada + pruebas nuevas). No escribe nada."""
        base = self._mission(base_mid)
        if base["state"] not in TERMINAL:
            raise RevisionError("Solo se pueden mejorar misiones terminadas")
        spec = json.loads(base["spec_json"])
        mode = spec.get("mode", "funcion")
        if mode not in REVISABLE_MODES:
            raise RevisionError("Este tipo de misión no admite revisiones (las exploraciones se continúan)")
        instruction = str(body.get("instruction", "")).replace("\x00", "").strip()
        if not 5 <= len(instruction) <= 4000:
            raise RevisionError("Escribí la modificación pedida (entre 5 y 4000 caracteres)")
        base_ref = body.get("base_ref") or self.default_base(base)
        if not REF_RE.match(str(base_ref)) or base_ref == "base":
            raise RevisionError("Versión base inválida")
        files = self.version_files(base_mid, base_ref)
        if not files:
            raise RevisionError(f"La versión «{base_ref}» no tiene archivos")
        new_cases = body.get("cases") or []
        new_checks = body.get("checks") or []
        extra_tests = (body.get("tests_py") or "").strip()
        if not isinstance(new_cases, list) or not isinstance(new_checks, list):
            raise RevisionError("Pruebas nuevas inválidas")
        has_new = bool(new_cases or new_checks or extra_tests)
        inherited_tests = spec.get("tests_py", "") or ""
        data = {
            "title": base["title"].replace(" (reintento)", "")[:120], "description": base["description"],
            "mode": mode, "function": spec.get("function", ""),
            "cases": list(spec.get("cases", [])) + (new_cases if mode != "web" else []),
            "checks": list(spec.get("checks", [])) + (new_checks if mode == "web" else []),
            "tests_py": (inherited_tests + ("\n\n" + extra_tests if extra_tests else "")).strip(),
            "float_tol": spec.get("float_tol", 1e-9),
            "max_attempts": body.get("max_attempts", default_attempts),
            "authorize_execution": body.get("authorize_execution"),
            "cases_reviewed": True if not has_new else body.get("cases_reviewed"),
        }
        try:
            m = validate_mission(data, max_attempts_limit, default_attempts)
        except SpecError as e:
            raise RevisionError(str(e)) from None
        for key in ("seed_files", "data_source", "entry"):
            if key in spec:
                m["spec"][key] = spec[key]
        pid = self.project_of(base)
        m["spec"]["revision"] = {
            "instruction": instruction, "base_mission": base_mid, "base_ref": base_ref, "project": pid,
            "inherited_cases": len(spec.get("cases", [])), "inherited_checks": len(spec.get("checks", [])),
            "history": self.history(base),
        }
        number = (self.db.one("SELECT MAX(revision) FROM missions WHERE id=? OR project_id=?", (pid, pid))[0] or 1) + 1
        return {"mission": m, "files": files, "lineage": {"project_id": pid, "revision": number, "parent_id": base_mid,
                                                          "base_ref": base_ref, "instruction": instruction}}

    def store_base(self, mid: str, mode: str, files: dict[str, str]) -> None:
        """Copia inmutable de la versión base dentro de la revisión."""
        for path, content in files.items():
            rel = f"base/web/{path}" if mode == "web" else "base/solucion.py"
            self.files.save(mid, rel, content, "base")

    def base_files(self, mid: str) -> dict[str, str]:
        return self.version_files(mid, "base")

    # Comparar ------------------------------------------------------------------
    def diff(self, a: str, b: str) -> dict:
        """a, b: «MISION:ref». Ambas versiones deben pertenecer al mismo proyecto."""
        (ma, ra), (mb, rb) = (self._split(a), self._split(b))
        rowa, rowb = self._mission(ma), self._mission(mb)
        if self.project_of(rowa) != self.project_of(rowb):
            raise RevisionError("Solo se comparan versiones del mismo proyecto")
        fa, fb = self.version_files(ma, ra), self.version_files(mb, rb)
        if not fa and not fb:
            raise RevisionError("Ninguna de las versiones tiene archivos")
        out = []
        for path in sorted(set(fa) | set(fb)):
            if path not in fa:
                status = "agregado"
            elif path not in fb:
                status = "eliminado"
            elif fa[path] == fb[path]:
                status = "igual"
            else:
                status = "modificado"
            lines = [] if status == "igual" else list(difflib.unified_diff(
                (fa.get(path) or "").splitlines(), (fb.get(path) or "").splitlines(), f"{a}/{path}", f"{b}/{path}",
                lineterm=""))[:3000]
            out.append({"path": path, "status": status, "diff": lines})
        return {"a": a, "b": b, "files": out}

    @staticmethod
    def _split(v: str) -> tuple[str, str]:
        if not isinstance(v, str) or ":" not in v:
            raise RevisionError("Versión inválida (formato MISION:ref)")
        mid, ref = v.split(":", 1)
        if not MISSION_ID_RE.match(mid) or not REF_RE.match(ref):
            raise RevisionError("Versión inválida")
        return mid, ref
