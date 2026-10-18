"""Almacén de archivos por misión.

Las rutas relativas las construye siempre el servidor a partir de patrones
fijos; el navegador solo puede referirse a un archivo por su id numérico.
Al leer se comprueba que la ruta real siga dentro del directorio de la misión
y se abre con O_NOFOLLOW para rechazar enlaces simbólicos.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from .db import Database

MISSION_ID_RE = re.compile(r"^M\d{8}-[0-9a-f]{6}$")
REL_RE = re.compile(r"^(?:[a-z0-9_-]{1,40}/){0,4}[A-Za-z0-9_.-]{1,64}$")
MAX_READ = 2_000_000


class FileStoreError(Exception):
    pass


class FileStore:
    def __init__(self, db: Database, root: Path):
        self.db = db
        self.root = Path(os.path.realpath(root))

    def mission_dir(self, mission_id: str) -> Path:
        if not MISSION_ID_RE.match(mission_id):
            raise FileStoreError("Id de misión inválido")
        return self.root / mission_id

    def _safe_path(self, mission_id: str, rel: str) -> Path:
        if not REL_RE.match(rel) or ".." in rel.split("/"):
            raise FileStoreError("Ruta relativa inválida")
        base = self.mission_dir(mission_id)
        p = base / rel
        # Ningún componente intermedio puede ser un enlace simbólico.
        cur = base
        for part in Path(rel).parts[:-1]:
            cur = cur / part
            if cur.is_symlink():
                raise FileStoreError("Enlace simbólico no permitido")
        return p

    def save(self, mission_id: str, rel: str, content: str | bytes, kind: str, attempt_n: int | None = None) -> int:
        p = self._safe_path(mission_id, rel)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = content.encode("utf-8") if isinstance(content, str) else content
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        digest = hashlib.sha256(data).hexdigest()
        self.db.execute(
            "INSERT INTO files(mission_id, attempt_n, rel_path, kind, size, sha256, created_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(mission_id, rel_path) DO UPDATE SET size=excluded.size, sha256=excluded.sha256,"
            " created_at=excluded.created_at, kind=excluded.kind, attempt_n=excluded.attempt_n",
            (mission_id, attempt_n, rel, kind, len(data), digest, time.time()))
        return self.db.one("SELECT id FROM files WHERE mission_id=? AND rel_path=?", (mission_id, rel))[0]

    def read(self, file_id: int) -> tuple[dict, bytes]:
        row = self.db.one("SELECT * FROM files WHERE id=?", (file_id,))
        if row is None:
            raise FileStoreError("Archivo inexistente")
        p = self._safe_path(row["mission_id"], row["rel_path"])
        base = os.path.realpath(self.mission_dir(row["mission_id"]))
        if not os.path.realpath(p).startswith(base + os.sep):
            raise FileStoreError("Ruta fuera de la misión")
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as e:
            raise FileStoreError(f"No se pudo abrir el archivo: {e.strerror}") from None
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(MAX_READ + 1)
        if len(data) > MAX_READ:
            raise FileStoreError("Archivo demasiado grande")
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise FileStoreError("El contenido no coincide con el registrado (posible alteración)")
        return dict(row), data

    def read_rel(self, mission_id: str, rel: str) -> str | None:
        row = self.db.one("SELECT id FROM files WHERE mission_id=? AND rel_path=?", (mission_id, rel))
        if row is None:
            return None
        return self.read(row[0])[1].decode("utf-8", "replace")
