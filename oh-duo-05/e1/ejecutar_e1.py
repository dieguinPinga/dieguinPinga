"""E1: corre P1 con el ejecutor de E1 (e1/ejecutor) y un guion de preguntas, con el orquestador de OH-DUO-01 sin
modificarlo. Es oh-duo-05/ejecutar.py más copiar guion.json a la instancia (correr.sh lo monta en solo lectura).

  python3 ejecutar_e1.py --corridas DIR ejecutar --corrida NOMBRE --guion guion.json [--semilla 11]
  python3 ejecutar_e1.py --corridas DIR verificar --corrida NOMBRE
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
RAIZ = AQUI.parent
sys.path.insert(0, str(RAIZ))
from ejecutar import hermano  # noqa: E402

EJECUTOR = AQUI / "ejecutor"
PUENTE = RAIZ / "ejecutor" / "puente.py"        # el mismo de A1/B1, sin copia propia
MARCA = ".e1"


def cargar_duo(oh_duo_01: Path, guion: Path | None):
    spec = importlib.util.spec_from_file_location("oh_duo_01_duo_e1", oh_duo_01 / "duo.py")
    duo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(duo)
    original = duo.armar_instancia

    def armar(p, clave, destino, oh_smoke, ejecutor, roles):
        original(p, clave, destino, oh_smoke, ejecutor, roles)
        shutil.copy2(PUENTE, destino / "puente.py")
        shutil.copy2(guion, destino / "guion.json")
    if guion is not None:
        duo.armar_instancia = armar                  # solo en este proceso; duo.py no cambia
    return duo


def ejecutar(corridas: Path, nombre: str, guion: Path, semilla: int, oh_smoke: Path, oh_supervisor: Path, oh_duo_01: Path):
    duo = cargar_duo(oh_duo_01, guion)
    (corridas / nombre).mkdir(parents=True, exist_ok=True)
    (corridas / nombre / MARCA).write_text(json.dumps({"guion_sha256": hashlib.sha256(guion.read_bytes()).hexdigest(),
                                                       "guion": json.loads(guion.read_text())}) + "\n")
    return duo.ejecutar(corridas, nombre, oh_smoke, oh_supervisor, ["P1"], ["qwen"], semilla, "alterno",
                        banco=oh_duo_01 / "banco", ejecutor=EJECUTOR, roles=oh_duo_01 / "roles")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--corridas", type=Path, required=True)
    sub = p.add_subparsers(dest="comando", required=True)
    e = sub.add_parser("ejecutar")
    e.add_argument("--corrida", required=True)
    e.add_argument("--guion", type=Path, required=True)
    e.add_argument("--semilla", type=int, default=11)
    e.add_argument("--oh-smoke", type=Path, default=Path.home() / "oh-smoke")
    e.add_argument("--oh-supervisor", type=Path, default=Path.home() / "oh-supervisor")
    v = sub.add_parser("verificar")
    v.add_argument("--corrida", required=True)
    a = p.parse_args(argv)
    oh1 = hermano("oh-duo-01", "duo.py")
    if a.comando == "ejecutar":
        ejecutar(a.corridas.resolve(), a.corrida, a.guion.resolve(), a.semilla, a.oh_smoke.resolve(), a.oh_supervisor.resolve(), oh1)
    else:
        cargar_duo(oh1, None).verificar(a.corridas.resolve(), a.corrida, banco=oh1 / "banco")
    return 0


if __name__ == "__main__":
    sys.exit(main())
