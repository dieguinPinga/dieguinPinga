"""Una misión del agente de datos en su propio proceso (la prueba de reinicio: solo sobrevive agente.db).

    python -m constructor_ia.agente_datos mision --carpeta DIR --conexion conexion.json --pregunta "…" \
        [--modelo guion --guion competente | --modelo qwen --ollama-url URL --nombre-modelo qwen3:4b-instruct]

conexion.json: {"name", "database", "user", "password_file", "socket"} de una cuenta de SOLO LECTURA (el
intermediario la verifica y se niega si tiene privilegios de más). Este proceso NO recibe la verdad del laboratorio
ni su ruta. Imprime en la salida estándar una línea JSON con el resumen de la misión.

Código de salida: 0 completa · 3 bloqueada o error · 4 interrumpida (sin resultado).
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from .. import config
from ..config import ExplorerConnection
from ..explorer.broker import Broker
from .coordinador import Coordinador
from .guion import Guion
from .memoria import Memoria

SALIDA = {"completa": 0, "bloqueada": 3, "error": 3, "interrumpida": 4}


def _interrumpir(signum, frame):
    """Ctrl-C, el límite del script protegido o la muerte del orquestador: la misión se cierra como INTERRUMPIDA
    (el coordinador lo registra). Las señales siguientes se ignoran para no cortar ese cierre."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise KeyboardInterrupt


def _escribir(texto: str, flujo) -> None:
    """Si el orquestador murió, sus tuberías ya no tienen lector: el mensaje se pierde, pero la misión igual se
    cierra y deja su informe en disco."""
    try:
        print(texto, file=flujo, flush=True)
    except (BrokenPipeError, OSError):
        pass


def main(argv=None) -> int:
    signal.signal(signal.SIGINT, _interrumpir)
    signal.signal(signal.SIGTERM, _interrumpir)
    ap = argparse.ArgumentParser(prog="python -m constructor_ia.agente_datos")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mision", help="una misión: pregunta en lenguaje natural sobre una base desconocida")
    m.add_argument("--carpeta", required=True, help="carpeta del laboratorio (agente.db y misiones/)")
    m.add_argument("--conexion", required=True, help="JSON con la cuenta de solo lectura")
    m.add_argument("--pregunta", required=True)
    m.add_argument("--modelo", choices=["guion", "qwen"], default="guion")
    m.add_argument("--guion", default="competente", help="perfil del modelo de guion (sin Qwen)")
    m.add_argument("--ollama-url", dest="ollama_url", default="http://127.0.0.1:11435")
    m.add_argument("--nombre-modelo", dest="nombre_modelo", default="qwen3:4b-instruct")
    m.add_argument("--num-ctx", dest="num_ctx", type=int, default=4096)
    m.add_argument("--keep-alive", dest="keep_alive", default="0s")
    m.add_argument("--memoria-minima-mb", dest="memoria_minima_mb", type=int, default=3900)
    m.add_argument("--max-llamadas", dest="max_llamadas", type=int, default=8)
    a = ap.parse_args(argv)
    carpeta = Path(a.carpeta)
    carpeta.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg = config.load(carpeta / "sin-config.toml", base_dir=carpeta)
    conn = ExplorerConnection(**json.loads(Path(a.conexion).read_text()))
    mem = Memoria(carpeta / "agente.db")
    broker = Broker(cfg, conn, mem.db)
    if a.modelo == "qwen":
        from . import qwen
        modelo = qwen.crear(cfg, a.ollama_url, a.nombre_modelo, num_ctx=a.num_ctx, keep_alive=a.keep_alive,
                            memoria_minima_mb=a.memoria_minima_mb)
    else:
        modelo = Guion(a.guion)
    coord = Coordinador(cfg, broker, mem, modelo, conexion=conn.name, base=conn.database,
                        carpeta=carpeta / "misiones", max_llamadas=a.max_llamadas,
                        log=lambda s: _escribir(s, sys.stderr))
    inf = coord.ejecutar(a.pregunta)
    mem.cerrar()
    _escribir(json.dumps({"mision": inf["mision"], "nivel": inf["nivel"], "estado": inf["estado"],
                          "informe": str(carpeta / "misiones" / inf["mision"] / "informe.json")}, ensure_ascii=False),
              sys.stdout)
    return SALIDA.get(inf["estado"], 3)


if __name__ == "__main__":
    sys.exit(main())
