"""Punto de entrada:  python -m vigd -c CONFIG {all|capture|web|worker|diag|hash-password}"""
import argparse
import getpass
import logging
import os
import signal
import subprocess
import sys
import time

from . import config

log = logging.getLogger("supervisor")
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def supervise(cfg, cfg_path):
    """Lanza captura, web y worker como procesos separados y los reinicia si caen."""
    env = dict(os.environ, PYTHONPATH=PROJECT_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))
    worker_py = cfg["transcription"]["python"] or sys.executable
    specs = {
        "capture": [sys.executable, "-m", "vigd", "-c", cfg_path, "capture"],
        "web": [sys.executable, "-m", "vigd", "-c", cfg_path, "web"],
        "worker": [worker_py, "-m", "vigd", "-c", cfg_path, "worker"],
    }
    procs, started, backoff = {}, {}, {k: 1 for k in specs}
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while not stopping:
        now = time.time()
        for name, cmd in specs.items():
            p = procs.get(name)
            if p is not None and p.poll() is None:
                continue
            if p is not None:
                log.error("Proceso %s terminó con código %s", name, p.returncode)
                backoff[name] = 1 if now - started[name] > 60 else min(backoff[name] * 2, 60)
                procs[name] = None
                started[name] = now + backoff[name]  # momento de reintento
                continue
            if now >= started.get(name, 0):
                log.info("Iniciando %s", name)
                started[name] = now
                try:
                    procs[name] = subprocess.Popen(cmd, cwd=PROJECT_DIR, env=env)
                except OSError as e:
                    log.error("No se pudo iniciar %s (%s): %s", name, cmd[0], e)
                    started[name] = now + 60
        time.sleep(1)

    log.info("Deteniendo procesos...")
    for p in procs.values():
        if p and p.poll() is None:
            p.send_signal(signal.SIGTERM)
    deadline = time.time() + 70
    for p in procs.values():
        if p:
            try:
                p.wait(max(1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                p.kill()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vigd", description="Estación de vigilancia audiovisual doméstica")
    ap.add_argument("-c", "--config", default=os.environ.get("VIGD_CONFIG", "config.toml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("all", help="captura + web + worker supervisados")
    sub.add_parser("capture", help="sólo captura y detección")
    sub.add_parser("web", help="sólo el dashboard")
    sub.add_parser("worker", help="sólo transcripción")
    sub.add_parser("hash-password", help="genera el hash para web.password_hash")
    d = sub.add_parser("diag", help="diagnóstico del sistema")
    d.add_argument("--mic", type=int, default=0, metavar="SEG", help="medir el micrófono SEG segundos")
    d.add_argument("--cam", action="store_true", help="capturar un cuadro de prueba de la cámara")
    args = ap.parse_args(argv)
    os.umask(0o077)

    if args.cmd == "hash-password":
        a = getpass.getpass("Contraseña nueva: ")
        b = getpass.getpass("Repetir: ")
        if a != b or len(a) < 10:
            sys.exit("Las contraseñas no coinciden o tienen menos de 10 caracteres")
        from .auth import hash_password
        print(f'password_hash = "{hash_password(a)}"')
        return

    cfg_path = os.path.abspath(args.config)
    if not os.path.exists(cfg_path):
        sys.exit(f"No existe {cfg_path}. Copiá config.example.toml y editalo.")
    cfg = config.load(cfg_path)

    if args.cmd == "diag":
        from .diag import run
        return run(cfg, args)

    config.Paths(cfg["general"]["data_dir"]).ensure()
    from .util import setup_logging
    setup_logging(cfg, "supervisor" if args.cmd == "all" else args.cmd)
    if args.cmd == "all":
        return supervise(cfg, cfg_path)
    if args.cmd == "capture":
        from .capture import run
    elif args.cmd == "web":
        from .web import run
    else:
        from .worker import run
    try:
        run(cfg)
    except KeyboardInterrupt:  # Ctrl+C en la terminal llega a todo el grupo de procesos
        pass


if __name__ == "__main__":
    main()
