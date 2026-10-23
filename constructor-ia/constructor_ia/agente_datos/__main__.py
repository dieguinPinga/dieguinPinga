"""Una misión del agente de datos en su propio proceso (la prueba de reinicio: solo sobrevive agente.db).

    python -m constructor_ia.agente_datos mision --carpeta DIR --conexion conexion.json --pregunta "…" \
        [--guion competente] [--max-llamadas 8]

conexion.json: {"name", "database", "user", "password_file", "socket"} de una cuenta de SOLO LECTURA (el
intermediario la verifica y se niega si tiene privilegios de más). En la fase 1 solo hay modelo de guion: Qwen
llega en la fase 2, en la VivoBook. Imprime en la salida estándar una línea JSON con el resumen de la misión.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import config
from ..config import ExplorerConnection
from ..explorer.broker import Broker
from .coordinador import Coordinador
from .guion import Guion
from .memoria import Memoria


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m constructor_ia.agente_datos")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mision", help="una misión: pregunta en lenguaje natural sobre una base desconocida")
    m.add_argument("--carpeta", required=True, help="carpeta del laboratorio (agente.db y misiones/)")
    m.add_argument("--conexion", required=True, help="JSON con la cuenta de solo lectura")
    m.add_argument("--pregunta", required=True)
    m.add_argument("--guion", default="competente", help="perfil del modelo de guion (fase 1, sin Qwen)")
    m.add_argument("--max-llamadas", dest="max_llamadas", type=int, default=8)
    a = ap.parse_args(argv)
    carpeta = Path(a.carpeta)
    carpeta.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg = config.load(carpeta / "sin-config.toml", base_dir=carpeta)
    conn = ExplorerConnection(**json.loads(Path(a.conexion).read_text()))
    mem = Memoria(carpeta / "agente.db")
    broker = Broker(cfg, conn, mem.db)
    coord = Coordinador(cfg, broker, mem, Guion(a.guion), conexion=conn.name, base=conn.database,
                        carpeta=carpeta / "misiones", max_llamadas=a.max_llamadas,
                        log=lambda s: print(s, file=sys.stderr))
    inf = coord.ejecutar(a.pregunta)
    mem.cerrar()
    print(json.dumps({"mision": inf["mision"], "nivel": inf["nivel"], "estado": inf["estado"],
                      "informe": str(carpeta / "misiones" / inf["mision"] / "informe.json")}, ensure_ascii=False))
    return 0 if inf["estado"] in ("completa",) else 3


if __name__ == "__main__":
    sys.exit(main())
