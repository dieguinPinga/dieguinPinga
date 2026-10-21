"""Corrida completa del laboratorio: para cada modelo y repetición, una base sintética idéntica (misma semilla), un
almacén propio (un modelo no reutiliza instrumentos de otro) y las mismas tareas, prompts y presupuestos."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .. import VERSION
from ..config import ExplorerConnection
from ..db import Database
from ..explorer.broker import Broker
from ..instrumentos.almacen import Almacen
from ..instrumentos.canal import Presupuesto
from ..instrumentos.medicion import observar_esquema
from . import sintetica
from .protocolo import LaboratorioAbortado, Modelo, Sesion, resumen

ESCALAS = {
    "pequena": {"dias": 30, "por_dia": 500},       # ~15 mil lecturas (pruebas rápidas)
    "normal": {"dias": 60, "por_dia": 2000},       # ~120 mil: más que el presupuesto de filas → obliga a agregar
    "grande": {"dias": 90, "por_dia": 10000},      # ~900 mil
}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:40] or "modelo"


def correr(cfg, *, executor, servidor, modelos: list[Modelo], tareas, repeticiones: int = 1, semilla: int = 7,
           escala: str = "normal", salida: Path, max_correcciones: int = 3, reparaciones_formato: int = 1,
           max_mismo_error: int = 3, cancel=None, log=print) -> dict:
    salida = Path(salida)
    salida.mkdir(parents=True, exist_ok=True, mode=0o700)
    dims = ESCALAS[escala] if isinstance(escala, str) else dict(escala)
    out = {"version": VERSION, "inicio": time.time(), "semilla": semilla,
           "escala": escala if isinstance(escala, str) else "personalizada", **dims,
           "tareas": [t.id for t in tareas], "repeticiones": repeticiones, "max_correcciones": max_correcciones,
           "reparaciones_formato": reparaciones_formato, "max_mismo_error": max_mismo_error,
           "presupuesto": Presupuesto.from_cfg(cfg).as_dict(), "modelos": [m.opciones() for m in modelos],
           "sesiones": [], "estado": "en_curso"}
    try:
        for mi, modelo in enumerate(modelos):
            for rep in range(1, repeticiones + 1):
                log(f"== {modelo.nombre} — repetición {rep}/{repeticiones}")
                out["sesiones"].append(_sesion(cfg, executor, servidor, modelo, tareas, semilla, dims,
                                               salida / f"{_slug(modelo.nombre)}_r{rep}", f"lab_{mi}_{rep}",
                                               rep, max_correcciones, reparaciones_formato, max_mismo_error,
                                               cancel, log))
        out["estado"] = "completa"
    except LaboratorioAbortado as e:
        out["estado"], out["motivo"] = "abortada", str(e)
        log(f"Laboratorio abortado: {e}")
    out["fin"] = time.time()
    out["resumen_por_modelo"] = {m.nombre: resumen([r for s in out["sesiones"] if s["modelo"] == m.nombre
                                                    for r in s["registros"]]) for m in modelos}
    return out


def _sesion(cfg, executor, servidor, modelo, tareas, semilla, dims, directorio: Path, base: str, rep: int,
            max_correcciones, reparaciones_formato, max_mismo_error, cancel, log) -> dict:
    directorio.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = sintetica.generar(servidor, base, semilla=semilla, **dims)
    pw = directorio / "clave-desechable"
    fd = os.open(pw, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(info["clave"] + "\n")
    registros: list[dict] = []
    ses = {"modelo": modelo.nombre, "repeticion": rep,
           "base": {k: v for k, v in info.items() if k != "clave"}, "registros": registros}
    try:
        db = Database(directorio / "lab.sqlite")
        db.migrate()
        broker = Broker(cfg, ExplorerConnection("laboratorio", base, info["usuario"], str(pw),
                                                socket=servidor.socket), db)
        if not broker.verify().get("verified"):
            raise LaboratorioAbortado(f"la cuenta de solo lectura del laboratorio no se verificó: {broker.status}")
        schema = observar_esquema(broker)
        sesion = Sesion(cfg=cfg, modelo=modelo, executor=executor, broker=broker, almacen=Almacen(db, cfg),
                        server=servidor, info=info, schema=schema, max_correcciones=max_correcciones,
                        reparaciones_formato=reparaciones_formato, max_mismo_error=max_mismo_error, cancel=cancel,
                        log=log)
        for t in tareas:
            if t.id == "R":  # los datos cambian: día nuevo, registros tardíos y correcciones
                info = sintetica.avanzar(servidor, info, semilla=semilla + 1000)
                sesion.info, sesion.schema = info, observar_esquema(broker)
                ses["base_avanzada"] = {k: v for k, v in info.items() if k != "clave"}
            registros.append(sesion.correr(t, t.verdad(servidor, base, info)))
    finally:
        pw.unlink(missing_ok=True)
        servidor.sql(f"DROP DATABASE IF EXISTS `{base}`", check=False)
    ses["resumen"] = resumen(registros)
    return ses
