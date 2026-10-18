"""Límites de recursos por cgroup v2 para cada ejecución aislada.

`SystemdScopeLimiter` envuelve el comando con `systemd-run --user --scope`,
de modo que todo el árbol de procesos (incluidos los que cambien de sesión o
grupo) queda dentro de una unidad `cia-job-*.scope` con MemoryMax, TasksMax y
CPUQuota. Los límites se VERIFICAN leyendo el cgroup real antes de entregar
el trabajo al arnés; si no coinciden, la ejecución se bloquea.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)
UNIT_PREFIX = "cia-job-"
UNIT_RE = re.compile(r"^cia-job-[a-z0-9]{8,32}$")
CGROUP_ROOT = Path("/sys/fs/cgroup")


class LimiterError(Exception):
    pass


class SystemdScopeLimiter:
    name = "systemd-scope"

    def __init__(self, memory_mb: int, tasks: int, cpu_percent: int):
        self.memory_bytes = memory_mb * 1024 * 1024
        self.tasks = tasks
        self.cpu_percent = cpu_percent
        self.systemd_run = shutil.which("systemd-run") or "/usr/bin/systemd-run"
        self.systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"

    def wrap(self, cmd: list[str], unit: str) -> list[str]:
        if not UNIT_RE.match(unit):
            raise LimiterError("Nombre de unidad inválido")
        return [self.systemd_run, "--user", "--scope", "--quiet", "--collect", f"--unit={unit}",
                "-p", f"MemoryMax={self.memory_bytes}", "-p", "MemorySwapMax=0",
                "-p", f"TasksMax={self.tasks}", "-p", f"CPUQuota={self.cpu_percent}%",
                # Con la política por defecto (stop), un OOM del código evaluado hace que
                # systemd detenga TODA la unidad (SIGTERM al arnés) y se pierde el resultado
                # firmado. "continue" deja que el kernel mate solo al proceso que excedió
                # memory.max; el límite sigue igual y la limpieza final no cambia.
                "-p", "OOMPolicy=continue",
                "--", *cmd]

    def cgroup_of(self, pid: int) -> Path | None:
        try:
            text = Path(f"/proc/{pid}/cgroup").read_text()
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith("0::"):
                return CGROUP_ROOT / line[3:].lstrip("/")
        return None

    def verify(self, pid: int, unit: str, wait_s: float = 5.0, alive=None) -> dict:
        """Espera a que el proceso esté en la unidad y comprueba los límites."""
        deadline = time.monotonic() + wait_s
        cg = None
        while time.monotonic() < deadline:
            cg = self.cgroup_of(pid)
            if cg is not None and cg.name == f"{unit}.scope":
                break
            if (alive is not None and not alive()) or not os.path.exists(f"/proc/{pid}"):
                raise LimiterError("systemd-run terminó antes de crear la unidad (¿gestor systemd de usuario disponible?)")
            time.sleep(0.02)
        else:
            raise LimiterError(f"El proceso no quedó dentro de la unidad {unit}.scope (cgroup: {cg})")

        def read(name):
            try:
                return (cg / name).read_text().strip()
            except OSError as e:
                raise LimiterError(f"No se pudo leer {name} del cgroup: {e}") from None

        mem, pids, cpu = read("memory.max"), read("pids.max"), read("cpu.max")
        swap = None
        try:
            swap = (cg / "memory.swap.max").read_text().strip()
        except OSError:
            pass
        quota, _, period = cpu.partition(" ")
        problems = []
        if mem == "max" or int(mem) > self.memory_bytes:
            problems.append(f"memory.max={mem}")
        if pids == "max" or int(pids) > self.tasks:
            problems.append(f"pids.max={pids}")
        if quota == "max" or int(quota) * 100 > self.cpu_percent * int(period or 100000) * 1.01:
            problems.append(f"cpu.max={cpu}")
        info = {"cgroup": str(cg), "memory.max": mem, "pids.max": pids, "cpu.max": cpu,
                "memory.swap.max": swap, "verified": not problems}
        if problems:
            raise LimiterError("Límites de cgroup no aplicados: " + ", ".join(problems))
        return info

    def memory_events(self, cg: str | None) -> dict | None:
        """Contadores de memory.events del cgroup (oom, oom_kill, max…); None si no se pueden leer."""
        if not cg:
            return None
        try:
            text = Path(cg, "memory.events").read_text()
        except OSError:
            return None
        events = {}
        for line in text.splitlines():
            key, _, value = line.partition(" ")
            if value.strip().isdigit():
                events[key] = int(value)
        return events

    def kill(self, unit: str) -> None:
        if not UNIT_RE.match(unit):
            return
        for args in (["kill", "--signal=SIGKILL", f"{unit}.scope"], ["stop", f"{unit}.scope"]):
            try:
                subprocess.run([self.systemctl, "--user", *args], timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def is_empty(self, cg: str | None) -> bool:
        if not cg:
            return True
        try:
            return not Path(cg, "cgroup.procs").read_text().strip()
        except OSError:
            return True  # el cgroup ya no existe

    def cleanup_stale(self) -> list[str]:
        """Detiene unidades cia-job-* que hayan quedado de una ejecución previa."""
        try:
            out = subprocess.run([self.systemctl, "--user", "list-units", "--plain", "--no-legend", "--all",
                                  f"{UNIT_PREFIX}*.scope"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        stopped = []
        for line in out.splitlines():
            name = line.split()[0] if line.split() else ""
            if name.endswith(".scope") and UNIT_RE.match(name[:-6]):
                self.kill(name[:-6])
                stopped.append(name)
        return stopped


class NullLimiter:
    """SOLO PARA PRUEBAS AUTOMÁTICAS: no aplica límites. No es seleccionable
    desde la configuración; el servidor real nunca lo usa."""

    name = "null-test-only"

    def wrap(self, cmd, unit):
        return list(cmd)

    def verify(self, pid, unit, wait_s=5.0, alive=None):
        return {"verified": False, "note": "sin límites (modo de prueba)"}

    def memory_events(self, cg):
        return None

    def kill(self, unit):
        pass

    def is_empty(self, cg):
        return True

    def cleanup_stale(self):
        return []
