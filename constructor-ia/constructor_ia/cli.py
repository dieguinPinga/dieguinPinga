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
        if st["status"] in ("invalida", "sin_respuesta"):
            label = "respuesta inválida" if st["status"] == "invalida" else "Ollama no respondió"
            print(f"  Paso {st['n']}: {label} — {(st.get('analysis') or '')[:160]}")
            continue
        q = db.one("SELECT status, row_count FROM explore_queries WHERE id=?", (st["query_id"],)) if st.get("query_id") else None
        res = f"C{st['query_id']} {q['status']}, {q['row_count']} filas" if q else "sin consulta"
        print(f"  Paso {st['n']}: {st['question'][:120]} → {st['tool']} → {res}")
        ap = json.loads(st["aporte_json"]) if st.get("aporte_json") else None
        if ap:
            print(f"      busca {ap.get('busca')} → {ap.get('respuesta')}"
                  + (f" · alcance: {ap['alcance']}" if ap.get("alcance") else "")
                  + (f" · repetición: {ap['repeticion']}" if ap.get("repeticion") else "")
                  + (f" · parecida a {ap['parecida_a']}" if ap.get("parecida_a") else ""))
            for c in ap.get("afirmaciones", []):
                print(f"      F{c['id']}: aporte {c['aporte']}" + (f" ({c['motivo']})" if c.get("motivo") else ""))
        ev = json.loads(st.get("evidence_json") or "{}")
        if ev.get("anticipadas_descartadas"):
            print(f"      etapa A: se descartaron {', '.join(ev['anticipadas_descartadas'])} anticipados (sin resultado)")
        it = json.loads(st["interpretation"]) if st.get("interpretation") else None
        if it and it.get("estado") == "valida":
            print(f"      etapa B ({it.get('forma', 'completa')}): {it['resumen'][:160]} [{it['resumen_verificacion']}] · "
                  f"{it['respaldados']} hallazgos respaldados de {len(it['hallazgos'])}")
        elif it:
            print(f"      etapa B: sin interpretación válida — {it.get('motivo', '')[:160]}")
        ctx = json.loads(st["context_json"]) if st.get("context_json") else {}
        notes = []
        for k, v in ctx.items():
            if not isinstance(v, dict):
                continue
            last = (v.get("intentos") or [{}])[-1]
            t = last.get("tiempos") or {}
            note = f"{k}: nivel {v.get('nivel', '—')}, ~{v.get('tokens_estimados', '?')} tokens"
            if any(a.get("desborde") for a in v.get("intentos", [])):
                note += " (con desborde)"
            if t:
                note += (f", {t['reloj_ms'] / 1000:.0f} s (prompt {t.get('tokens_prompt')} tok, generó "
                         f"{t.get('tokens_generados')} tok"
                         + (f" a {t['generacion_tokens_s']} tok/s" if t.get("generacion_tokens_s") else "")
                         + (f", sin explicar {t['sin_explicar_ms'] / 1000:.0f} s" if t.get("sin_explicar_ms") else "")
                         + f", fin {t.get('fin')})")
            if last.get("timeout"):
                note += f", SIN RESPUESTA de Ollama tras {last['timeout']['espera_s']} s ({last['timeout']['fase']})"
            notes.append(note)
        if notes:
            print(f"      contexto: {'; '.join(notes)}")
    print("Hallazgos respaldados:")
    from .explorer.mapstore import EFFECTIVE_SQL
    for f in db.all(f"SELECT f.id, f.text FROM explore_facts f WHERE f.mission_id=? AND f.kind IN ('hallazgo','advertencia')"
                    f" AND {EFFECTIVE_SQL}='inferencia' ORDER BY f.id", (mid,)):
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


def cmd_explorer_fact(args) -> int:
    """Procedencia de un hecho del mapa: texto original, certeza, alcance, aporte e historial completo."""
    cfg = _cfg(args)
    from .db import Database
    from .explorer.mapstore import MapStore

    db = Database(cfg.db_path)
    db.migrate()
    row = db.one("SELECT * FROM explore_facts WHERE conn=? AND id=?", (args.conn, args.id))
    if row is None:
        print(f"No existe el hecho F{args.id} en {args.conn}", file=sys.stderr)
        return 2
    d = json.loads(row["data_json"] or "{}") or {}
    maps = MapStore(db)
    eff = maps.effective(row["id"], row["certainty"])
    print(f"F{row['id']} [{row['kind']} · {eff}" + (f" (original: {row['certainty']})" if eff != row["certainty"] else "")
          + f" · {row['status']}] {row['text']}")
    print(f"  creado en {row['mission_id']} · última observación en {row['last_mission_id']} · evidencia "
          f"{', '.join(json.loads(row['evidence_json'] or '[]')) or '—'}")
    for k in ("verificacion", "detalle", "alcance", "alcance_declarado", "aporte", "aporte_motivo", "relacion_con",
              "verificar_con"):
        if d.get(k):
            print(f"  {k}: {d[k]}")
    for rv in maps.reviews(args.conn, "hecho", args.id):
        print(f"  reevaluado ({rv['regla']}, {rv['version']}, plan {rv['plan']}): {rv['certeza_anterior']} → "
              f"{rv['certeza_nueva']} · {rv['detalle']}")
    print("Historial:")
    for e in maps.history(args.conn, args.id):
        print(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(e['created_at']))} {e['event']} ({e['mission_id']}): "
              f"{e['certainty_before']} → {e['certainty_after']}" + (f" · evidencia {', '.join(e['evidence'])}" if e['evidence'] else ""))
    return 0


def cmd_explorer_reevaluate(args) -> int:
    """Reevalúa la memoria con las reglas de 0.4.7. Por defecto SOLO SIMULA (base abierta en solo lectura)."""
    cfg = _cfg(args)
    from .db import Database
    from .explorer.reevaluate import Reevaluator

    only = {x.strip().upper() for x in (args.solo or "").split(",") if x.strip()} or None
    if not args.aplicar:
        if not Path(cfg.db_path).exists():
            print(f"No existe la base {cfg.db_path}", file=sys.stderr)
            return 2
        rv = Reevaluator(Database(cfg.db_path, readonly=True), args.conn)
        items = rv.plan(only)
        code = rv.code(items)
        if args.json:
            print(json.dumps({"plan": code, "items": items}, ensure_ascii=False, indent=1, default=str))
        else:
            for it in items:
                ref = f"{'C' if it['objeto'] == 'consulta' else 'F'}{it['id']}"
                cert = (f"{it['certeza_anterior']} → {it['certeza_nueva']}" if it["objeto"] == "hecho" else "alcance")
                print(f"{ref:>6} [{it['regla']}] {cert}: {it['detalle']}")
            counts: dict = {}
            for it in items:
                counts[it["regla"]] = counts.get(it["regla"], 0) + 1
            print(f"\nSIMULACIÓN — no se escribió nada. {len(items)} reevaluaciones"
                  + (f" ({', '.join(f'{k}: {v}' for k, v in counts.items())})" if counts else "") + f". Plan {code}.")
            if items:
                print(f"Para aplicarlo (con copia de seguridad previa): constructor-ia explorer-reevaluar {args.conn}"
                      + (f" --solo {','.join(sorted(only))}" if only else "") + f" --aplicar --plan {code}")
        return 0
    if not args.plan:
        print("Aplicar exige --plan con el código de una simulación revisada", file=sys.stderr)
        return 2
    backup = Path(cfg.data_dir / "backups" / time.strftime("constructor-%Y%m%d-%H%M%S-pre-reevaluacion.db"))
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    src, dst = sqlite3.connect(cfg.db_path), sqlite3.connect(backup)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    os.chmod(backup, 0o600)
    print(f"Copia de seguridad: {backup}")
    db = Database(cfg.db_path)
    db.migrate()
    rv = Reevaluator(db, args.conn)
    items = rv.plan(only)
    try:
        n = rv.apply(items, args.plan, authorized_by=f"cli:{os.environ.get('USER', '?')}")
    except (ValueError, RuntimeError) as e:
        print(f"No se aplicó nada: {e}", file=sys.stderr)
        return 1
    print(f"Aplicadas {n} reevaluaciones del plan {args.plan}. Los hechos conservan su texto y su certeza original.")
    return 0


def cmd_investigate(args) -> int:
    """Investigación experimental sin interfaz, con el modelo real (para la VivoBook). No genera aplicaciones."""
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
        print("El servicio constructor-ia está activo: deténgalo (systemctl --user stop constructor-ia) para no "
              "competir por Ollama CPU.", file=sys.stderr)
        return 2
    _explorer_conn(cfg, args.conn)
    logsetup.setup(cfg.log_dir, cfg.log_max_bytes, cfg.log_backups)
    from .app import App

    app = App(cfg)
    spec = {"mode": "investigacion", "conn": args.conn, "max_ciclos": args.ciclos, "max_minutos": args.minutos}
    if args.reanudar:
        spec["reanudar"] = args.reanudar
    text = args.objetivo or ("Investigá esta base de datos por iniciativa propia: elegí qué averiguar, aprendé de la "
                             "evidencia y usá lo aprendido para decidir qué investigar después.")
    mid = app.missions.create({"title": "Investigación experimental (CLI)", "description": text, "spec": spec,
                               "tests_py": "", "max_attempts": 1}, cfg.explorer_model.name, "cli")
    app.db.execute("UPDATE missions SET state='preparando', claimed_by='cli', started_at=? WHERE id=?", (time.time(), mid))
    print(f"Investigación {mid}: hasta {args.ciclos} ciclos y {args.minutos} min"
          + (f", reanudando {args.reanudar}" if args.reanudar else "") + ". Ctrl-C la interrumpe (reanudable).")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    last = app.bus.latest_id()
    done = threading.Event()

    def tail():
        nonlocal last
        while True:
            for ev in app.bus.since(last):
                last = ev["id"]
                if ev["mission_id"] == mid:
                    print(time.strftime("%H:%M:%S"), ev["kind"], "|", ev["message"][:300], flush=True)
            if done.wait(1):
                return

    t = threading.Thread(target=tail, daemon=True)
    t.start()
    app.engine.investigator.run(mid, spec, text, stop)
    done.set()
    t.join(5)
    print()
    return _print_investigation(app.db, app.files, mid)


def _print_investigation(db, files, mid) -> int:
    from .investigador.memory import InvMemory
    from .explorer.mapstore import MapStore

    row = db.one("SELECT conn FROM inv_sessions WHERE mission_id=?", (mid,))
    if row is None:
        print(f"No hay una investigación {mid}", file=sys.stderr)
        return 2
    s = InvMemory(db, MapStore(db), row[0]).session(mid)
    m = s.get("metrics") or {}
    print(f"Investigación {mid} [{s['status']}]" + (f" (reanuda {s['resumed_from']})" if s.get("resumed_from") else "")
          + (f" (reanudada por {s['resumed_by']})" if s.get("resumed_by") else ""))
    try:
        print(files.read_rel(mid, "investigacion/resumen.md"))
    except Exception:  # noqa: BLE001
        print(json.dumps(m, ensure_ascii=False, indent=1))
    return 0 if (m.get("criterios") or {}).get("cumplidos") else 1


def cmd_investigation_report(args) -> int:
    cfg = _cfg(args)
    from .db import Database
    from .files import FileStore

    db = Database(cfg.db_path)
    db.migrate()
    return _print_investigation(db, FileStore(db, cfg.missions_dir), args.mission)


def cmd_ollama_diag(args) -> int:
    """Diagnóstico de solo lectura del Ollama CPU (memoria, swap, PSI, cgroup, CPU) cruzado con las llamadas."""
    from . import diag_ollama as D

    cfg = _cfg(args)
    calls = None
    if args.mision:
        from .db import Database
        from .files import FileStore

        db = Database(cfg.db_path)
        raw = FileStore(db, cfg.missions_dir).read_rel(args.mision, "exploracion/llamadas_ollama.json")
        if raw is None:
            print(f"La misión {args.mision} no tiene registro de llamadas (exploraciones desde 0.4.4).", file=sys.stderr)
        else:
            calls = json.loads(raw)
    if args.analizar:
        data = D.load(Path(args.analizar))
    else:
        out = Path(args.salida or cfg.log_dir / time.strftime("ollama-diag-%Y%m%d-%H%M%S.jsonl"))
        out.parent.mkdir(parents=True, exist_ok=True)
        data = D.collect(cfg.ollama_url, args.segundos, args.intervalo, args.pid, out)
    report = D.analyze(data, calls)
    print(D.render(report))
    print(D.journal_hint(data["meta"]))
    if not args.analizar:
        Path(str(out) + ".resumen.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"Muestras: {out} · resumen: {out}.resumen.json")
    return 0


def cmd_lab_instruments(args) -> int:
    """Fase 1: laboratorio de instrumentos con los modelos reales, sobre una base sintética en un MariaDB PRIVADO."""
    import subprocess

    cfg = _cfg(args)
    if not args.yes:
        print("Agregue --yes para confirmar: crea un MariaDB privado y desechable (datadir y socket propios, sin red; "
              "no toca el MariaDB instalado) y usa Ollama CPU durante bastante tiempo.", file=sys.stderr)
        return 2
    try:
        active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "constructor-ia.service"],
                                timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        active = False
    if active:
        print("El servicio constructor-ia está activo: deténgalo (systemctl --user stop constructor-ia) para no "
              "competir por Ollama CPU.", file=sys.stderr)
        return 2
    from .laboratorio import corrida, informe, privado, tareas
    from .laboratorio.protocolo import Modelo
    from .ollama import OllamaClient, OllamaError
    from .sandbox.executor import Executor

    try:
        sel = tareas.seleccionar([t for t in args.tareas.split(",") if t.strip()])
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    faltan = privado.disponible()
    if faltan:
        print(f"No se puede crear el MariaDB privado: faltan {', '.join(faltan)}.", file=sys.stderr)
        return 2
    cfg.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ex = Executor(cfg)
    report = ex.self_test()
    if not report["ok"]:
        print("El aislamiento no está operativo (constructor-ia selftest): el laboratorio no ejecuta código.",
              file=sys.stderr)
        return 1
    ollama = OllamaClient(cfg.ollama_url)
    modelos = [m.strip() for m in args.modelos.split(",") if m.strip()]
    try:
        disponibles = ollama.tags()
    except OllamaError as e:
        print(f"Ollama CPU no responde en {cfg.ollama_url}: {e}", file=sys.stderr)
        return 1
    faltan = [m for m in modelos if m not in disponibles]
    if faltan:
        print(f"Modelos no instalados en {cfg.ollama_url}: {', '.join(faltan)} (no se descargan).", file=sys.stderr)
        return 2
    base = cfg.explorer_model
    lab = [Modelo(ollama, base, m, num_ctx=args.num_ctx or base.num_ctx,
                  temperatura=base.temperature if args.temperatura is None else args.temperatura,
                  timeout_s=base.timeout_s) for m in modelos]
    salida = cfg.data_dir / "laboratorio" / time.strftime("%Y%m%d-%H%M%S")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    servidor = privado.ServidorPrivado(salida / "mariadb", conservar=args.conservar_mariadb)
    try:
        res = corrida.correr(cfg, executor=ex, servidor=servidor, modelos=lab, tareas=sel,
                             repeticiones=args.repeticiones, semilla=args.semilla, escala=args.escala, salida=salida,
                             max_correcciones=args.correcciones, reparaciones_formato=args.reparaciones_formato,
                             max_mismo_error=args.mismo_error, cancel=stop,
                             log=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
    finally:
        servidor.detener()
    j, m = informe.escribir(res, salida)
    print()
    print(m.read_text())
    print(f"Resultados: {j}\nResumen: {m}")
    return 0 if res["estado"] == "completa" else 1


def _servicio_activo() -> bool:
    import subprocess

    try:
        return subprocess.run(["systemctl", "--user", "is-active", "--quiet", "constructor-ia.service"],
                              timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _piloto_preparar(args, necesita_modelo: bool, exigir_servicio_detenido: bool = True):
    """Comprobaciones comunes del piloto: servicio detenido (si usa Ollama), MariaDB privado posible y aislamiento
    operativo."""
    cfg = _cfg(args)
    if not args.yes:
        print("Agregue --yes: crea un MariaDB privado y desechable (sin red, no toca el MariaDB instalado)"
              + (" y usa Ollama CPU durante horas." if necesita_modelo else "."), file=sys.stderr)
        raise SystemExit(2)
    if exigir_servicio_detenido and _servicio_activo():
        print("El servicio constructor-ia está activo: deténgalo para no competir por CPU y Ollama.", file=sys.stderr)
        raise SystemExit(2)
    from .laboratorio import privado
    from .sandbox.executor import Executor

    faltan = privado.disponible()
    if faltan:
        print(f"No se puede crear el MariaDB privado: faltan {', '.join(faltan)}.", file=sys.stderr)
        raise SystemExit(2)
    cfg.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ex = Executor(cfg)
    if not ex.self_test()["ok"]:
        print("El aislamiento no está operativo (constructor-ia selftest): no se ejecuta código.", file=sys.stderr)
        raise SystemExit(1)
    return cfg, ex


def cmd_lab_mutants(args) -> int:
    """Estudio de mutantes SIN MODELO: detección y localización del coordinador solo."""
    from .laboratorio import privado
    from .laboratorio.piloto import informe as PI, mutantes
    from .laboratorio.piloto.entorno import Entorno

    cfg, ex = _piloto_preparar(args, necesita_modelo=False)
    salida = Path(args.salida) if args.salida else cfg.data_dir / "laboratorio" / ("mutantes-" + time.strftime(
        "%Y%m%d-%H%M%S"))
    servidor = privado.ServidorPrivado(salida / "mariadb")
    try:
        ent = Entorno(cfg, ex, servidor, salida)
        mut = mutantes.estudiar(ent, log=lambda m: print(m, flush=True))
        ent.cerrar()
    finally:
        servidor.detener()
    print(PI.escribir(salida, None, mut).read_text())
    return 0


def cmd_lab_pilot(args) -> int:
    """Piloto preregistrado (PILOTO.md): 2 defectos × repeticiones × 5 brazos, con el modelo real."""
    from .laboratorio import privado
    from .laboratorio.piloto import informe as PI, mutantes, piloto
    from .laboratorio.piloto.entorno import Entorno
    from .laboratorio.protocolo import Modelo
    from .ollama import OllamaClient, OllamaError

    cfg, ex = _piloto_preparar(args, necesita_modelo=True)
    ollama = OllamaClient(cfg.ollama_url)
    try:
        if args.modelo not in ollama.tags():
            print(f"El modelo {args.modelo} no está instalado en {cfg.ollama_url} (no se descarga).", file=sys.stderr)
            return 2
    except OllamaError as e:
        print(f"Ollama CPU no responde en {cfg.ollama_url}: {e}", file=sys.stderr)
        return 1
    v2 = args.protocolo == "v2"
    prefijo = ("ensayo-" if args.ensayo else "piloto-") + ("v2-" if v2 else "")
    salida = Path(args.reanudar) if args.reanudar else cfg.data_dir / "laboratorio" / (prefijo + time.strftime(
        "%Y%m%d-%H%M%S"))
    try:
        piloto.preparar_salida(salida, args.ensayo, args.protocolo)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    base = cfg.explorer_model
    # v1: como el ensayo original. v2 (Enmienda 3): contexto 4096, modelo descargado tras cada llamada y guardia.
    num_ctx = args.num_ctx or (4096 if v2 else 6144)
    keep_alive = args.keep_alive if args.keep_alive is not None else ("0s" if v2 else None)
    minimo_mb = args.memoria_minima_mb if args.memoria_minima_mb is not None else (3900 if v2 else 0)
    from .laboratorio.piloto import v2 as PV2
    guardia = PV2.Guardia(ollama, minimo_mb=minimo_mb)
    print(f"Protocolo {args.protocolo}: num_ctx={num_ctx}, keep_alive={keep_alive}, memoria mínima "
          f"{minimo_mb} MiB por llamada.", flush=True)

    def crear_modelo():  # mismas opciones en todos los brazos (PILOTO.md, presupuestos y Enmiendas 2 y 3)
        return Modelo(ollama, base, args.modelo, num_ctx=num_ctx, temperatura=0.2, timeout_s=piloto.TIMEOUT_S,
                      num_predict_codigo=2048, num_predict_json=768, keep_alive=keep_alive)

    if args.ensayo:
        defectos, repeticiones, pres = ("D2",), 1, piloto.Presupuesto(**piloto.PRESUPUESTO_ENSAYO)
        print("ENSAYO TÉCNICO (excluido del análisis preregistrado): D2, 1 repetición, 5 brazos, "
              f"{pres.llamadas} llamadas por episodio.", flush=True)
    else:
        defectos, repeticiones, pres = tuple(args.defectos.split(",")), args.repeticiones, piloto.Presupuesto()

    if (salida / "mariadb").exists():
        import shutil
        shutil.rmtree(salida / "mariadb")
    servidor = privado.ServidorPrivado(salida / "mariadb")
    try:
        ent = Entorno(cfg, ex, servidor, salida)
        mut = None if args.sin_mutantes else mutantes.estudiar(ent, log=lambda m: None)
        res = piloto.correr(cfg, ent=ent, crear_modelo=crear_modelo, defectos=defectos, repeticiones=repeticiones,
                            brazos=tuple(args.brazos.split(",")), salida=salida, presupuesto=pres,
                            ensayo=args.ensayo, protocolo=args.protocolo, guardia=guardia,
                            log=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
        ent.cerrar()
    finally:
        servidor.detener()
    print(PI.escribir(salida, res, mut).read_text())
    print(f"Resultados en {salida} (episodios.jsonl, prompts.jsonl, piloto.json, resumen.md). "
          f"Para reanudar: laboratorio-piloto --yes --protocolo {args.protocolo}{' --ensayo' if args.ensayo else ''} "
          f"--reanudar {salida}")
    if res.get("estado") != "completo":
        print("La corrida se DETUVO por el entorno (ver interrupciones.jsonl); se puede reanudar.", file=sys.stderr)
        return 3
    return 0


def cmd_lab_choice_forensic(args) -> int:
    """Análisis forense de SOLO LECTURA de una Etapa 1 terminada: no llama al modelo y no escribe en la carpeta."""
    from .laboratorio.piloto import forense as FO

    carpeta = Path(args.carpeta).resolve()
    if not (carpeta / "ensayos.jsonl").exists() or not (carpeta / "casos.json").exists():
        print(f"{carpeta} no tiene ensayos.jsonl y casos.json de una Etapa 1.", file=sys.stderr)
        return 2
    destinos = [Path(x).resolve() for x in (args.salida, args.json) if x]
    if any(carpeta == d or carpeta in d.parents for d in destinos):
        print("El análisis no escribe dentro de la carpeta del experimento: elija otra ruta.", file=sys.stderr)
        return 2
    res = FO.analizar_carpeta(carpeta)
    texto = FO.markdown(res)
    if args.salida:
        Path(args.salida).write_text(texto)
        print(f"Análisis forense: {args.salida}")
    else:
        print(texto)
    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0


def cmd_lab_choice_stage0(args) -> int:
    """Experimento de elección, Etapa 0 (SIN modelo): construye los casos y mide si la traza distingue a la
    candidata sembrada. No usa Ollama: puede correr con el servicio activo."""
    from .laboratorio import privado
    from .laboratorio.piloto import eleccion as EL
    from .laboratorio.piloto.entorno import Entorno

    cfg, ex = _piloto_preparar(args, necesita_modelo=False, exigir_servicio_detenido=False)
    salida = Path(args.salida) if args.salida else cfg.data_dir / "laboratorio" / ("eleccion-etapa0-" + time.strftime(
        "%Y%m%d-%H%M%S"))
    servidor = privado.ServidorPrivado(salida / "mariadb")
    try:
        ent = Entorno(cfg, ex, servidor, salida)
        info = EL.construir_casos(ent, log=lambda m: print(m, flush=True))
        ent.cerrar()
    finally:
        servidor.detener()
    (salida / "casos.json").write_text(json.dumps(info, ensure_ascii=False, indent=1, default=str))
    e0 = info["etapa0"]
    print(json.dumps(e0, ensure_ascii=False, indent=1))
    print(f"Casos para la Etapa 1: {', '.join(info['seleccion'])}\nArchivo de casos: {salida / 'casos.json'}")
    return 0


def cmd_lab_choice_e0(args) -> int:
    """E0 del próximo experimento de elección (SIN modelo): ¿la evidencia del coordinador alcanza para que una
    regla fija elija la causa? Ejecuta versiones intervenidas en el sandbox; no usa Ollama."""
    from .laboratorio import privado
    from .laboratorio.piloto import defectos as DF
    from .laboratorio.piloto import e0 as E0
    from .laboratorio.piloto.entorno import Entorno

    externos = None
    if args.defectos:
        externos, problemas = DF.cargar(args.defectos)
        for p in problemas:
            print(f"  defecto descartado: {p}", file=sys.stderr)
        if not externos:
            print("No hay defectos válidos para evaluar.", file=sys.stderr)
            return 2
        print(f"Evaluación a ciegas: {len(externos)} defectos externos; reglas {E0.huella_reglas()}.", flush=True)
    cfg, ex = _piloto_preparar(args, necesita_modelo=False, exigir_servicio_detenido=False)
    salida = Path(args.salida) if args.salida else cfg.data_dir / "laboratorio" / ("eleccion-e0-" + time.strftime(
        "%Y%m%d-%H%M%S"))
    servidor = privado.ServidorPrivado(salida / "mariadb")
    try:
        ent = Entorno(cfg, ex, servidor, salida)
        res = E0.correr(ent, externos=externos, log=lambda m: print(m, flush=True))
        ent.cerrar()
    finally:
        servidor.detener()
    (salida / "e0.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    (salida / "e0.md").write_text(E0.markdown(res))
    print(E0.markdown(res))
    print(f"Resultados: {salida / 'e0.md'}")
    return 0


def cmd_lab_choice(args) -> int:
    """Experimento de elección, Etapa 1 (con modelo). No ejecuta código: arma prompts con los casos de la Etapa 0.
    Exige --limite N (prueba de rendimiento) o --completo: nunca lanza las 96 llamadas por omisión."""
    from .laboratorio.piloto import eleccion as EL
    from .laboratorio.piloto import v2 as PV2
    from .laboratorio.protocolo import Modelo
    from .ollama import OllamaClient, OllamaError

    cfg = _cfg(args)
    if not args.yes:
        print("Agregue --yes: usa Ollama CPU (llamadas cortas).", file=sys.stderr)
        return 2
    if (args.limite is None) == (not args.completo):
        print("Elija UNA: --limite N (prueba de rendimiento) o --completo (todos los ensayos planificados).",
              file=sys.stderr)
        return 2
    if _servicio_activo():
        print("El servicio constructor-ia está activo: deténgalo para no competir por Ollama CPU.", file=sys.stderr)
        return 2
    salida = Path(args.reanudar) if args.reanudar else cfg.data_dir / "laboratorio" / ("eleccion-" + time.strftime(
        "%Y%m%d-%H%M%S"))
    origen = salida / "casos.json" if args.reanudar else (Path(args.casos) if args.casos else None)
    if origen is None or not origen.exists():
        print("Falta el archivo de casos de la Etapa 0 (--casos <ruta>/casos.json).", file=sys.stderr)
        return 2
    casos_info = json.loads(origen.read_text())
    ollama = OllamaClient(cfg.ollama_url)
    try:
        if args.modelo not in ollama.tags():
            print(f"El modelo {args.modelo} no está instalado en {cfg.ollama_url} (no se descarga).", file=sys.stderr)
            return 2
    except OllamaError as e:
        print(f"Ollama CPU no responde en {cfg.ollama_url}: {e}", file=sys.stderr)
        return 1
    base = cfg.explorer_model
    guardia = PV2.Guardia(ollama, minimo_mb=args.memoria_minima_mb)
    print(f"Elección: {len(casos_info['seleccion'])} casos × {EL.ROTACIONES} rotaciones × {args.repeticiones} "
          f"repeticiones × 2 condiciones; num_ctx={args.num_ctx}, keep_alive={args.keep_alive}, temperatura 0.2, "
          f"semilla por par; memoria mínima {args.memoria_minima_mb} MiB.", flush=True)

    def crear_modelo():
        return Modelo(ollama, base, args.modelo, num_ctx=args.num_ctx, temperatura=0.2, timeout_s=900,
                      num_predict_codigo=2048, num_predict_json=768, keep_alive=args.keep_alive)

    res = EL.correr(cfg, crear_modelo=crear_modelo, salida=salida, casos_info=casos_info,
                    repeticiones=args.repeticiones, limite=args.limite, guardia=guardia,
                    log=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
    print((salida / "resumen.md").read_text())
    print(f"Resultados en {salida}. Para seguir: laboratorio-eleccion --yes --reanudar {salida} "
          "(--limite N o --completo)")
    return 0 if res["estado"] in ("completo", "limite") else 3


def cmd_lab_cycle(args) -> int:
    """Ciclo de investigación v1: hipótesis → reparación (Qwen propone) → verificación (el coordinador, en el sandbox)
    → juez (después y por separado). Exige --sin-modelo, --limite-llamadas N o --completo."""
    from .laboratorio import privado
    from .laboratorio.piloto import ciclo as CI
    from .laboratorio.piloto import defectos as DF
    from .laboratorio.piloto import v2 as PV2
    from .laboratorio.piloto.entorno import Entorno
    from .laboratorio.protocolo import Modelo
    from .ollama import OllamaClient, OllamaError

    modos = [args.sin_modelo, args.limite_llamadas is not None, args.completo]
    if sum(modos) != 1:
        print("Elija UNA: --sin-modelo (observación, reglas y juez; 0 llamadas), --limite-llamadas N o --completo.",
              file=sys.stderr)
        return 2
    if not 1 <= args.llamadas_por_defecto <= CI.TOPE_LLAMADAS:
        print(f"--llamadas-por-defecto debe estar entre 1 y {CI.TOPE_LLAMADAS}.", file=sys.stderr)
        return 2
    salida = Path(args.reanudar) if args.reanudar else None
    plan = json.loads((salida / "plan.json").read_text()) if salida and (salida / "plan.json").exists() else {}
    carpeta = args.defectos or plan.get("defectos")
    if not carpeta:
        print("Falta --defectos <carpeta con manifiesto.json>.", file=sys.stderr)
        return 2
    defectos, problemas = DF.cargar(carpeta)
    for p in problemas:
        print(f"  defecto descartado: {p}", file=sys.stderr)
    ids = [x for x in (args.ids or "").split(",") if x] or plan.get("ids") or [d["id"] for d in defectos]
    if plan and ids != plan.get("ids"):
        print(f"La corrida a reanudar es de {plan.get('ids')}: no se mezclan defectos.", file=sys.stderr)
        return 2
    faltan = [i for i in ids if i not in {d["id"] for d in defectos}]
    if faltan:
        print(f"Estos ids no están en el manifiesto: {faltan}", file=sys.stderr)
        return 2
    defectos = [d for d in defectos if d["id"] in ids]
    cfg, ex = _piloto_preparar(args, necesita_modelo=not args.sin_modelo, exigir_servicio_detenido=not args.sin_modelo)
    salida = salida or cfg.data_dir / "laboratorio" / ("ciclo-" + time.strftime("%Y%m%d-%H%M%S"))
    salida.mkdir(parents=True, exist_ok=True, mode=0o700)
    if plan.get("llamadas_por_defecto", args.llamadas_por_defecto) != args.llamadas_por_defecto:
        print(f"La corrida a reanudar usa {plan['llamadas_por_defecto']} llamadas por defecto: no se cambia a mitad.",
              file=sys.stderr)
        return 2
    (salida / "plan.json").write_text(json.dumps({"defectos": str(Path(carpeta).resolve()), "ids": ids,
                                                  "llamadas_por_defecto": args.llamadas_por_defecto}))
    crear_modelo = guardia = None
    if not args.sin_modelo:
        ollama = OllamaClient(cfg.ollama_url)
        try:
            if args.modelo not in ollama.tags():
                print(f"El modelo {args.modelo} no está instalado en {cfg.ollama_url} (no se descarga).",
                      file=sys.stderr)
                return 2
        except OllamaError as e:
            print(f"Ollama CPU no responde en {cfg.ollama_url}: {e}", file=sys.stderr)
            return 1
        guardia = PV2.Guardia(ollama, minimo_mb=args.memoria_minima_mb)
        base = cfg.explorer_model

        def crear_modelo():
            return Modelo(ollama, base, args.modelo, num_ctx=args.num_ctx, temperatura=0.2, timeout_s=900,
                          num_predict_codigo=2048, num_predict_json=768, keep_alive=args.keep_alive)
    print(f"{CI.VERSION}: {len(defectos)} defectos {ids}; hasta {args.llamadas_por_defecto} llamadas por defecto; "
          + ("SIN modelo." if args.sin_modelo else f"límite {args.limite_llamadas or 'ninguno'} llamadas nuevas; "
             f"num_ctx={args.num_ctx}, keep_alive={args.keep_alive}, memoria mínima {args.memoria_minima_mb} MiB."),
          flush=True)
    servidor = privado.ServidorPrivado(salida / "mariadb")
    try:
        ent = Entorno(cfg, ex, servidor, salida / "entorno")
        res = CI.correr(cfg, ent, defectos=defectos, salida=salida, crear_modelo=crear_modelo,
                        limite_llamadas=args.limite_llamadas, llamadas_por_defecto=args.llamadas_por_defecto,
                        guardia=guardia,
                        log=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
        ent.cerrar()
    finally:
        servidor.detener()
    print((salida / "resumen.md").read_text())
    print(f"Resultados en {salida}. Para seguir: laboratorio-ciclo --yes --reanudar {salida} "
          "(--limite-llamadas N o --completo)")
    return 0 if res["corrida"] in ("completa", "limite") else 3


def cmd_lab_suggestions(args) -> int:
    """Experimento de sugerencias: «localizador» contra «evidencia» en la primera llamada, con plan fijo por tanda
    (1: X05 y X08; 2: X11 y X12; control: X07 con la sugerencia 17). Con --solo-prompts no llama al modelo ni
    detiene nada: compara los prompts de cada pareja."""
    from .laboratorio import privado
    from .laboratorio.piloto import sugerencias as SG
    from .laboratorio.piloto import v2 as PV2
    from .laboratorio.piloto.entorno import Entorno
    from .laboratorio.protocolo import Modelo
    from .ollama import OllamaClient, OllamaError

    cfg, ex = _piloto_preparar(args, necesita_modelo=not args.solo_prompts,
                               exigir_servicio_detenido=not args.solo_prompts)
    carpeta = Path(args.carpeta) if args.carpeta else cfg.data_dir / "laboratorio" / "sugerencias-v1"
    salida = carpeta / (f"tanda-{args.tanda}" + ("-solo-prompts" if args.solo_prompts else ""))
    crear_modelo = guardia = None
    if not args.solo_prompts:
        ollama = OllamaClient(cfg.ollama_url)
        try:
            if args.modelo not in ollama.tags():
                print(f"El modelo {args.modelo} no está instalado en {cfg.ollama_url} (no se descarga).",
                      file=sys.stderr)
                return 2
        except OllamaError as e:
            print(f"Ollama CPU no responde en {cfg.ollama_url}: {e}", file=sys.stderr)
            return 1
        guardia = PV2.Guardia(ollama, minimo_mb=args.memoria_minima_mb)
        base = cfg.explorer_model

        def crear_modelo():          # los mismos ajustes que laboratorio-ciclo
            return Modelo(ollama, base, args.modelo, num_ctx=args.num_ctx, temperatura=0.2, timeout_s=900,
                          num_predict_codigo=2048, num_predict_json=768, keep_alive=args.keep_alive)
    plan = SG.TANDAS[args.tanda]
    print(f"{SG.VERSION}: tanda {args.tanda} {plan}; {SG.LLAMADAS_POR_CONDICION} llamada por defecto y condición; "
          + ("SOLO PROMPTS (sin modelo)." if args.solo_prompts else
             f"como máximo {len(plan)} llamadas; num_ctx={args.num_ctx}, keep_alive={args.keep_alive}, "
             f"memoria mínima {args.memoria_minima_mb} MiB."), flush=True)
    salida.mkdir(parents=True, exist_ok=True, mode=0o700)
    servidor = privado.ServidorPrivado(salida / "mariadb")
    try:
        ent = Entorno(cfg, ex, servidor, salida / "entorno")
        try:
            res = SG.correr_tanda(cfg, ent, tanda=args.tanda, salida=salida, crear_modelo=crear_modelo,
                                  guardia=guardia, log=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
        except SG.PlanInvalido as e:
            print(f"Plan inválido, no se llamó al modelo: {e}", file=sys.stderr)
            return 2
        finally:
            ent.cerrar()
    finally:
        servidor.detener()
    if args.solo_prompts:
        print((salida / "parejas.json").read_text())
    else:
        print((salida / "resumen.md").read_text())
        general = SG.resumen_general(carpeta)
        if general:
            (carpeta / "resumen.md").write_text(general)
            print(f"Resumen de todas las tandas terminadas: {carpeta / 'resumen.md'}")
    print(f"Resultados en {salida}. Si se cortó, el mismo comando reanuda sin repetir llamadas.")
    return 0 if res["corrida"] in ("completa", "solo_prompts") else 3


def cmd_lab_agent(args) -> int:
    """Agente de datos, fase 2 (AGENTE_DATOS.md): --preparar, --comprobar (sin modelo), --mision M1|M2|M2p, --estado.
    Solo bases sintéticas en un MariaDB privado; la verdad la lee el juez, nunca el proceso del agente."""
    from .agente_datos import experimento as EX
    from .laboratorio import privado

    cfg = _cfg(args)
    carpeta = Path(args.carpeta) if args.carpeta else cfg.data_dir / "laboratorio" / "agente-fase2"
    if args.estado:
        print(EX.resumen(carpeta) if (carpeta / "plan.json").exists() else f"{carpeta} no está preparada.")
        return 0
    if not args.yes:
        print("Agregue --yes: crea un MariaDB privado y desechable (sin red, no toca el MariaDB instalado ni ninguna "
              "base productiva).", file=sys.stderr)
        return 2
    faltan = privado.disponible()
    if faltan:
        print(f"No se puede crear el MariaDB privado: faltan {', '.join(faltan)}.", file=sys.stderr)
        return 2
    log = lambda m: print(m, flush=True)  # noqa: E731
    try:
        if args.preparar:
            EX.preparar(carpeta, log=log)
            return 0
        if args.comprobar:
            inf = EX.comprobar(carpeta_plan=carpeta if (carpeta / "plan.json").exists() else None,
                               ollama_url=cfg.ollama_url, modelo=args.nombre_modelo, log=log)
            print("RESULTADO: " + ("ENTORNO Y AISLAMIENTO OK (sin inferencias)" if inf["ok"] else "HAY FALLAS"))
            return 0 if inf["ok"] else 1
        if args.guion:
            modelo_args = ["--modelo", "guion", "--guion", args.guion]
        else:
            if _servicio_activo():
                print("El servicio constructor-ia está activo: deténgalo para no competir por Ollama CPU (usar el "
                      "comando protegido «agente» de scripts/piloto_etapas.sh).", file=sys.stderr)
                return 2
            from .ollama import OllamaClient, OllamaError
            try:
                if args.nombre_modelo not in OllamaClient(cfg.ollama_url).tags():
                    print(f"El modelo {args.nombre_modelo} no está instalado en {cfg.ollama_url} (no se descarga).",
                          file=sys.stderr)
                    return 2
            except OllamaError as e:
                print(f"Ollama CPU no responde en {cfg.ollama_url}: {e}", file=sys.stderr)
                return 1
            modelo_args = ["--modelo", "qwen", "--ollama-url", cfg.ollama_url, "--nombre-modelo", args.nombre_modelo,
                           "--num-ctx", str(args.num_ctx), "--keep-alive", args.keep_alive,
                           "--memoria-minima-mb", str(args.memoria_minima_mb)]
        modelo_args += ["--max-llamadas", str(args.max_llamadas)]
        print(f"{EX.VERSION_EXPERIMENTO}: {args.mision} en {carpeta}; modelo {modelo_args[1]}"
              + (f" {args.nombre_modelo}, num_ctx={args.num_ctx}, keep_alive={args.keep_alive}, memoria mínima "
                 f"{args.memoria_minima_mb} MiB" if not args.guion else f" ({args.guion}, SIN inferencias)")
              + f"; hasta {args.max_llamadas} llamadas.", flush=True)
        res = EX.mision(carpeta, args.mision, modelo_args, log=log)
    except (EX.ErrorExperimento, privado.ServidorNoDisponible) as e:
        print(f"No se ejecutó: {e}", file=sys.stderr)
        return 2
    print((carpeta / "resumen.md").read_text())
    print(f"Resultado de {args.mision}: estado {res['estado']} · coordinador {res['coordinador']['nivel']} · juez "
          f"{'sin veredicto' if res['juez'].get('correcta') is None else ('correcta' if res['juez']['correcta'] else 'incorrecta')}"
          f" · confirmación falsa: {'SÍ' if res['juez'].get('confirmacion_falsa') else 'no'}. Detalle en "
          f"{carpeta / 'resultados' / (args.mision + '.json')}")
    return 0 if res["estado"] == "completa" else 3


def cmd_lab_pilot_review(args) -> int:
    from .laboratorio.piloto import informe as PI

    try:
        print(PI.revisar(Path(args.carpeta), max_respuesta=args.largo))
    except (OSError, ValueError, KeyError) as e:
        print(f"No se pudo revisar {args.carpeta}: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_lab_review(args) -> int:
    from .laboratorio import informe

    try:
        print(informe.revisar(Path(args.carpeta)))
    except (OSError, ValueError, KeyError) as e:
        print(f"No se pudo revisar {args.carpeta}: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_lab_compare(args) -> int:
    from .laboratorio import informe

    try:
        a, b = (json.loads(Path(x).read_text()) for x in (args.a, args.b))
    except (OSError, ValueError) as e:
        print(f"No se pudo leer: {e}", file=sys.stderr)
        return 2
    print(informe.comparar(a, b))
    return 0


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


def cmd_oficina(args) -> int:
    """Oficina distribuida, etapa 1 (OFICINA.md): registro de trabajadores y disponibilidad, SIN inferencias.
    Base propia (<datos>/oficina/oficina.db): no toca la base de la aplicación, el laboratorio ni Tailscale."""
    from .oficina import cola as CO
    from .oficina import ensayo as EN
    from .oficina import registro as RG

    cfg = _cfg(args)
    carpeta = Path(args.carpeta) if args.carpeta else cfg.data_dir / "oficina"
    carpeta.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.accion == "ensayo-cola":
        destino = carpeta / "ensayos" / time.strftime("cola-%Y%m%d-%H%M%S")
        return 0 if EN.correr(destino)["ok"] else 1
    if args.accion == "ensayo-almacen":
        from .oficina import ensayo_almacen as EA
        destino = carpeta / "ensayos" / time.strftime("almacen-%Y%m%d-%H%M%S")
        r = EA.correr(destino)
        (destino / "informe.json").write_text(json.dumps(r, ensure_ascii=False, indent=1, default=str))
        print(f"Historial completo (quién, cuándo, por qué): {destino / 'informe.json'}")
        return 0 if r["ok"] else 1
    if args.accion in ("comprobar-programacion", "encolar-programacion", "programar"):
        return _oficina_programacion(args, cfg, carpeta)
    if args.accion == "revisar-existente":
        return _oficina_revision_existente(args, cfg, carpeta)
    if args.accion == "sondear-comprension":
        return _oficina_sonda_comprension(args, cfg, carpeta)
    if args.accion == "reparar-guiado":
        return _oficina_reparacion_guiada(args, cfg, carpeta)
    if args.accion in ("mision", "cola", "recuperar", "cancelar"):
        cola = CO.Cola(carpeta / "oficina.db")
        try:
            if args.accion == "mision":
                plan = json.loads(Path(args.archivo).read_text())
                print(f"Misión {cola.crear_mision(plan['titulo'], plan['tareas'])} creada.")
            elif args.accion == "recuperar":
                tocadas = cola.recuperar()
                print(f"{len(tocadas)} arriendo(s) vencido(s) devuelto(s) a la cola" + (": " + ", ".join(tocadas)
                                                                                        if tocadas else "."))
            elif args.accion == "cancelar":
                cola.cancelar_mision(args.mision)
                print(f"{args.mision}: cancelada.")
            else:
                ms = cola.misiones()
                print(json.dumps(ms, ensure_ascii=False, indent=1, default=str) if args.json else CO.texto_cola(ms))
            return 0
        except (CO.ErrorCola, KeyError, ValueError) as e:
            print(f"oficina: {e}", file=sys.stderr)
            return 2
        finally:
            cola.cerrar()
    reg = RG.Registro(carpeta / "oficina.db")
    try:
        if args.accion == "registrar":
            recursos = {k: v for k, v in (("cpu", args.cpu), ("ram_gb", args.ram_gb), ("gpu", args.gpu),
                                          ("vram_gb", args.vram_gb)) if v is not None}
            t = reg.registrar(args.id, args.url, args.modelo, nodo=args.nodo, recursos=recursos,
                              permitir_local=args.permitir_local)
            print(f"Registrado {t['id']} (capacidad {t['capacidad']}, permisos {', '.join(t['permisos'])}). "
                  "Comprobá su disponibilidad con «oficina estado».")
            return 0
        if args.accion == "retirar":
            t = reg.retirar(args.id)
            print(f"{t['id']}: retirado.")
            return 0
        lista = [reg.comprobar(args.id)] if args.id else reg.comprobar_todos()
        print(json.dumps(lista, ensure_ascii=False, indent=1, default=str) if args.json else RG.texto_estado(lista))
        return 0 if all(t["estado"] == "disponible" for t in lista) else 3
    except RG.ErrorRegistro as e:
        print(f"oficina: {e}", file=sys.stderr)
        return 2
    finally:
        reg.cerrar()


def _oficina_programacion(args, cfg, carpeta) -> int:
    """Etapa 3 (OFICINA.md). «comprobar-programacion» no infiere ni ejecuta código de la IA; «programar» exige
    --autorizo-inferencia-remota y corre UNA tarea de programación con el trabajador indicado."""
    from .oficina import cola as CO
    from .oficina import programador as PG
    from .oficina import registro as RG
    from .oficina import remoto as RM
    from .oficina.misiones_programacion import MISIONES
    from .sandbox.executor import Executor

    if args.accion == "encolar-programacion":
        if args.mision not in MISIONES:
            print(f"oficina: misión desconocida «{args.mision}»; existen: {', '.join(MISIONES)}", file=sys.stderr)
            return 2
        cola = CO.Cola(carpeta / "oficina.db")
        try:
            mid = cola.crear_mision(f"programación: {args.mision}", [{
                "nombre": args.mision, "rol": "programador", "modelo": args.modelo, "carga": {"mision": args.mision},
                "max_intentos": 2, "tiempo_max_s": 300}])
        finally:
            cola.cerrar()
        print(f"Misión {mid} encolada (rol programador, modelo {args.modelo}). No se ejecuta nada hasta «programar».")
        return 0
    reg, cola = RG.Registro(carpeta / "oficina.db"), CO.Cola(carpeta / "oficina.db")
    pasos = []

    def paso(nombre, ok, detalle):
        pasos.append(ok)
        print(f"[{'OK' if ok else 'FALLA'}] {nombre}: {detalle}")
    try:
        try:
            t = reg.comprobar(args.trabajador)
            paso("trabajador (identidad, modelos, latido; sin inferencias)", t["disponible_ahora"],
                 f"{t['estado']} · {t['motivo']} · cargados: {(t.get('comprobacion') or {}).get('cargados')}")
        except RG.ErrorRegistro as e:
            t = None
            paso("trabajador", False, str(e))
        if t:
            paso(f"modelo {args.modelo} declarado e instalado", args.modelo in t["modelos"] and t["disponible_ahora"],
                 ", ".join(t["modelos"]))
            elegido, motivo = RM.elegir({"modelo": args.modelo, "permiso": "inferencia"}, [t])
            paso("planificador", elegido is not None, motivo)
        ex = Executor(cfg)
        rep = ex.self_test()
        paso("sandbox (autodiagnóstico de aislamiento, sin código de la IA)", ex.enabled,
             ex.disabled_reason if not ex.enabled else "bwrap, seccomp y límites verificados")
        if ex.enabled:
            c = PG.comprobar_mision(ex, args.mision)
            paso(f"misión «{args.mision}» con la solución de REFERENCIA", c["ok"],
                 f"{c['casos']} casos ({c['visibles']} visibles, {c['ocultos']} ocultos) · {c['estado']}")
        mem = RM.memoria_disponible_mb()
        print(f"[INFO] memoria disponible en esta máquina: {mem} MiB (la inferencia es remota; el sandbox usa su "
              f"propio límite de {cfg.sandbox_memory_mb} MiB)")
        if args.accion == "comprobar-programacion" or not all(pasos):
            print("Comprobación " + ("completa: listo para autorizar «programar»." if all(pasos) else "con FALLAS."))
            return 0 if all(pasos) else 3
        if not args.autorizo_inferencia_remota:
            print("oficina: «programar» envía inferencias al trabajador remoto y ejecuta en el sandbox el código que "
                  "genere. Agregá --autorizo-inferencia-remota para autorizarlo.", file=sys.stderr)
            return 2
        revisor = None
        if args.revisor:
            from .oficina import revisor as RV
            revisor = RV.ConfigRevisor(PG.modelo_remoto(args.revisor_modelo or args.modelo, num_predict=1024,
                                                        timeout_s=600),
                                       max_revisiones=args.max_revisiones, max_segundos=args.max_segundos_revision,
                                       hipotesis_verificables=args.hipotesis_verificables)
            print(f"[INFO] revisor activado: {revisor.modelo.name}, hasta {revisor.max_revisiones} revisiones y "
                  f"{revisor.max_segundos:.0f} s por misión; interviene solo ante un estancamiento")
        prog = PG.Programador(carpeta, cfg, ex, cola, revisor=revisor)
        r = prog.atender(t)
        if r is None:
            print("No hay tareas de programación pendientes para este trabajador (usá «encolar-programacion»).")
            return 0
        print(json.dumps({k: v for k, v in r.items() if k != "informe"}, ensure_ascii=False))
        inf = r["informe"]
        for i in inf["intentos"]:
            print(f"  intento {i['n']}: {i['test_status']} {i['passed']}/{i['total']} · {i['eval_tokens']} tokens · "
                  f"{(i['gen_ms'] or 0) / 1000:.1f} s" + (f" · {i['error'][:120]}" if i["error"] else ""))
        for rv in (json.loads(PG.Taller(carpeta, cfg, ex, t["url"], PG.modelo_remoto(args.modelo), cliente=object())
                              .files.read_rel(inf["mision_taller"], "mejor/resumen.json") or "{}").get("revisiones") or []):
            print(f"  revisor antes del intento {rv['antes_del_intento']}: {rv['estado']}"
                  + (f" · {rv.get('tokens_generados')} tokens · {rv.get('segundos')} s" if rv["estado"] != "omitida" else "")
                  + (f" · {rv['motivo']}" if rv.get("motivo") else ""))
            if rv.get("contraste"):
                ct = rv["contraste"]
                print(f"    caso sintético: discrepancia {ct['discrepancia']} · hipótesis causal "
                      f"{ct['hipotesis_causal']} · {ct['motivo']}")
            if rv.get("efecto"):
                ef = rv["efecto"]
                print(f"    efecto en el intento {ef['intento']}: {ef['atencion_textual']} · "
                      f"{ef.get('resolucion_funcional')} · {ef['tipo_de_cambio']} "
                      f"(similitud {ef['similitud_con_base']}) · {ef['aprobados']}/{ef['total']}"
                      + (f" · originales {ef['originales']}" if ef.get("originales") else "")
                      + (f" · discrepancia sintética {ef['discrepancia_sintetica']}"
                         if ef.get("discrepancia_sintetica") else ""))
        if inf.get("mejor"):
            print(f"  mejor versión conservada: intento {inf['mejor']['intento']} "
                  f"({inf['mejor']['aprobados']}/{inf['mejor']['total']})")
        if r["resultado"] == "propuesta":
            ok, detalle = PG.verificar(cola, carpeta, cfg, ex, r["tarea"])
            print(f"Verificación central: {'ACEPTADA' if ok else 'RECHAZADA'} — {detalle}")
            return 0 if ok else 1
        return 1
    finally:
        reg.cerrar()
        cola.cerrar()


def _oficina_revision_existente(args, cfg, carpeta) -> int:
    """Experimento 7 (OFICINA.md): sin --autorizo-inferencia-remota solo PREPARA y comprueba (sin inferencias ni
    escribir nada); con la autorización, corre el recorrido revisor → contraste → programador → verificación en
    <carpeta>/experimento-7/corrida-<fecha>/."""
    from .oficina import programador as PG
    from .oficina import registro as RG
    from .oficina import remoto as RM
    from .oficina import revision_existente as RE
    from .sandbox.executor import Executor

    pasos = []

    def paso(nombre, ok, detalle):
        pasos.append(ok)
        print(f"[{'OK' if ok else 'FALLA'}] {nombre}: {detalle}")
    print(f"{RE.VERSION_EXPERIMENTO} · variante {args.variante} ({RE.VARIANTES[args.variante]}): misión {args.mision}"
          + (f", código inicial {args.codigo}" if args.codigo else ", código inicial: referencia de la misión")
          + (f", defecto controlado «{args.defecto}»" if args.defecto else ", sin defecto controlado"))
    previos = [x for x in ("taller.db", "taller") if (carpeta / x).exists()]
    paso("laboratorio aislado", not previos, f"{carpeta}" + (f" tiene «{previos[0]}» de un experimento anterior: "
                                                             "usá una carpeta nueva" if previos else ""))
    codigo = None
    if args.codigo:
        try:
            codigo = Path(args.codigo).read_text()
        except OSError as e:
            print(f"oficina: no se pudo leer {args.codigo}: {e}", file=sys.stderr)
            return 2
    reg = RG.Registro(carpeta / "oficina.db")
    try:
        try:
            t = reg.comprobar(args.trabajador)
            paso("trabajador (identidad, modelos, latido; sin inferencias)", t["disponible_ahora"],
                 f"{t['estado']} · {t['motivo']}")
        except RG.ErrorRegistro as e:
            t = None
            paso("trabajador", False, str(e))
    finally:
        reg.cerrar()
    modelos = {args.modelo, args.revisor_modelo or args.modelo}
    if t:
        for m in sorted(modelos):
            elegido, motivo = RM.elegir({"modelo": m, "permiso": "inferencia"}, [t])
            paso(f"planificador ({m})", elegido is not None, motivo)
    ex = Executor(cfg)
    ex.self_test()
    paso("sandbox (autodiagnóstico de aislamiento, sin código de la IA)", ex.enabled,
         ex.disabled_reason if not ex.enabled else "bwrap, seccomp y límites verificados")
    prep = None
    if ex.enabled:
        try:
            prep = RE.preparar(ex, args.mision, codigo, args.defecto)
        except RE.ErrorExperimento as e:
            paso("solución inicial", False, str(e))
        if prep:
            r = prep["referencia"]
            paso("referencia de la misión contra la batería completa", r["ok"], f"{r['aprobados']}/{r['total']}")
            paso("solución inicial con un defecto funcional verificable (el resultado no llega a los modelos)",
                 prep["defecto_verificable"], prep["motivo"])
    if not args.autorizo_inferencia_remota or not all(pasos):
        print("Preparación " + ("completa: listo para autorizar el experimento 7 con --autorizo-inferencia-remota."
                                if all(pasos) else "con FALLAS: no se envía ninguna inferencia."))
        return 0 if all(pasos) else 3
    try:
        destino = RE.carpeta_de_corrida(carpeta)
    except RE.ErrorExperimento as e:
        print(f"oficina: {e}", file=sys.stderr)
        return 2
    conf = RE.ConfigExperimento(
        programador=PG.modelo_remoto(args.modelo),
        revisor=PG.modelo_remoto(args.revisor_modelo or args.modelo, num_predict=RE.NUM_PREDICT_REVISOR,
                                 timeout_s=600),
        mision=args.mision, max_revisiones=args.max_revisiones, max_reparaciones=args.max_reparaciones,
        max_segundos=args.max_segundos, variante=args.variante)
    print(f"[INFO] corrida en {destino}: hasta {conf.max_revisiones} revisiones y {conf.max_reparaciones} "
          f"reparaciones, {conf.max_segundos:.0f} s de inferencia")
    inf = RE.Experimento(destino, ex, RM.ClienteRemoto(t["url"]), RM.compuerta(t["url"]), conf).correr(prep)
    ver = inf.get("verificacion")
    if ver:
        print(f"Verificación independiente del intento {ver['intento']}: {ver['aprobados']}/{ver['total']} "
              + ("APROBADA" if ver["ok"] else "NO aprobada"))
    if inf.get("detenido"):
        print(f"Detenido: {inf['detenido']}")
    for ll in inf["consumo"]["llamadas"]:
        print(f"  {ll['rol']}" + (f" ({ll['etapa']} {ll['reparacion']})" if ll.get("etapa") else "")
              + f": {ll['tokens_prompt']} tokens de entrada · {ll['tokens_generados']} generados · "
              f"{ll['segundos']} s" + (" · CORTADA" if ll["cortada"] else "") + (f" · {ll['error']}" if ll["error"] else ""))
    for nombre, ok in inf["criterios"].items():
        print(f"  criterio {nombre}: {'sí' if ok else 'no'}")
    a = inf["auditoria_de_fugas"]
    print(f"  auditoría de fugas: {'sin fugas' if a['sin_fugas'] else 'FUGAS: ' + ', '.join(a['hallazgos'])} "
          f"({a['pedidos_revisados']} pedidos, {a['marcas_buscadas']} marcas)")
    print(f"Experimento 7: {'ÉXITO' if inf['exito'] else 'SIN ÉXITO'} · registro completo en {destino / 'informe.json'}")
    return 0 if inf["exito"] else 1


def _oficina_sonda_comprension(args, cfg, carpeta) -> int:
    """Experimento 7.4 (OFICINA.md): UNA pregunta de comprensión al programador sobre una corrida terminada. Sin
    --autorizo-inferencia-remota solo comprueba y muestra el pedido (no infiere ni escribe)."""
    from .oficina import programador as PG
    from .oficina import registro as RG
    from .oficina import remoto as RM
    from .oficina import sonda_comprension as SC

    print(f"{SC.VERSION_SONDA}: corrida {args.corrida}, modelo {args.modelo}")
    try:
        f = SC.fuentes(Path(args.corrida))
    except SC.ErrorSonda as e:
        print(f"oficina: {e}", file=sys.stderr)
        return 2
    msgs = SC.mensajes(f)
    print(f"[OK] fuentes (solo lectura): misión {f['mision']}, revisión {f['revision']} con la discrepancia "
          f"demostrada, zonas {', '.join(f'{a}–{b}' for a, b in f['zonas'])}")
    print(f"[INFO] pedido: {sum(len(m['content']) for m in msgs)} caracteres; sin código pedido, sin evidencia de "
          "la batería ni el defecto")
    reg = RG.Registro(carpeta / "oficina.db")
    try:
        t = reg.comprobar(args.trabajador)
    except RG.ErrorRegistro as e:
        print(f"oficina: {e}", file=sys.stderr)
        return 3
    finally:
        reg.cerrar()
    elegido, motivo = RM.elegir({"modelo": args.modelo, "permiso": "inferencia"}, [t])
    print(f"[{'OK' if elegido else 'FALLA'}] trabajador y planificador: {motivo}")
    if not elegido:
        return 3
    if not args.autorizo_inferencia_remota:
        if args.mostrar_pedido:
            print("\n".join(f"--- {m['role']} ---\n{m['content']}" for m in msgs))
        print("Preparación completa: listo para autorizar la sonda con --autorizo-inferencia-remota.")
        return 0
    try:
        destino = SC.carpeta_de_sonda(carpeta, Path(args.corrida))
        r = SC.correr(destino, f, RM.ClienteRemoto(t["url"]), RM.compuerta(t["url"]), PG.modelo_remoto(args.modelo))
    except SC.ErrorSonda as e:
        print(f"oficina: {e}", file=sys.stderr)
        return 2
    m = r["metadatos"]
    if not r["ok"]:
        print(f"La inferencia falló: {m.get('error')}")
        return 1
    ev = r["evaluacion"]
    print(f"Respuesta: {m['tokens_generados']} tokens · {m['segundos']} s · fin {m['fin']}"
          + (" · CORTADA" if m["cortada"] else ""))
    for k in ("identifica_ausencia_en_aplicados", "observa_que_la_referida_no_esta", "explica_ref_none",
              "cita_la_condicion", "contiene_codigo", "propone_cambios", "solo_parafrasis_del_revisor"):
        print(f"  {k}: {'sí' if ev[k] else 'no'}")
    print(f"Evaluación automática e indicativa; leé {destino / 'respuesta.txt'}")
    return 0


def _oficina_reparacion_guiada(args, cfg, carpeta) -> int:
    """Experimento 7.5 (OFICINA.md): parche mínimo con la causa explícita, a lo sumo 3 intentos. Sin
    --autorizo-inferencia-remota solo comprueba y muestra el pedido (no infiere ni escribe)."""
    from .oficina import programador as PG
    from .oficina import registro as RG
    from .oficina import remoto as RM
    from .oficina import reparacion_guiada as RGD
    from .oficina import revision_existente as RE
    from .oficina.misiones_programacion import MISIONES
    from .sandbox.executor import Executor

    print(f"{RGD.VERSION_NORMALIZACION if args.normalizar_sangria else RGD.VERSION_REPARACION}: corrida "
          f"{args.corrida}, modelo {args.modelo}")
    try:
        f = RGD.preparar(Path(args.corrida))
    except RGD.ErrorReparacion as e:
        print(f"oficina: {e}", file=sys.stderr)
        return 2
    print(f"[OK] fuentes (solo lectura): misión {f['mision']}, zonas {', '.join(f'{a}–{b}' for a, b in f['zonas'])}, "
          "causa explícita del equipo")
    pasos = []
    ex = Executor(cfg)
    ex.self_test()
    pasos.append(ex.enabled)
    print(f"[{'OK' if ex.enabled else 'FALLA'}] sandbox: " + ("bwrap, seccomp y límites verificados" if ex.enabled
                                                             else ex.disabled_reason))
    if ex.enabled:
        fabrica, referencia = MISIONES[f["mision"]]
        ref, ini = RE._bateria(ex, referencia, fabrica()), RE._bateria(ex, f["codigo"], fabrica())
        pasos.append(ref["ok"] and not ini["ok"])
        print(f"[{'OK' if pasos[-1] else 'FALLA'}] batería independiente: la referencia aprueba y el código defectuoso "
              "no (el resultado no llega al modelo)")
    reg = RG.Registro(carpeta / "oficina.db")
    try:
        t = reg.comprobar(args.trabajador)
    except RG.ErrorRegistro as e:
        print(f"oficina: {e}", file=sys.stderr)
        return 3
    finally:
        reg.cerrar()
    elegido, motivo = RM.elegir({"modelo": args.modelo, "permiso": "inferencia"}, [t])
    pasos.append(elegido is not None)
    print(f"[{'OK' if elegido else 'FALLA'}] trabajador y planificador: {motivo}")
    if not all(pasos):
        return 3
    if not args.autorizo_inferencia_remota:
        if args.mostrar_pedido:
            print("\n".join(f"--- {m['role']} ---\n{m['content']}" for m in RGD.mensajes(f)))
        print(f"Preparación completa: listo para autorizar con --autorizo-inferencia-remota (hasta {RGD.MAX_INTENTOS} "
              "inferencias).")
        return 0
    try:
        destino = RGD.carpeta_de_reparacion(carpeta, Path(args.corrida),
                                            "experimento-7-6" if args.normalizar_sangria else "experimento-7-5")
    except RGD.ErrorReparacion as e:
        print(f"oficina: {e}", file=sys.stderr)
        return 2
    inf = RGD.correr(destino, f, ex, RM.ClienteRemoto(t["url"]), RM.compuerta(t["url"]), PG.modelo_remoto(args.modelo),
                     max_segundos=args.max_segundos, normalizar=args.normalizar_sangria)
    for i, ll in zip(inf["intentos"], inf["llamadas"]):
        print(f"  intento {i['intento']}: {i['estado']}" + (f" · {i['motivo']}" if i.get("motivo") else "")
              + (f" · discrepancia {i['discrepancia_sintetica']} · visibles {i['visibles']['aprobados']}/"
                 f"{i['visibles']['total']}" if i["estado"] == "aplicado" else "")
              + (" · sangría normalizada" if i.get("sangria_normalizada") else "")
              + f" · {ll.get('tokens_generados')} tokens · {ll['segundos']} s" + (" · CORTADA" if ll.get("cortada") else ""))
    for k, v in inf["criterios"].items():
        print(f"  criterio {k}: {'sí' if v else 'no'}")
    print(f"Experimento {'7.6' if args.normalizar_sangria else '7.5'}: {'ÉXITO' if inf['exito'] else 'SIN ÉXITO'}"
          + (f" ({inf['clasificacion']})" if inf.get("clasificacion") else "") + f" · {inf['motivo']} · registro en {destino}")
    return 0 if inf["exito"] else 1


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
    a = sub.add_parser("explorer-hecho", help="Procedencia e historial de un hecho del mapa (solo lectura)")
    a.add_argument("conn")
    a.add_argument("id", type=int)
    a.set_defaults(fn=cmd_explorer_fact)
    a = sub.add_parser("explorer-reevaluar", help="Reevalúa la memoria con las reglas actuales (por defecto: simulación)")
    a.add_argument("conn")
    a.add_argument("--solo", help="limitar a estos objetos, p. ej. F106,C61")
    a.add_argument("--json", action="store_true")
    a.add_argument("--aplicar", action="store_true", help="registrar el plan (exige --plan de una simulación)")
    a.add_argument("--plan", help="código del plan simulado que se autoriza")
    a.set_defaults(fn=cmd_explorer_reevaluate)
    a = sub.add_parser("investigar", help="Investigación EXPERIMENTAL con el modelo real (ciclos cortos, sin aplicaciones)")
    a.add_argument("conn")
    a.add_argument("--ciclos", type=int, default=4)
    a.add_argument("--minutos", type=int, default=15)
    a.add_argument("--reanudar", help="misión cuya investigación se continúa")
    a.add_argument("--objetivo", help="texto del objetivo (por defecto, investigar por iniciativa propia)")
    a.add_argument("--yes", action="store_true")
    a.set_defaults(fn=cmd_investigate)
    a = sub.add_parser("investigacion-report", help="Ciclos, dependencias y costo de una investigación (solo lectura)")
    a.add_argument("mission")
    a.set_defaults(fn=cmd_investigation_report)
    a = sub.add_parser("ollama-diag", help="Diagnóstico de solo lectura del Ollama CPU (memoria, swap, PSI, CPU)")
    a.add_argument("--segundos", type=float, default=1200, help="duración del muestreo (default 1200)")
    a.add_argument("--intervalo", type=float, default=1.0)
    a.add_argument("--pid", type=int, help="PID del servidor Ollama CPU si no se detecta solo")
    a.add_argument("--salida", help="archivo .jsonl de muestras (default: logs/ollama-diag-*.jsonl)")
    a.add_argument("--mision", help="cruzar con las llamadas registradas de esta exploración")
    a.add_argument("--analizar", help="analizar un .jsonl ya capturado en vez de muestrear")
    a.set_defaults(fn=cmd_ollama_diag)
    a = sub.add_parser("explorer-trial", help="Expedición de ensayo sin interfaz con el modelo real")
    a.add_argument("conn")
    a.add_argument("--minutes", type=int, default=20)
    a.add_argument("--steps", type=int, default=15)
    a.add_argument("--queries", type=int, default=30)
    a.add_argument("--mission-text", dest="mission_text")
    a.add_argument("--yes", action="store_true")
    a.set_defaults(fn=cmd_explorer_trial)
    a = sub.add_parser("laboratorio-instrumentos",
                       help="Fase 1: compara modelos escribiendo instrumentos sobre una base sintética (MariaDB privado)")
    a.add_argument("--modelos", default="qwen3:4b-instruct,qwen3:1.7b", help="separados por comas")
    a.add_argument("--tareas", default="T1,T2,T3,T4,T5,C,R", help="subconjunto de T1,T2,T3,T4,T5,C,R")
    a.add_argument("--repeticiones", type=int, default=1)
    a.add_argument("--semilla", type=int, default=7)
    a.add_argument("--escala", choices=["pequena", "normal", "grande"], default="normal")
    a.add_argument("--correcciones", type=int, default=3,
                   help="máximo de versiones nuevas por tarea (reparaciones y revisiones del método)")
    a.add_argument("--reparaciones-formato", dest="reparaciones_formato", type=int, default=1,
                   help="pedidos para reparar una autoevaluación ilegible (no reescriben el instrumento)")
    a.add_argument("--mismo-error", dest="mismo_error", type=int, default=3,
                   help="corta la reparación cuando el mismo error aparece estas veces seguidas (mínimo 2)")
    a.add_argument("--num-ctx", dest="num_ctx", type=int, help="contexto (igual para todos; default el del explorador)")
    a.add_argument("--temperatura", type=float, help="igual para todos; default la del explorador")
    a.add_argument("--conservar-mariadb", dest="conservar_mariadb", action="store_true",
                   help="no borrar el datadir privado al terminar (para inspección)")
    a.add_argument("--yes", action="store_true")
    a.set_defaults(fn=cmd_lab_instruments)
    a = sub.add_parser("laboratorio-mutantes",
                       help="Estudio sin modelo: cuánto detecta y localiza el coordinador solo (MariaDB privado)")
    a.add_argument("--salida")
    a.add_argument("--yes", action="store_true")
    a.set_defaults(fn=cmd_lab_mutants)
    a = sub.add_parser("laboratorio-piloto", help="Piloto preregistrado de microtareas (PILOTO.md), modelo real")
    a.add_argument("--modelo", default="qwen3:4b-instruct")
    a.add_argument("--defectos", default="D1,D2")
    a.add_argument("--repeticiones", type=int, default=2)
    a.add_argument("--brazos", default="A,B,B*,C,D")
    a.add_argument("--reanudar", help="carpeta de un piloto interrumpido: se saltan los episodios ya hechos")
    a.add_argument("--sin-mutantes", dest="sin_mutantes", action="store_true")
    a.add_argument("--protocolo", choices=["v1", "v2"], default="v2",
                   help="v2 (Enmienda 3) por defecto; v1 reproduce el ensayo original")
    a.add_argument("--num-ctx", dest="num_ctx", type=int, help="contexto (v2: 4096; v1: 6144)")
    a.add_argument("--keep-alive", dest="keep_alive", help="por pedido a Ollama (v2: 0s = descargar tras cada llamada)")
    a.add_argument("--memoria-minima-mb", dest="memoria_minima_mb", type=int,
                   help="memoria disponible exigida antes de cada llamada (v2: 3900; 0 desactiva)")
    a.add_argument("--ensayo", action="store_true",
                   help="ensayo técnico pequeño (D2, 1 repetición, 2 llamadas por episodio), EXCLUIDO del análisis")
    a.add_argument("--yes", action="store_true")
    a.set_defaults(fn=cmd_lab_pilot)
    a = sub.add_parser("laboratorio-eleccion-etapa0",
                       help="Experimento de elección, Etapa 0 (sin modelo): casos y calidad de la traza")
    a.add_argument("--salida")
    a.add_argument("--yes", action="store_true")
    a.set_defaults(fn=cmd_lab_choice_stage0)
    a = sub.add_parser("laboratorio-eleccion",
                       help="Experimento de elección, Etapa 1: ¿la traza ayuda a elegir la causa? (exige --limite o "
                            "--completo)")
    a.add_argument("--casos", help="casos.json de la Etapa 0")
    a.add_argument("--reanudar", help="carpeta de una Etapa 1 empezada")
    a.add_argument("--limite", type=int, help="correr como máximo N ensayos nuevos (prueba de rendimiento)")
    a.add_argument("--completo", action="store_true", help="correr todos los ensayos planificados que falten")
    a.add_argument("--modelo", default="qwen3:4b-instruct")
    a.add_argument("--repeticiones", type=int, default=2)
    a.add_argument("--num-ctx", dest="num_ctx", type=int, default=4096)
    a.add_argument("--keep-alive", dest="keep_alive", default="0s")
    a.add_argument("--memoria-minima-mb", dest="memoria_minima_mb", type=int, default=3900)
    a.add_argument("--yes", action="store_true")
    a.set_defaults(fn=cmd_lab_choice)
    a = sub.add_parser("laboratorio-eleccion-e0",
                       help="E0 (sin modelo): ¿una regla fija elige la causa con la evidencia del coordinador?")
    a.add_argument("--salida")
    a.add_argument("--defectos", help="carpeta con manifiesto.json de defectos externos (evaluación a ciegas)")
    a.add_argument("--yes", action="store_true")
    a.set_defaults(fn=cmd_lab_choice_e0)
    a = sub.add_parser("laboratorio-ciclo",
                       help="Ciclo v1.3.4: diagnóstico estructurado → reparación (Qwen) → coherencia y verificación (coordinador) → juez separado")
    a.add_argument("--defectos", help="carpeta con manifiesto.json (p. ej. el banco de desarrollo)")
    a.add_argument("--ids", help="solo estos defectos, separados por comas (prueba pequeña)")
    a.add_argument("--reanudar", help="carpeta de un ciclo empezado")
    a.add_argument("--sin-modelo", dest="sin_modelo", action="store_true",
                   help="observación, reglas v1 de punta a punta y juez; 0 llamadas")
    a.add_argument("--limite-llamadas", dest="limite_llamadas", type=int, help="como máximo N llamadas NUEVAS")
    a.add_argument("--completo", action="store_true", help="todas las llamadas que falten")
    a.add_argument("--llamadas-por-defecto", dest="llamadas_por_defecto", type=int, default=3,
                   help="presupuesto de llamadas de cada investigación (1 a 6)")
    a.add_argument("--modelo", default="qwen3:4b-instruct")
    a.add_argument("--num-ctx", dest="num_ctx", type=int, default=4096)
    a.add_argument("--keep-alive", dest="keep_alive", default="0s")
    a.add_argument("--memoria-minima-mb", dest="memoria_minima_mb", type=int, default=3900)
    a.add_argument("--yes", action="store_true")
    a.set_defaults(fn=cmd_lab_cycle)
    a = sub.add_parser("laboratorio-agente",
                       help="Agente de datos, fase 2: --preparar, --comprobar (sin modelo), --mision M1|M2|M2p, --estado")
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--preparar", action="store_true", help="base sintética B1, verdad oculta y huella de los datos")
    g.add_argument("--comprobar", action="store_true",
                   help="SIN inferencias: MariaDB privado, solo lectura, datos idénticos, misión de prueba con guion")
    g.add_argument("--mision", choices=["M1", "M2", "M2p"], help="una misión (una sola vez por carpeta)")
    g.add_argument("--estado", action="store_true", help="resumen de la carpeta (solo lectura)")
    a.add_argument("--carpeta", help="carpeta del experimento (por omisión, <datos>/laboratorio/agente-fase2)")
    a.add_argument("--guion", help="usar el modelo de guion con este perfil (pruebas SIN inferencias)")
    a.add_argument("--nombre-modelo", dest="nombre_modelo", default="qwen3:4b-instruct")
    a.add_argument("--num-ctx", dest="num_ctx", type=int, default=4096)
    a.add_argument("--keep-alive", dest="keep_alive", default="0s")
    a.add_argument("--memoria-minima-mb", dest="memoria_minima_mb", type=int, default=3900)
    a.add_argument("--max-llamadas", dest="max_llamadas", type=int, default=8)
    a.add_argument("--yes", action="store_true")
    a.set_defaults(fn=cmd_lab_agent)
    a = sub.add_parser("oficina", help="Oficina distribuida, etapa 1: registrar trabajadores y comprobar su "
                                       "disponibilidad (sin inferencias)")
    osub = a.add_subparsers(dest="accion", required=True)
    o = osub.add_parser("registrar", help="registra un trabajador del tailnet (comprueba su identidad con whois)")
    o.add_argument("--id", required=True, help="nombre corto del trabajador (minúsculas)")
    o.add_argument("--url", required=True, help="http://<IP 100.x o nombre *.ts.net>:<puerto> de Tailscale Serve")
    o.add_argument("--modelo", action="append", required=True, help="modelo declarado (repetible)")
    o.add_argument("--nodo", help="nombre del nodo de Tailscale esperado (obligatorio salvo loopback de prueba)")
    o.add_argument("--cpu")
    o.add_argument("--ram-gb", dest="ram_gb", type=float)
    o.add_argument("--gpu")
    o.add_argument("--vram-gb", dest="vram_gb", type=float)
    o.add_argument("--permitir-local", dest="permitir_local", action="store_true", help="solo pruebas: loopback")
    o.add_argument("--carpeta", help="carpeta de la oficina (por omisión, <datos>/oficina)")
    o = osub.add_parser("estado", help="comprueba identidad, /api/version, /api/tags y /api/ps (sin inferencias)")
    o.add_argument("--id", help="solo este trabajador")
    o.add_argument("--json", action="store_true")
    o.add_argument("--carpeta", help="carpeta de la oficina (por omisión, <datos>/oficina)")
    o = osub.add_parser("retirar", help="retira un trabajador del registro (queda su historial)")
    o.add_argument("--id", required=True)
    o.add_argument("--carpeta", help="carpeta de la oficina (por omisión, <datos>/oficina)")
    o = osub.add_parser("mision", help="crea una misión desde un JSON {titulo, tareas:[{nombre, rol, modelo, carga, "
                                       "depende_de, max_intentos, tiempo_max_s}]} (no ejecuta nada)")
    o.add_argument("--archivo", required=True)
    o.add_argument("--carpeta", help="carpeta de la oficina (por omisión, <datos>/oficina)")
    o = osub.add_parser("cola", help="misiones y tareas con su estado (solo lectura)")
    o.add_argument("--json", action="store_true")
    o.add_argument("--carpeta", help="carpeta de la oficina (por omisión, <datos>/oficina)")
    o = osub.add_parser("recuperar", help="devuelve a la cola las tareas con el arriendo vencido")
    o.add_argument("--carpeta", help="carpeta de la oficina (por omisión, <datos>/oficina)")
    o = osub.add_parser("cancelar", help="cancela las tareas no terminadas de una misión")
    o.add_argument("--mision", required=True)
    o.add_argument("--carpeta", help="carpeta de la oficina (por omisión, <datos>/oficina)")
    o = osub.add_parser("ensayo-cola", help="ensayo con trabajadores SIMULADOS en una base desechable propia (sin red "
                                            "ni inferencias)")
    o.add_argument("--carpeta", help="carpeta de la oficina (el ensayo va en <carpeta>/ensayos/cola-<fecha>)")
    o = osub.add_parser("ensayo-almacen", help="escenario estándar: 20 ventas de un almacén, misiones A–F y nueve "
                                               "trabajadores simulados (base desechable, sin red ni inferencias)")
    o.add_argument("--carpeta", help="carpeta de la oficina (el ensayo va en <carpeta>/ensayos/almacen-<fecha>)")
    for nombre, ayuda in (("comprobar-programacion", "etapa 3, SIN inferencias ni código de la IA: trabajador, "
                                                      "planificador, sandbox y misión con la solución de referencia"),
                          ("programar", "etapa 3: UNA tarea de programación con el trabajador remoto (exige "
                                        "--autorizo-inferencia-remota)")):
        o = osub.add_parser(nombre, help=ayuda)
        o.add_argument("--trabajador", required=True)
        o.add_argument("--modelo", default="gemma4:12b")
        o.add_argument("--mision", default="resumen_ventas")
        o.add_argument("--autorizo-inferencia-remota", dest="autorizo_inferencia_remota", action="store_true")
        o.add_argument("--revisor", action="store_true", help="activa el rol revisor (solo ante un estancamiento)")
        o.add_argument("--revisor-modelo", dest="revisor_modelo", help="modelo del revisor (por omisión, el mismo)")
        o.add_argument("--max-revisiones", dest="max_revisiones", type=int, default=2)
        o.add_argument("--max-segundos-revision", dest="max_segundos_revision", type=float, default=1200.0)
        o.add_argument("--hipotesis-verificables", dest="hipotesis_verificables", action="store_true",
                       help="experimento 6: el revisor propone un caso sintético que el coordinador contrasta")
        o.add_argument("--carpeta", help="carpeta de la oficina (por omisión, <datos>/oficina)")
    o = osub.add_parser("revisar-existente", help="experimento 7: el revisor examina una solución existente, "
                                                  "el coordinador contrasta su caso y el programador repara (sin "
                                                  "--autorizo-inferencia-remota solo prepara, sin inferencias)")
    o.add_argument("--carpeta", required=True, help="laboratorio propio (no la carpeta de un experimento anterior)")
    o.add_argument("--trabajador", required=True)
    o.add_argument("--modelo", default="gemma4:12b")
    o.add_argument("--revisor-modelo", dest="revisor_modelo", help="modelo del revisor (por omisión, el mismo)")
    o.add_argument("--mision", default="conciliar_movimientos")
    o.add_argument("--codigo", help="archivo Python con la solución inicial (por omisión, la referencia de la misión)")
    o.add_argument("--defecto", help="defecto controlado a introducir (ver OFICINA.md, «Experimento 7»)")
    o.add_argument("--max-revisiones", dest="max_revisiones", type=int, default=2)
    o.add_argument("--max-reparaciones", dest="max_reparaciones", type=int, default=3)
    o.add_argument("--max-segundos", dest="max_segundos", type=float, default=2400.0,
                   help="tiempo total de inferencia (revisor + programador)")
    o.add_argument("--variante", choices=["7.1", "7.2", "7.3"], default="7.1",
                   help="7.1: reparación directa (por omisión) · 7.2: diagnóstico previo del programador y reparación "
                        "· 7.3: parches mínimos sobre zonas autorizadas")
    o.add_argument("--autorizo-inferencia-remota", dest="autorizo_inferencia_remota", action="store_true")
    o = osub.add_parser("sondear-comprension", help="experimento 7.4: pregunta de comprensión causal al programador "
                                                    "sobre una corrida terminada (sin código ni parches; sin "
                                                    "--autorizo-inferencia-remota no infiere)")
    o.add_argument("--corrida", required=True, help="carpeta corrida-* de revisar-existente (solo lectura)")
    o.add_argument("--carpeta", required=True, help="laboratorio propio para la sonda (no la corrida)")
    o.add_argument("--trabajador", required=True)
    o.add_argument("--modelo", default="qwen2.5-coder:7b")
    o.add_argument("--mostrar-pedido", dest="mostrar_pedido", action="store_true",
                   help="sin inferencia: imprime el pedido completo")
    o.add_argument("--autorizo-inferencia-remota", dest="autorizo_inferencia_remota", action="store_true")
    o = osub.add_parser("reparar-guiado", help="experimento 7.5: parche mínimo con la causa explícita sobre las zonas "
                                               "de una corrida 7.3 (a lo sumo 3 intentos; sin "
                                               "--autorizo-inferencia-remota no infiere)")
    o.add_argument("--corrida", required=True, help="carpeta corrida-* de revisar-existente 7.3 (solo lectura)")
    o.add_argument("--carpeta", required=True, help="laboratorio propio (no la corrida)")
    o.add_argument("--trabajador", required=True)
    o.add_argument("--modelo", default="qwen2.5-coder:7b")
    o.add_argument("--max-segundos", dest="max_segundos", type=float, default=2400.0)
    o.add_argument("--normalizar-sangria", dest="normalizar_sangria", action="store_true",
                   help="experimento 7.6: si un parche no coincide literalmente, ajustar SOLO su sangría inicial")
    o.add_argument("--mostrar-pedido", dest="mostrar_pedido", action="store_true")
    o.add_argument("--autorizo-inferencia-remota", dest="autorizo_inferencia_remota", action="store_true")
    o = osub.add_parser("encolar-programacion", help="encola una misión de programación (no ejecuta nada)")
    o.add_argument("--mision", default="resumen_ventas")
    o.add_argument("--modelo", default="gemma4:12b")
    o.add_argument("--carpeta", help="carpeta de la oficina (por omisión, <datos>/oficina)")
    a.set_defaults(fn=cmd_oficina)
    a = sub.add_parser("laboratorio-sugerencias",
                       help="Experimento de sugerencias (ciclo v1.3.4): «localizador» contra «evidencia», plan fijo")
    a.add_argument("--tanda", required=True, choices=["1", "2", "control"],
                   help="1: X05 y X08 · 2: X11 y X12 · control: X07 con la sugerencia 17")
    a.add_argument("--carpeta", help="carpeta del experimento (una subcarpeta por tanda)")
    a.add_argument("--solo-prompts", dest="solo_prompts", action="store_true",
                   help="sin modelo: observa y compara los prompts de cada pareja; no detiene nada")
    a.add_argument("--modelo", default="qwen3:4b-instruct")
    a.add_argument("--num-ctx", dest="num_ctx", type=int, default=4096)
    a.add_argument("--keep-alive", dest="keep_alive", default="0s")
    a.add_argument("--memoria-minima-mb", dest="memoria_minima_mb", type=int, default=3900)
    a.add_argument("--yes", action="store_true")
    a.set_defaults(fn=cmd_lab_suggestions)
    a = sub.add_parser("laboratorio-eleccion-forense",
                       help="Análisis forense de SOLO LECTURA de una Etapa 1 terminada (sin modelo)")
    a.add_argument("carpeta")
    a.add_argument("--salida", help="archivo .md (fuera de la carpeta del experimento); por omisión, pantalla")
    a.add_argument("--json", help="además, todos los rasgos por respuesta en JSON (fuera de la carpeta)")
    a.set_defaults(fn=cmd_lab_choice_forensic)
    a = sub.add_parser("laboratorio-piloto-revisar",
                       help="Revisión de solo lectura de un piloto o ensayo: llamadas, prompts, respuestas y autoría")
    a.add_argument("carpeta")
    a.add_argument("--largo", type=int, default=400, help="caracteres de cada respuesta cruda a mostrar")
    a.set_defaults(fn=cmd_lab_pilot_review)
    a = sub.add_parser("laboratorio-revisar",
                       help="Revisión de solo lectura de una corrida: llamadas, textos crudos, código y consultas")
    a.add_argument("carpeta", help="data/laboratorio/<fecha>")
    a.set_defaults(fn=cmd_lab_review)
    a = sub.add_parser("laboratorio-comparar", help="Compara dos resultados.json del laboratorio (solo lectura)")
    a.add_argument("a")
    a.add_argument("b")
    a.set_defaults(fn=cmd_lab_compare)
    a = sub.add_parser("explorer-verify", help="Verifica privilegios y límites de una conexión del Explorador")
    a.add_argument("conn")
    a.set_defaults(fn=cmd_explorer_verify)
    args = p.parse_args(argv)
    return args.fn(args)
