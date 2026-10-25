"""OH-DUO-05: corre problemas con el ejecutor de buzón, usando el orquestador de OH-DUO-01 sin modificarlo.

  python3 ejecutar.py ejecutar  --corrida NOMBRE [--problemas P1] [--modelos qwen] [--semilla 11]
  python3 ejecutar.py verificar --corrida NOMBRE

Importa oh-duo-01/duo.py y le pasa ejecutor=oh-duo-05/ejecutor y sus propias corridas (oh-duo-05/corridas/), que
nunca son las de producción de OH-DUO-01. La única adaptación es envolver duo.armar_instancia para copiar
también puente.py a la instancia (correr.sh lo monta en solo lectura). Cada corrida queda marcada con
.oh-duo-05 para que el despachador acepte escribir en sus buzones.
Solo biblioteca estándar.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
EJECUTOR = AQUI / "ejecutor"
MARCA = ".oh-duo-05"


def hermano(nombre: str, archivo: str) -> Path:
    import os
    for p in (Path(os.environ.get(nombre.upper().replace("-", "_"), "/nonexistent")), AQUI.parent / nombre,
              Path.home() / nombre):
        if (p / archivo).exists():
            return p
    raise SystemExit(f"no encuentro {nombre} (variable {nombre.upper().replace('-', '_')}, ../{nombre} o ~/{nombre})")


def cargar_duo(oh_duo_01: Path):
    spec = importlib.util.spec_from_file_location("oh_duo_01_duo", oh_duo_01 / "duo.py")
    duo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(duo)
    original = duo.armar_instancia

    def armar_con_puente(p, clave, destino, oh_smoke, ejecutor, roles):
        original(p, clave, destino, oh_smoke, ejecutor, roles)
        shutil.copy2(ejecutor / "puente.py", destino / "puente.py")
    duo.armar_instancia = armar_con_puente      # solo en este proceso; duo.py no cambia
    return duo


def ejecutar(corridas: Path, nombre: str, problemas, modelos, semilla: int, orden: str, oh_smoke: Path,
             oh_supervisor: Path, oh_duo_01: Path, **kw):
    duo = cargar_duo(oh_duo_01)
    (corridas / nombre).mkdir(parents=True, exist_ok=True)
    marca = {"ejecutor": str(EJECUTOR), "puente_sha256": hashlib.sha256((EJECUTOR / "puente.py").read_bytes()).hexdigest()}
    (corridas / nombre / MARCA).write_text(json.dumps(marca) + "\n")
    return duo.ejecutar(corridas, nombre, oh_smoke, oh_supervisor, problemas, modelos, semilla, orden,
                        banco=oh_duo_01 / "banco", ejecutor=EJECUTOR, roles=oh_duo_01 / "roles", **kw)


def verificar(corridas: Path, nombre: str, oh_duo_01: Path, **kw):
    return cargar_duo(oh_duo_01).verificar(corridas, nombre, banco=oh_duo_01 / "banco", **kw)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--corridas", type=Path, default=AQUI / "corridas")
    sub = p.add_subparsers(dest="comando", required=True)
    e = sub.add_parser("ejecutar")
    e.add_argument("--corrida", required=True)
    e.add_argument("--problemas", default="P1")
    e.add_argument("--modelos", default="qwen")
    e.add_argument("--semilla", type=int, default=11)
    e.add_argument("--orden", choices=("alterno", "bloque"), default="alterno")
    e.add_argument("--oh-smoke", type=Path, default=Path.home() / "oh-smoke")
    e.add_argument("--oh-supervisor", type=Path, default=Path.home() / "oh-supervisor")
    v = sub.add_parser("verificar")
    v.add_argument("--corrida", required=True)
    a = p.parse_args(argv)
    oh1 = hermano("oh-duo-01", "duo.py")
    if a.comando == "ejecutar":
        ejecutar(a.corridas.resolve(), a.corrida, [x for x in a.problemas.split(",") if x],
                 [x for x in a.modelos.split(",") if x], a.semilla, a.orden, a.oh_smoke.resolve(),
                 a.oh_supervisor.resolve(), oh1)
    else:
        verificar(a.corridas.resolve(), a.corrida, oh1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
