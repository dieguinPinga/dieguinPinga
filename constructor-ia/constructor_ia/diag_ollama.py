"""Diagnóstico de SOLO LECTURA del Ollama CPU de Constructor IA (v0.4.4).

Mide, mientras corre una exploración, si los bloqueos de Ollama se explican por presión de memoria (swap, PSI,
fallos de página, límites del cgroup), por falta de CPU (otros procesos) o por el propio servidor (sin señales del
sistema mientras no responde). No cambia nada: lee /proc, /sys/fs/cgroup y GET /api/ps de la instancia configurada
(ollama.url de Constructor). Se niega a apuntar al Ollama principal (puerto 11434).

Uso: constructor-ia ollama-diag --segundos 1200 [--llamadas exploracion/llamadas_ollama.json]
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

PROC = Path("/proc")
MAIN_OLLAMA_PORT = 11434
PAGE_MIB = os.sysconf("SC_PAGE_SIZE") / 1048576
CLK = os.sysconf("SC_CLK_TCK")


# Lectura de /proc ------------------------------------------------------------------------------------
def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def meminfo() -> dict:
    out = {}
    for line in _read(PROC / "meminfo").splitlines():
        k, _, v = line.partition(":")
        if v.strip():
            out[k] = int(v.split()[0]) // 1024  # MiB
    return {k: out.get(k) for k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree", "SwapCached", "Dirty")}


def vmstat() -> dict:
    vals = dict(line.split() for line in _read(PROC / "vmstat").splitlines() if line.count(" ") == 1)
    return {k: int(vals[k]) for k in ("pswpin", "pswpout", "pgmajfault") if k in vals}


def pressure(text: str) -> dict:
    """Formato PSI: «some avg10=… avg60=… total=…» y «full …»."""
    out = {}
    for line in text.splitlines():
        kind, *fields = line.split()
        vals = dict(f.split("=") for f in fields if "=" in f)
        if "avg10" in vals:
            out[kind] = {"avg10": float(vals["avg10"]), "total_us": int(vals.get("total", 0))}
    return out


def cpu_total() -> int:
    parts = _read(PROC / "stat").splitlines()[0].split()[1:]
    return sum(int(x) for x in parts[:8]) if parts else 0


def proc_stat(pid: int) -> dict | None:
    raw = _read(PROC / str(pid) / "stat")
    if not raw:
        return None
    comm = raw[raw.index("(") + 1:raw.rindex(")")]
    f = raw[raw.rindex(")") + 2:].split()
    status = {}
    for line in _read(PROC / str(pid) / "status").splitlines():
        k, _, v = line.partition(":")
        if k in ("VmRSS", "VmSwap", "Threads"):
            status[k] = int(v.split()[0])
    return {"pid": pid, "comm": comm, "state": f[0], "ppid": int(f[1]), "majflt": int(f[9]),
            "cpu_ticks": int(f[11]) + int(f[12]), "rss_mib": status.get("VmRSS", 0) // 1024,
            "swap_mib": status.get("VmSwap", 0) // 1024, "threads": status.get("Threads")}


def all_pids() -> list[int]:
    return [int(p.name) for p in PROC.iterdir() if p.name.isdigit()]


def cmdline(pid: int) -> str:
    return _read(PROC / str(pid) / "cmdline").replace("\x00", " ").strip()


# Identificación del servidor --------------------------------------------------------------------------
def listening_inodes(port: int) -> set[str]:
    inodes = set()
    for name in ("tcp", "tcp6"):
        for line in _read(PROC / "net" / name).splitlines()[1:]:
            f = line.split()
            if len(f) > 9 and f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(f[9])
    return inodes


def server_pid(port: int) -> int | None:
    """PID que escucha en el puerto (solo visible si el proceso es del mismo usuario o se corre como root)."""
    inodes = listening_inodes(port)
    if not inodes:
        return None
    wanted = {f"socket:[{i}]" for i in inodes}
    for pid in all_pids():
        try:
            for fd in (PROC / str(pid) / "fd").iterdir():
                if os.readlink(fd) in wanted:
                    return pid
        except OSError:
            continue
    return None


def descendants(root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for pid in all_pids():
        st = proc_stat(pid)
        if st:
            children.setdefault(st["ppid"], []).append(pid)
    out, todo = [], [root]
    while todo:
        p = todo.pop()
        out.append(p)
        todo += children.get(p, [])
    return out


def ollama_candidates() -> list[dict]:
    out = []
    for pid in all_pids():
        cl = cmdline(pid)
        if "ollama" in cl.lower():
            out.append({"pid": pid, "cmdline": cl[:160], "uid": (PROC / str(pid)).stat().st_uid})
    return out


def cgroup_dir(pid: int) -> Path | None:
    for line in _read(PROC / str(pid) / "cgroup").splitlines():
        if line.startswith("0::"):
            d = Path("/sys/fs/cgroup") / line[3:].lstrip("/")
            return d if d.is_dir() else None
    return None


def cgroup_sample(d: Path | None) -> dict:
    if d is None:
        return {}
    out = {}
    for name in ("memory.current", "memory.swap.current", "memory.high", "memory.max", "memory.swap.max"):
        v = _read(d / name).strip()
        if v:
            out[name] = v if v == "max" else int(v) // 1048576
    ev = dict(line.split() for line in _read(d / "memory.events").splitlines() if " " in line)
    out["memory.events"] = {k: int(v) for k, v in ev.items()}
    out["memory.pressure"] = pressure(_read(d / "memory.pressure"))
    return out


def api_ps(base_url: str, timeout: float = 2.0) -> dict:
    import http.client

    u = urlsplit(base_url)
    conn = http.client.HTTPConnection(u.hostname or "127.0.0.1", u.port or 11435, timeout=timeout)
    t0 = time.monotonic()
    try:
        conn.request("GET", "/api/ps")
        r = conn.getresponse()
        body = json.loads(r.read(200_000) or b"{}")
        models = [{k: m.get(k) for k in ("name", "size", "size_vram", "context_length", "expires_at")}
                  for m in body.get("models", [])]
        return {"ok": r.status == 200, "ms": int((time.monotonic() - t0) * 1000), "modelos": models}
    except (OSError, ValueError, http.client.HTTPException) as e:
        return {"ok": False, "ms": int((time.monotonic() - t0) * 1000), "error": str(e)[:120]}
    finally:
        conn.close()


# Muestreo --------------------------------------------------------------------------------------------
def sample(pids: list[int], cg: Path | None) -> dict:
    procs = [st for st in (proc_stat(p) for p in pids) if st]
    return {
        "t": time.time(), "cpu_total": cpu_total(), "mem": meminfo(), "vm": vmstat(),
        "psi": {k: pressure(_read(PROC / "pressure" / k)) for k in ("memory", "cpu", "io")},
        "load": _read(PROC / "loadavg").split()[:3],
        "ollama": {"rss_mib": sum(p["rss_mib"] for p in procs), "swap_mib": sum(p["swap_mib"] for p in procs),
                   "majflt": sum(p["majflt"] for p in procs), "cpu_ticks": sum(p["cpu_ticks"] for p in procs),
                   "estados": "".join(sorted(p["state"] for p in procs)), "procesos": len(procs)},
        "cgroup": cgroup_sample(cg),
    }


def top_cpu(prev: dict[int, int], exclude: set[int]) -> tuple[list[dict], dict[int, int]]:
    """Procesos (ajenos a Ollama CPU) que más CPU usaron desde la muestra anterior."""
    now, deltas = {}, []
    for pid in all_pids():
        st = proc_stat(pid)
        if not st:
            continue
        now[pid] = st["cpu_ticks"]
        if pid in prev and pid not in exclude:
            d = st["cpu_ticks"] - prev[pid]
            if d > 0:
                deltas.append({"pid": pid, "comm": st["comm"], "ticks": d})
    return sorted(deltas, key=lambda x: -x["ticks"])[:5], now


def collect(base_url: str, seconds: float, interval: float, pid: int | None, out_path: Path, log=print) -> dict:
    port = urlsplit(base_url).port or 11435
    if port == MAIN_OLLAMA_PORT:
        raise SystemExit("ollama-diag no se usa sobre el Ollama principal (puerto 11434).")
    root = pid or server_pid(port)
    if root is None:
        cands = ollama_candidates()
        log(f"No pude identificar el proceso que escucha en {port} (si es de otro usuario, corré con --pid).")
        for c in cands:
            log(f"  candidato pid={c['pid']} uid={c['uid']}: {c['cmdline']}")
    pids = descendants(root) if root else []
    cg = cgroup_dir(root) if root else None
    meta = {"inicio": time.time(), "url": base_url, "pid": root, "procesos": [cmdline(p)[:160] for p in pids],
            "cgroup": str(cg) if cg else None, "nucleos": os.cpu_count(), "clk": CLK}
    log(f"Ollama CPU: pid {root} ({len(pids)} procesos), cgroup {cg}. Muestreo cada {interval} s durante {seconds:.0f} s"
        f" → {out_path}")
    prev_cpu: dict[int, int] = {}
    _, prev_cpu = top_cpu({}, set())
    t_end = time.monotonic() + seconds
    n = 0
    with out_path.open("w") as f:
        f.write(json.dumps({"meta": meta}) + "\n")
        try:
            while time.monotonic() < t_end:
                if root and n % 10 == 0:
                    pids = descendants(root)  # el servidor puede lanzar o cerrar procesos hijos
                s = sample(pids, cg)
                if n % 5 == 0:
                    s["api_ps"] = api_ps(base_url)
                if n % 10 == 0:
                    s["top_cpu"], prev_cpu = top_cpu(prev_cpu, set(pids))
                f.write(json.dumps(s) + "\n")
                f.flush()
                n += 1
                time.sleep(max(0.0, interval - 0.05))
        except KeyboardInterrupt:
            log("Interrumpido: se resume lo medido hasta ahora")
    return load(out_path)


def load(path: Path) -> dict:
    meta, samples = {}, []
    for line in path.read_text().splitlines():
        d = json.loads(line)
        if "meta" in d:
            meta = d["meta"]
        else:
            samples.append(d)
    return {"meta": meta, "muestras": samples}


# Análisis --------------------------------------------------------------------------------------------
def _window_stats(samples: list[dict], cores: int, clk: int) -> dict:
    if len(samples) < 2:
        return {"muestras": len(samples)}
    a, b = samples[0], samples[-1]
    secs = max(1e-6, b["t"] - a["t"])
    vm = lambda k: b["vm"].get(k, 0) - a["vm"].get(k, 0)  # noqa: E731
    used = [s["mem"]["SwapTotal"] - s["mem"]["SwapFree"] for s in samples if s["mem"].get("SwapTotal") is not None]
    psi = lambda res, kind: max((s["psi"].get(res, {}).get(kind, {}).get("avg10", 0.0) for s in samples), default=0.0)  # noqa: E731
    stall = lambda res, kind: (b["psi"].get(res, {}).get(kind, {}).get("total_us", 0)  # noqa: E731
                               - a["psi"].get(res, {}).get(kind, {}).get("total_us", 0)) / 1e6
    cpu_ollama = (b["ollama"]["cpu_ticks"] - a["ollama"]["cpu_ticks"]) / clk / secs  # núcleos usados en promedio
    cpu_all = (b["cpu_total"] - a["cpu_total"]) / clk / secs  # núcleos-segundo totales por segundo (≈ núcleos)
    ev = lambda k: (b["cgroup"].get("memory.events", {}).get(k, 0) - a["cgroup"].get("memory.events", {}).get(k, 0))  # noqa: E731
    return {
        "segundos": round(secs, 1), "muestras": len(samples),
        "memoria_disponible_min_mib": min((s["mem"]["MemAvailable"] or 0) for s in samples),
        "swap_usado_max_mib": max(used, default=0), "swap_leido_mib": round(vm("pswpin") * PAGE_MIB, 1),
        "swap_escrito_mib": round(vm("pswpout") * PAGE_MIB, 1), "fallos_mayores_sistema": vm("pgmajfault"),
        "ollama_fallos_mayores": b["ollama"]["majflt"] - a["ollama"]["majflt"],
        "ollama_swap_max_mib": max(s["ollama"]["swap_mib"] for s in samples),
        "ollama_rss_max_mib": max(s["ollama"]["rss_mib"] for s in samples),
        "ollama_cpu_nucleos": round(cpu_ollama, 2), "cpu_sistema_ocupada": round(cpu_all / max(1, cores), 2),
        "psi_memoria_some_max": psi("memory", "some"), "psi_memoria_full_max": psi("memory", "full"),
        "psi_cpu_some_max": psi("cpu", "some"), "psi_io_some_max": psi("io", "some"),
        "psi_memoria_full_s": round(stall("memory", "full"), 1), "psi_io_full_s": round(stall("io", "full"), 1),
        "cgroup_eventos_high": ev("high"), "cgroup_eventos_max": ev("max"), "cgroup_oom": ev("oom_kill"),
    }


def verdict(st: dict, cores: int) -> list[str]:
    """Indicios a partir de lo MEDIDO (cada uno cita sus cifras). No es una conclusión automática."""
    out = []
    if st.get("muestras", 0) < 2:
        return ["muy pocas muestras para concluir"]
    secs = st["segundos"]
    mem_stalled = st["psi_memoria_full_s"] >= 0.05 * secs or st["psi_memoria_full_max"] >= 5
    swapping = st["swap_leido_mib"] >= 50 or st["ollama_fallos_mayores"] >= 1000
    if swapping or mem_stalled:
        out.append(f"PRESIÓN DE MEMORIA: {st['swap_leido_mib']} MiB leídos de swap, {st['ollama_fallos_mayores']} fallos "
                   f"mayores de Ollama, tareas trabadas por memoria {st['psi_memoria_full_s']} s de {secs} s "
                   f"(PSI full máx {st['psi_memoria_full_max']}%)")
    elif st["swap_usado_max_mib"] > 0:
        out.append(f"Hay {st['swap_usado_max_mib']} MiB en swap pero casi sin lecturas ({st['swap_leido_mib']} MiB) ni "
                   "esperas por memoria: el swap ocupado NO explica la lentitud en esta ventana")
    if st["cgroup_eventos_high"] or st["cgroup_eventos_max"]:
        out.append(f"LÍMITE DEL CGROUP de Ollama alcanzado: memory.events high +{st['cgroup_eventos_high']}, "
                   f"max +{st['cgroup_eventos_max']} (estrangulamiento por MemoryHigh/MemoryMax)")
    if st["psi_cpu_some_max"] >= 40 and st["ollama_cpu_nucleos"] < 0.5 * cores:
        out.append(f"CONTENCIÓN DE CPU: PSI cpu some máx {st['psi_cpu_some_max']}% con Ollama usando solo "
                   f"{st['ollama_cpu_nucleos']} núcleos (ver top_cpu)")
    if not out:
        if st["ollama_cpu_nucleos"] >= 0.5:
            out.append(f"Sin presión de memoria ni de CPU: Ollama trabajó con {st['ollama_cpu_nucleos']} núcleos en promedio. "
                       "Si no respondía, el tiempo se fue en su propio cómputo (servidor)")
        else:
            out.append(f"Sin presión del sistema y Ollama casi sin CPU ({st['ollama_cpu_nucleos']} núcleos): si en esta "
                       "ventana no respondía, estaba esperando algo interno (cola, bloqueo del servidor)")
    if st["psi_io_full_s"] >= 0.05 * secs:
        out.append(f"E/S de disco trabada {st['psi_io_full_s']} s (PSI io): lecturas de disco o swap lentas")
    return out


def analyze(data: dict, calls: list[dict] | None = None) -> dict:
    samples, meta = data["muestras"], data["meta"]
    cores, clk = meta.get("nucleos") or os.cpu_count() or 1, meta.get("clk") or CLK
    whole = _window_stats(samples, cores, clk)
    report = {"meta": meta, "total": whole, "indicios": verdict(whole, cores), "llamadas": []}
    models = [s["api_ps"] for s in samples if s.get("api_ps", {}).get("ok")]
    if models:
        report["modelos_cargados_max"] = max(len(m["modelos"]) for m in models)
        report["modelos"] = sorted({m["name"] for x in models for m in x["modelos"]})
    for c in calls or []:
        if not c.get("ts") or not c.get("reloj_ms"):
            continue
        t0, t1 = c["ts"], c["ts"] + c["reloj_ms"] / 1000
        win = [s for s in samples if t0 - 1 <= s["t"] <= t1 + 1]
        st = _window_stats(win, cores, clk)
        report["llamadas"].append({"etapa": c.get("etapa"), "resultado": c.get("resultado"),
                                   "reloj_s": round(c["reloj_ms"] / 1000, 1),
                                   "sin_explicar_s": round((c.get("sin_explicar_ms") or 0) / 1000, 1),
                                   "medido": st, "indicios": verdict(st, cores)})
    return report


def render(report: dict) -> str:
    t = report["total"]
    lines = [f"Diagnóstico de Ollama CPU — pid {report['meta'].get('pid')}, cgroup {report['meta'].get('cgroup')}",
             f"Ventana: {t.get('segundos')} s, {t.get('muestras')} muestras"]
    if t.get("muestras", 0) >= 2:
        lines += [
            f"Memoria disponible mínima: {t['memoria_disponible_min_mib']} MiB · swap usado máx {t['swap_usado_max_mib']} MiB"
            f" · leído de swap {t['swap_leido_mib']} MiB · escrito {t['swap_escrito_mib']} MiB",
            f"Ollama: RSS máx {t['ollama_rss_max_mib']} MiB, en swap máx {t['ollama_swap_max_mib']} MiB, "
            f"fallos mayores {t['ollama_fallos_mayores']}, CPU {t['ollama_cpu_nucleos']} núcleos en promedio",
            f"PSI máx (avg10): memoria some {t['psi_memoria_some_max']}% / full {t['psi_memoria_full_max']}% · "
            f"cpu some {t['psi_cpu_some_max']}% · io some {t['psi_io_some_max']}%",
            f"cgroup: memory.events high +{t['cgroup_eventos_high']}, max +{t['cgroup_eventos_max']}, "
            f"oom_kill +{t['cgroup_oom']}"]
    if report.get("modelos"):
        lines.append(f"Modelos cargados en la instancia: {', '.join(report['modelos'])} "
                     f"(hasta {report['modelos_cargados_max']} a la vez)")
    lines.append("Indicios (toda la ventana):")
    lines += [f"  - {x}" for x in report["indicios"]]
    if report["llamadas"]:
        lines.append("Por llamada de Constructor (ventana de cada pedido):")
        for c in report["llamadas"]:
            m = c["medido"]
            lines.append(f"  · etapa {c['etapa']} {c['resultado']} {c['reloj_s']} s (sin explicar por Ollama "
                         f"{c['sin_explicar_s']} s): swap leído {m.get('swap_leido_mib')} MiB, PSI memoria full "
                         f"{m.get('psi_memoria_full_s')} s, Ollama CPU {m.get('ollama_cpu_nucleos')} núcleos")
            lines += [f"      {x}" for x in c["indicios"]]
    return "\n".join(lines)


def journal_hint(meta: dict) -> str:
    cg = meta.get("cgroup") or ""
    m = re.search(r"([^/]+\.service)$", cg)
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta.get("inicio", time.time())))
    if not m:
        return "No identifiqué la unidad de systemd de Ollama CPU; buscá sus registros con journalctl."
    user = "--user " if "/user@" in cg else ""
    return (f"Registros del servidor en la misma ventana (solo lectura):\n  journalctl {user}-u {m.group(1)} "
            f"--since '{since}' --no-pager | grep -Ei 'cache|slot|prompt|load|error|500'")
