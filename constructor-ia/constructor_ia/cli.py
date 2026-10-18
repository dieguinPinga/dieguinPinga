"""Línea de comandos: serve, adduser, passwd, selftest, backup, check-config."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

from . import VERSION, config, logsetup

log = logging.getLogger("constructor_ia")


def _cfg(args) -> config.Config:
    try:
        return config.load(Path(args.config) if args.config else None)
    except config.ConfigError as e:
        print(f"Error de configuración: {e}", file=sys.stderr)
        raise SystemExit(2)


def cmd_serve(args) -> int:
    cfg = _cfg(args)
    os.umask(0o077)
    logsetup.setup(cfg.log_dir, cfg.log_max_bytes, cfg.log_backups)
    from .app import App
    from .web.server import serve

    app = App(cfg)
    if app.auth.user_count() == 0:
        log.warning("No hay usuarios. Cree uno con: constructor-ia adduser <nombre>")
    app.start()
    try:
        servers = serve(app)
    except OSError as e:
        log.error("No se pudo abrir el puerto %d: %s", cfg.port, e)
        app.stop()
        return 1
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("Constructor IA %s listo", VERSION)
    stop.wait()
    log.info("Deteniendo…")
    for s in servers:
        s.stopping.set()
        s.shutdown()
        s.server_close()
    app.stop()
    log.info("Detenido limpiamente")
    return 0


def _read_password(confirm: bool = True) -> str:
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    pw = getpass.getpass("Contraseña (mín. 10 caracteres): ")
    if confirm and getpass.getpass("Repetir contraseña: ") != pw:
        print("Las contraseñas no coinciden", file=sys.stderr)
        raise SystemExit(1)
    return pw


def cmd_adduser(args) -> int:
    cfg = _cfg(args)
    from .auth import Auth
    from .db import Database

    db = Database(cfg.db_path)
    db.migrate()
    auth = Auth(db, cfg.session_idle_hours, cfg.session_max_days)
    try:
        auth.create_user(args.username, _read_password())
    except (ValueError, sqlite3.IntegrityError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    db.audit("usuario_creado", username=args.username, detail="cli")
    print(f"Usuario {args.username} creado")
    return 0


def cmd_passwd(args) -> int:
    cfg = _cfg(args)
    from .auth import Auth
    from .db import Database

    db = Database(cfg.db_path)
    db.migrate()
    try:
        Auth(db, cfg.session_idle_hours, cfg.session_max_days).set_password(args.username, _read_password())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    db.audit("contraseña_cambiada", username=args.username, detail="cli")
    print("Contraseña actualizada; se cerraron las sesiones abiertas")
    return 0


def cmd_selftest(args) -> int:
    cfg = _cfg(args)
    cfg.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    from .sandbox.executor import Executor

    ex = Executor(cfg)
    stress = [s for s in (args.stress or "").split(",") if s]
    unknown = set(stress) - {"mem", "fork", "orphan", "spin"}
    if unknown:
        print(f"Pruebas de estrés desconocidas: {unknown}", file=sys.stderr)
        return 2
    report = ex.self_test(stress=stress)
    for c in report["checks"]:
        print(f"[{'OK' if c['ok'] else 'FALLA'}] {c['name']}: {c['detail']}")
    if "exec" in report:
        print("Ejecución:", json.dumps(report["exec"], ensure_ascii=False))
    probe = report.get("probe") or {}
    for key in ("stress_mem", "stress_fork"):
        if key in probe:
            r = probe[key]
            print(f"{key}: código={r['code']} timeout={r['timeout']} stdout={r['stdout'].strip()[:200]!r}")
    if "mem" in stress and "exec" in report:
        print("memory.events oom_kill:", report["exec"].get("oom_kills"))
    if "stress_orphan" in probe:
        print("stress_orphan: proceso nieto lanzado; limpieza verificada =", report["exec"]["cleanup_verified"])
    if "spin" in stress:
        print("spin: estado de ejecución =", report["exec"]["status"], "(se espera 'timeout')")
    print("RESULTADO:", "AISLAMIENTO OPERATIVO" if report["ok"] else "EJECUCIÓN BLOQUEADA")
    return 0 if report["ok"] or "spin" in stress else 1


def cmd_backup(args) -> int:
    cfg = _cfg(args)
    dest = Path(args.dest or cfg.data_dir / "backups" / time.strftime("constructor-%Y%m%d-%H%M%S.db"))
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    src = sqlite3.connect(cfg.db_path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    os.chmod(dest, 0o600)
    print(f"Copia de seguridad: {dest}")
    return 0


def cmd_check_config(args) -> int:
    cfg = _cfg(args)
    print(json.dumps({
        "listen": cfg.listen, "port": cfg.port, "allowed_hosts": cfg.allowed_hosts,
        "cookie_secure": cfg.cookie_secure, "data_dir": str(cfg.data_dir), "ollama_url": cfg.ollama_url,
        "builder": cfg.builder.__dict__, "chat": cfg.chat.__dict__, "max_attempts": cfg.max_attempts,
        "sandbox": {"enabled": cfg.sandbox_enabled, "memory_mb": cfg.sandbox_memory_mb,
                    "tasks": cfg.sandbox_tasks, "cpu_percent": cfg.sandbox_cpu_percent,
                    "timeout_s": cfg.sandbox_timeout_s, "seccomp": cfg.sandbox_seccomp},
    }, indent=2, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="constructor-ia", description=f"Constructor IA {VERSION}")
    p.add_argument("--config", help="Ruta del config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="Inicia el servidor").set_defaults(fn=cmd_serve)
    a = sub.add_parser("adduser", help="Crea un usuario")
    a.add_argument("username")
    a.set_defaults(fn=cmd_adduser)
    a = sub.add_parser("passwd", help="Cambia la contraseña")
    a.add_argument("username")
    a.set_defaults(fn=cmd_passwd)
    a = sub.add_parser("selftest", help="Autodiagnóstico del aislamiento")
    a.add_argument("--stress", help="Lista separada por comas: mem,fork,orphan,spin")
    a.set_defaults(fn=cmd_selftest)
    a = sub.add_parser("backup", help="Copia consistente de la base SQLite")
    a.add_argument("dest", nargs="?")
    a.set_defaults(fn=cmd_backup)
    sub.add_parser("check-config", help="Valida y muestra la configuración").set_defaults(fn=cmd_check_config)
    args = p.parse_args(argv)
    return args.fn(args)
