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


def _print_evaluation(db, mid) -> int:
    run = db.one("SELECT * FROM explore_runs WHERE mission_id=?", (mid,))
    m = db.one("SELECT state, error, started_at, finished_at FROM missions WHERE id=?", (mid,))
    if m is None:
        print(f"No existe la misión {mid}", file=sys.stderr)
        return 2
    print(f"Misión {mid}: proceso «{m['state']}»")
    if run is None:
        print("Sin evaluación registrada (exploración anterior a 0.4.1 o abortada antes de observar la estructura).")
        return 1
    metrics = json.loads(run["metrics_json"])
    print(f"Finalización: {run['ended_reason']} · tiempo {run['elapsed_s'] / 60:.1f} de {run['budget_s'] / 60:.1f} min")
    print(f"Resultado: {run['quality'].upper()} · objetivo {'CUMPLIDO' if run['objective_met'] else 'NO cumplido'}"
          f" · informe {run['report_status']}")
    for k, v in metrics.items():
        print(f"  {k}: {v}")
    for r in json.loads(run["reasons_json"]):
        print(f"  motivo: {r}")
    print("Traza del ciclo (pregunta → consulta → registros → interpretación → siguiente pregunta):")
    for st in db.all("SELECT * FROM explore_steps WHERE mission_id=? ORDER BY n, id", (mid,)):
        st = dict(st)
        if st["status"] == "invalida":
            print(f"  Paso {st['n']}: respuesta inválida — {(st.get('analysis') or '')[:160]}")
            continue
        q = db.one("SELECT status, row_count FROM explore_queries WHERE id=?", (st["query_id"],)) if st.get("query_id") else None
        res = f"C{st['query_id']} {q['status']}, {q['row_count']} filas" if q else "sin consulta"
        print(f"  Paso {st['n']}: {st['question'][:120]} → {st['tool']} → {res}")
        ev = json.loads(st.get("evidence_json") or "{}")
        if ev.get("anticipadas_descartadas"):
            print(f"      etapa A: se descartaron {', '.join(ev['anticipadas_descartadas'])} anticipados (sin resultado)")
        it = json.loads(st["interpretation"]) if st.get("interpretation") else None
        if it and it.get("estado") == "valida":
            print(f"      etapa B: {it['resumen'][:160]} [{it['resumen_verificacion']}] · "
                  f"{it['respaldados']} hallazgos respaldados de {len(it['hallazgos'])}")
        elif it:
            print(f"      etapa B: sin interpretación válida — {it.get('motivo', '')[:160]}")
        ctx = json.loads(st["context_json"]) if st.get("context_json") else {}
        notes = [f"{k}: nivel {v.get('nivel', '—')}, ~{v.get('tokens_estimados', '?')} tokens"
                 + (" (con desborde)" if any(a.get("desborde") for a in v.get("intentos", [])) else "")
                 for k, v in ctx.items() if v]
        if notes:
            print(f"      contexto: {'; '.join(notes)}")
    print("Hallazgos respaldados:")
    for f in db.all("SELECT id, text FROM explore_facts WHERE mission_id=? AND kind IN ('hallazgo','advertencia')"
                    " AND certainty='inferencia' ORDER BY id", (mid,)):
        print(f"  F{f[0]} {f[1][:200]}")
    print("Aplicaciones propuestas:")
    for f in db.all("SELECT id, text FROM explore_facts WHERE mission_id=? AND kind='oportunidad' ORDER BY id", (mid,)):
        print(f"  F{f[0]} {f[1][:200]}")
    return 0 if run["objective_met"] else 1


def _print_raw_answers(db, mid) -> None:
    """Respuestas crudas del modelo en la etapa A (y sus reparaciones), para diagnosticar argumentos inválidos."""
    print("Respuestas crudas de la etapa A (investigación):")
    for st in db.all("SELECT * FROM explore_steps WHERE mission_id=? ORDER BY n, id", (mid,)):
        st = dict(st)
        ctx = json.loads(st["context_json"]) if st.get("context_json") else {}
        tries = ctx.get("respuestas_modelo") or ([{"raw": st["raw"], "problema": st.get("analysis") or ""}]
                                                 if st["status"] == "invalida" and st.get("raw") else [])
        if not tries:
            continue
        print(f"  Paso {st['n']} ({st['status']}, {st.get('repairs') or 0} reparaciones):")
        for i, t in enumerate(tries):
            label = "respuesta" if i == 0 else f"reparación {i}"
            print(f"    {label}{' [esquema estricto]' if t.get('esquema') == 'estricto' else ''}: {t['raw'][:600]}")
            print(f"      → {t['problema'] or 'aceptada'}")


def cmd_explorer_report(args) -> int:
    cfg = _cfg(args)
    from .db import Database

    db = Database(cfg.db_path)
    db.migrate()
    rc = _print_evaluation(db, args.mission)
    if args.respuestas:
        _print_raw_answers(db, args.mission)
    return rc


def cmd_explorer_trial(args) -> int:
    """Expedición de ensayo sin interfaz, con el modelo real configurado (para la VivoBook)."""
    import subprocess

    cfg = _cfg(args)
    if not args.yes:
        print("Agregue --yes para confirmar: ejecuta consultas de SOLO LECTURA y usa Ollama durante varios minutos.",
              file=sys.stderr)
        return 2
    try:
        active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "constructor-ia.service"],
                                timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        active = False
    if active:
        print("El servicio constructor-ia está activo: deténgalo (systemctl --user stop constructor-ia) o lance la "
              "exploración desde la interfaz, para no competir por Ollama CPU.", file=sys.stderr)
        return 2
    _explorer_conn(cfg, args.conn)
    logsetup.setup(cfg.log_dir, cfg.log_max_bytes, cfg.log_backups)
    from .app import App

    app = App(cfg)
    spec = {"mode": "exploracion", "conn": args.conn, "max_steps": args.steps, "max_queries": args.queries,
            "max_minutes": args.minutes, "continuar": True}
    text = args.mission_text or ("Explorá la base de datos que tenés disponible. No te voy a explicar qué contiene. "
                                 "Descubrí su estructura, investigá su información y construí un mapa de lo que "
                                 "encontrás. Identificá relaciones, patrones y oportunidades para desarrollar "
                                 "herramientas útiles. Cuando termines, presentame tus descubrimientos y tres "
                                 "aplicaciones que podrías construir.")
    mid = app.missions.create({"title": "Ensayo de exploración (CLI)", "description": text, "spec": spec,
                               "tests_py": "", "max_attempts": 1}, cfg.explorer_model.name, "cli")
    app.db.execute("UPDATE missions SET state='preparando', claimed_by='cli', started_at=? WHERE id=?", (time.time(), mid))
    print(f"Misión de ensayo {mid}: hasta {args.steps} pasos, {args.queries} consultas, {args.minutes} min")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    last = app.bus.latest_id()

    def tail():
        nonlocal last
        while True:
            for ev in app.bus.since(last):
                last = ev["id"]
                if ev["mission_id"] == mid:
                    print(time.strftime("%H:%M:%S"), ev["kind"], "|", ev["message"][:220], flush=True)
            if done.wait(1):
                for ev in app.bus.since(last):
                    if ev["mission_id"] == mid:
                        print(time.strftime("%H:%M:%S"), ev["kind"], "|", ev["message"][:220], flush=True)
                return

    done = threading.Event()
    t = threading.Thread(target=tail, daemon=True)
    t.start()
    app.engine.explorer.run(mid, spec, text, stop)
    done.set()
    t.join(5)
    print()
    return _print_evaluation(app.db, mid)


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
    a = sub.add_parser("explorer-report", help="Muestra la evaluación de una exploración (solo lectura)")
    a.add_argument("mission")
    a.add_argument("--respuestas", action="store_true", help="incluir las respuestas crudas del modelo en la etapa A")
    a.set_defaults(fn=cmd_explorer_report)
    a = sub.add_parser("explorer-trial", help="Expedición de ensayo sin interfaz con el modelo real")
    a.add_argument("conn")
    a.add_argument("--minutes", type=int, default=20)
    a.add_argument("--steps", type=int, default=15)
    a.add_argument("--queries", type=int, default=30)
    a.add_argument("--mission-text", dest="mission_text")
    a.add_argument("--yes", action="store_true")
    a.set_defaults(fn=cmd_explorer_trial)
    a = sub.add_parser("explorer-verify", help="Verifica privilegios y límites de una conexión del Explorador")
    a.add_argument("conn")
    a.set_defaults(fn=cmd_explorer_verify)
    args = p.parse_args(argv)
    return args.fn(args)
