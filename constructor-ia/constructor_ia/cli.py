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
    if cfg.web_enabled and cfg.web_preview_enabled:
        from .web.preview import serve_preview

        try:
            servers += serve_preview(app)
            app.preview_available = True
        except OSError as e:
            log.error("Vista previa DESHABILITADA: no se pudo abrir el puerto %d (%s); queda la descarga ZIP",
                      cfg.web_preview_port, e)
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


def _explorer_conn(cfg, name):
    for c in cfg.explorer_connections:
        if c.name == name:
            return c
    print(f"No existe la conexión «{name}» en [[explorer.connections]] de config.toml", file=sys.stderr)
    raise SystemExit(2)


def cmd_explorer_setup(args) -> int:
    """Genera la contraseña (archivo 0600) e imprime el SQL para crear la cuenta de SOLO LECTURA.

    No se conecta a MariaDB ni ejecuta nada: el operador revisa y ejecuta el SQL como administrador.
    El SQL usa el hash mysql_native_password, así la contraseña no aparece en el historial.
    """
    import hashlib
    import secrets

    cfg = _cfg(args)
    c = _explorer_conn(cfg, args.conn)
    path = Path(c.password_file)
    if path.exists():
        from .explorer.broker import BrokerError, read_password

        try:
            pw = read_password(str(path))
        except BrokerError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(f"-- Se usa la contraseña existente de {path}", file=sys.stderr)
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        pw = secrets.token_urlsafe(24)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(pw + "\n")
        print(f"-- Contraseña generada en {path} (0600). No se muestra.", file=sys.stderr)
    digest = "*" + hashlib.sha1(hashlib.sha1(pw.encode()).digest()).hexdigest().upper()
    host = "localhost" if c.socket or c.host == "localhost" else c.host
    db_escaped = c.database.replace("_", "\\_").replace("%", "\\%")
    print(f"""-- Ejecutar como administrador de MariaDB (por ejemplo: sudo mariadb < este_archivo.sql)
-- Crea una cuenta EXCLUSIVA de solo lectura para Constructor IA. No modifica otras cuentas ni la configuración global.
CREATE USER '{c.user}'@'{host}' IDENTIFIED BY PASSWORD '{digest}'
  WITH MAX_USER_CONNECTIONS 2 MAX_QUERIES_PER_HOUR 3000 MAX_STATEMENT_TIME 15;
GRANT SELECT, SHOW VIEW ON `{db_escaped}`.* TO '{c.user}'@'{host}';
SHOW GRANTS FOR '{c.user}'@'{host}';""")
    return 0


def cmd_explorer_verify(args) -> int:
    cfg = _cfg(args)
    c = _explorer_conn(cfg, args.conn)
    from .db import Database
    from .explorer.broker import Broker

    db = Database(cfg.db_path)
    db.migrate()
    info = Broker(cfg, c, db).verify()
    for k in ("verified", "current_user", "server_version", "session", "write_probe", "reason"):
        if k in info:
            print(f"{k}: {info[k]}")
    for g in info.get("grants", []):
        print("grant:", g)
    print("RESULTADO:", "CONEXIÓN VERIFICADA (solo lectura)" if info["verified"] else "CONEXIÓN BLOQUEADA")
    return 0 if info["verified"] else 1


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
    a = sub.add_parser("explorer-setup", help="Genera la contraseña e imprime el SQL de la cuenta de solo lectura")
    a.add_argument("conn")
    a.set_defaults(fn=cmd_explorer_setup)
    a = sub.add_parser("explorer-verify", help="Verifica privilegios y límites de una conexión del Explorador")
    a.add_argument("conn")
    a.set_defaults(fn=cmd_explorer_verify)
    args = p.parse_args(argv)
    return args.fn(args)
