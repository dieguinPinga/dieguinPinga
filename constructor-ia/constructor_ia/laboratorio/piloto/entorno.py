"""Entorno compartido del piloto: base sintética en el MariaDB privado, conexión de solo lectura y almacenes
SEPARADOS (uno por episodio, uno para la detección y uno para el juez), para que ningún brazo vea instrumentos de
otro ni la referencia."""
from __future__ import annotations

import os
from pathlib import Path

from ...config import ExplorerConnection
from ...db import Database
from ...explorer.broker import Broker
from ...instrumentos.almacen import Almacen
from ...instrumentos.medicion import observar_esquema
from .. import sintetica

ESCALA = {"dias": 30, "por_dia": 500}   # «pequena», fijada en el preregistro
SEMILLA = 7


class Entorno:
    def __init__(self, cfg, executor, servidor, directorio: Path, *, base: str = "piloto_base", escala=ESCALA,
                 semilla: int = SEMILLA, info: dict | None = None):
        self.cfg, self.executor, self.servidor, self.dir = cfg, executor, servidor, Path(directorio)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.info = info or sintetica.generar(servidor, base, semilla=semilla, **escala)
        self.pw = self.dir / "clave-desechable"
        fd = os.open(self.pw, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.info["clave"] + "\n")
        self.conn = ExplorerConnection("piloto", self.info["base"], self.info["usuario"], str(self.pw),
                                       socket=servidor.socket)
        self.schema = None
        self._dbs: list[Database] = []
        self.schema = observar_esquema(self.almacen("esquema").broker)

    def almacen(self, nombre: str) -> Almacen:
        db = Database(self.dir / f"{nombre}.sqlite")
        db.migrate()
        self._dbs.append(db)
        broker = Broker(self.cfg, self.conn, db)
        if not broker.verify().get("verified"):
            raise RuntimeError(f"entorno: la cuenta de solo lectura no se verificó: {broker.status}")
        alm = Almacen(db, self.cfg)
        alm.broker = broker
        return alm

    def liberar(self, almacen) -> None:
        """Cierra la base SQLite de un almacén que ya no se usa (los archivos quedan para revisar)."""
        almacen.db.close()
        if almacen.db in self._dbs:
            self._dbs.remove(almacen.db)

    def cerrar(self, borrar_base: bool = True) -> None:
        for db in self._dbs:
            db.close()
        self._dbs.clear()
        self.pw.unlink(missing_ok=True)
        if borrar_base:
            self.servidor.sql(f"DROP DATABASE IF EXISTS `{self.info['base']}`", check=False)
