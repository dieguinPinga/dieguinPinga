"""Ejecutor aislado: Bubblewrap + seccomp + cgroup (systemd scope) + arnés firmado.

El navegador nunca decide qué se ejecuta: el comando es siempre el mismo
(intérprete Python del sistema + arnés propio), construido como lista de
argumentos sin shell. Lo único variable es el contenido de solucion.py y la
especificación de pruebas, que viajan como datos.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from . import seccomp
from .limiter import LimiterError, SystemdScopeLimiter

log = logging.getLogger(__name__)
PAYLOAD_DIR = Path(__file__).resolve().parent
STDOUT_CAP = 4 * 1024 * 1024
STDERR_CAP = 64 * 1024


@dataclass
class ExecResult:
    status: str  # ok | oom | timeout | cancelled | error | blocked
    reason: str = ""
    result: dict | None = None
    stderr: str = ""
    limits: dict = field(default_factory=dict)
    cleanup_verified: bool = False
    duration_ms: int = 0
    unit: str = ""
    # Procesos matados por el límite de memoria según memory.events (None = no se pudo leer).
    oom_kills: int | None = None

    def summary(self) -> dict:
        return {"status": self.status, "reason": self.reason, "limits": self.limits,
                "cleanup_verified": self.cleanup_verified, "duration_ms": self.duration_ms, "unit": self.unit,
                "oom_kills": self.oom_kills}


class _Reader(threading.Thread):
    def __init__(self, fh, cap):
        super().__init__(daemon=True)
        self.fh, self.cap, self.buf, self.overflow = fh, cap, bytearray(), False
        self.start()

    def run(self):
        while True:
            try:
                chunk = self.fh.read1(65536)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            room = self.cap - len(self.buf)
            if room > 0:
                self.buf += chunk[:room]
            if len(chunk) > room:
                self.overflow = True


class Executor:
    def __init__(self, cfg: Config, limiter=None, allow_unverified_limits: bool = False):
        self.cfg = cfg
        self.limiter = limiter or SystemdScopeLimiter(cfg.sandbox_memory_mb, cfg.sandbox_tasks,
                                                      cfg.sandbox_cpu_percent)
        # Solo las pruebas automáticas pasan True (con NullLimiter); la
        # configuración no puede activarlo.
        self.allow_unverified_limits = allow_unverified_limits
        self.bwrap = shutil.which("bwrap") or "/usr/bin/bwrap"
        self.enabled = False
        self.disabled_reason = "Autodiagnóstico del aislamiento pendiente"
        self.selftest_report: dict | None = None
        self._lock = threading.Lock()

    # Construcción del comando ----------------------------------------------
    def python_path(self) -> str:
        real = os.path.realpath(self.cfg.sandbox_python)
        if not real.startswith("/usr/") or not os.access(real, os.X_OK):
            raise RuntimeError(f"Intérprete del sandbox inválido: {real} (debe estar bajo /usr)")
        return real

    def bwrap_command(self, run_dir: Path, seccomp_fd: int | None) -> list[str]:
        args = [self.bwrap, "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent",
                "--new-session", "--cap-drop", "ALL", "--hostname", "cia-sandbox", "--clearenv",
                "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", "/tmp", "--setenv", "LANG", "C.UTF-8",
                "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                "--ro-bind", "/usr", "/usr"]
        for name in ("bin", "sbin", "lib", "lib64", "lib32", "libx32"):
            p = Path("/") / name
            if p.is_symlink():
                target = os.readlink(p)
                if target.lstrip("/").startswith("usr/"):
                    args += ["--symlink", target, f"/{name}"]
            elif p.is_dir() and name in ("bin", "lib", "lib64"):
                args += ["--ro-bind", str(p), f"/{name}"]
        args += ["--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
                 "--proc", "/proc", "--dev", "/dev",
                 "--size", str(self.cfg.sandbox_tmpfs_mb * 1024 * 1024), "--tmpfs", "/tmp",
                 "--ro-bind", str(run_dir / "code"), "/work/code",
                 "--ro-bind", str(run_dir / "harness"), "/work/harness",
                 "--remount-ro", "/", "--chdir", "/tmp"]
        if seccomp_fd is not None:
            args += ["--seccomp", str(seccomp_fd)]
        args += [self.python_path(), "-I", "-S", "/work/harness/harness.py"]
        return args

    def _prepare_dir(self, code: str | None, files: dict[str, str] | None = None) -> Path:
        runs = self.cfg.runs_dir
        runs.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_dir = Path(os.path.realpath(runs)) / f"run-{secrets.token_hex(8)}"
        run_dir.mkdir(mode=0o700)
        (run_dir / "code").mkdir(mode=0o755)
        (run_dir / "harness").mkdir(mode=0o755)
        for name in ("harness.py", "child_runner.py"):
            shutil.copyfile(PAYLOAD_DIR / name, run_dir / "harness" / name)
            os.chmod(run_dir / "harness" / name, 0o444)
        if files is None:
            files = {"solucion.py": code or ""}
        from ..webproject import valid_path

        for rel, content in files.items():
            if rel != "solucion.py" and not valid_path(rel):
                raise RuntimeError(f"ruta de proyecto inválida: {rel[:80]}")
            target = run_dir / "code" / rel
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
        os.chmod(run_dir / "code", 0o555)
        os.chmod(run_dir / "harness", 0o555)
        return run_dir

    @staticmethod
    def _remove_dir(run_dir: Path) -> None:
        for sub in ("code", "harness"):
            try:
                os.chmod(run_dir / sub, 0o700)
            except OSError:
                pass
        for dirpath, dirnames, _ in os.walk(run_dir):
            for d in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, d), 0o700)
                except OSError:
                    pass
        shutil.rmtree(run_dir, ignore_errors=True)

    # Ejecución ------------------------------------------------------------
    def run(self, code: str, spec: dict, tests_py: str | None, cancel: threading.Event | None = None,
            _probe: dict | None = None, _skip_enabled_check: bool = False,
            files: dict[str, str] | None = None) -> ExecResult:
        if not _skip_enabled_check and not self.enabled:
            return ExecResult("blocked", reason=f"Ejecución bloqueada: {self.disabled_reason}")
        with self._lock:  # una ejecución a la vez
            return self._run(code, spec, tests_py, cancel or threading.Event(), _probe, files)

    def _run(self, code, spec, tests_py, cancel, probe_opts, files=None) -> ExecResult:
        t0 = time.monotonic()
        unit = f"cia-job-{secrets.token_hex(8)}"
        run_dir = self._prepare_dir(code, files)
        nonce = secrets.token_bytes(32)
        payload = {"nonce": nonce.hex(), "case_timeout": self.cfg.sandbox_case_timeout_s}
        if probe_opts is not None:
            payload.update(mode="probe", probe=probe_opts)
        else:
            payload.update(mode="test", spec=spec, tests_py=tests_py or "")
            if spec.get("mode") == "web":
                # Node del sistema (bajo /usr, visible en el sandbox en solo lectura) para node --check.
                payload["node"] = self.cfg.web_node if os.path.exists(self.cfg.web_node) else ""
        data = (json.dumps(payload) + "\n").encode()
        bpf_fd = None
        res = ExecResult("error", unit=unit)
        proc = None
        try:
            if self.cfg.sandbox_seccomp:
                bpf_fd = os.memfd_create("cia-seccomp", 0)
                os.write(bpf_fd, seccomp.build_filter())
                os.lseek(bpf_fd, 0, os.SEEK_SET)
            cmd = self.limiter.wrap(self.bwrap_command(run_dir, bpf_fd), unit)
            env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
            for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
                if k in os.environ:
                    env[k] = os.environ[k]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    pass_fds=(bpf_fd,) if bpf_fd is not None else (), start_new_session=True,
                                    cwd=run_dir, env=env)
            if bpf_fd is not None:
                os.close(bpf_fd)
                bpf_fd = None
            out, err = _Reader(proc.stdout, STDOUT_CAP), _Reader(proc.stderr, STDERR_CAP)
            # 1) Verificar límites ANTES de que el arnés reciba trabajo.
            try:
                res.limits = self.limiter.verify(proc.pid, unit, alive=lambda: proc.poll() is None)
            except LimiterError as e:
                self._kill(proc, unit)
                err.join(1)
                detail = err.buf.decode("utf-8", "replace").strip()[-300:]
                res.status, res.reason = "blocked", str(e) + (f" — {detail}" if detail else "")
                return res
            if not res.limits.get("verified") and not self.allow_unverified_limits:
                self._kill(proc, unit)
                res.status, res.reason = "blocked", "Límites de recursos no verificados"
                return res
            # 2) Entregar el trabajo por stdin (los casos no existen en disco).
            writer = threading.Thread(target=self._feed, args=(proc, data), daemon=True)
            writer.start()
            deadline = t0 + self.cfg.sandbox_timeout_s
            cg = res.limits.get("cgroup")
            stdin_open = True
            while True:
                try:
                    proc.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    pass
                res.oom_kills = self._oom_kills(cg, res.oom_kills)
                if stdin_open and not writer.is_alive() and (out.overflow or self._result_ready(out)):
                    # El arnés espera el cierre de stdin: el cgroup sigue vivo para una lectura final.
                    res.oom_kills = self._oom_kills(cg, res.oom_kills)
                    self._close_stdin(proc)
                    stdin_open = False
                if cancel.is_set():
                    self._kill(proc, unit)
                    res.status, res.reason = "cancelled", "Cancelada por el operador"
                    break
                if time.monotonic() > deadline:
                    self._kill(proc, unit)
                    res.status, res.reason = "timeout", f"Tiempo máximo de ejecución agotado ({self.cfg.sandbox_timeout_s} s)"
                    break
            # Última lectura: el cgroup puede seguir existiendo un instante tras la salida.
            res.oom_kills = self._oom_kills(cg, res.oom_kills)
            out.join(2)
            err.join(2)
            res.stderr = err.buf.decode("utf-8", "replace")[-4000:]
            if res.status in ("cancelled", "timeout"):
                return res
            parsed = self._parse(out.buf.decode("utf-8", "replace"), nonce)
            if parsed is None:
                if res.oom_kills:
                    # Causa verificada por el kernel, no un fallo del supervisor. Sigue sin ser éxito.
                    res.status = "oom"
                    res.reason = (f"Se superó el límite de memoria del sandbox ({self.cfg.sandbox_memory_mb} MiB): "
                                  f"memory.events oom_kill={res.oom_kills}; el arnés no pudo informar resultados "
                                  f"(código {proc.returncode}).")
                else:
                    res.status = "error"
                    res.reason = (f"El arnés no produjo un resultado válido (código {proc.returncode}; "
                                  f"memory.events oom_kill={res.oom_kills if res.oom_kills is not None else 'no disponible'})."
                                  + (" Salida truncada." if out.overflow else ""))
                return res
            parsed["oom_kills"] = res.oom_kills
            if res.oom_kills:
                for c in parsed.get("cases", []):
                    if "SIGKILL" in str(c.get("detail", "")):
                        c["detail"] += f" — confirmado: límite de memoria ({self.cfg.sandbox_memory_mb} MiB, memory.events)"
            res.status, res.result = "ok", parsed
            return res
        except (OSError, RuntimeError, LimiterError) as e:
            res.status, res.reason = "error", f"Fallo del ejecutor: {e}"
            if proc is not None:
                self._kill(proc, unit)
            return res
        finally:
            if bpf_fd is not None:
                os.close(bpf_fd)
            if proc is not None:
                if proc.poll() is None:
                    self._kill(proc, unit)
                for fh in (proc.stdin, proc.stdout, proc.stderr):
                    try:
                        fh.close()
                    except (OSError, ValueError):
                        pass
            res.cleanup_verified = self._verify_cleanup(res.limits.get("cgroup"), unit)
            res.duration_ms = int((time.monotonic() - t0) * 1000)
            self._remove_dir(run_dir)

    @staticmethod
    def _feed(proc, data: bytes) -> None:
        # stdin queda abierto: el arnés no termina hasta que el anfitrión lea memory.events.
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except OSError:
            pass

    @staticmethod
    def _close_stdin(proc) -> None:
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    @staticmethod
    def _result_ready(out: "_Reader") -> bool:
        buf = bytes(out.buf)
        return b"CIA-RESULT " in buf and buf.endswith(b"\n")

    def _oom_kills(self, cg: str | None, previous: int | None) -> int | None:
        events = self.limiter.memory_events(cg)
        if events is None:
            return previous
        return max(previous or 0, events.get("oom_kill", 0))

    def _kill(self, proc, unit: str) -> None:
        self.limiter.kill(unit)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.kill()
            proc.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _verify_cleanup(self, cgroup: str | None, unit: str) -> bool:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.limiter.is_empty(cgroup):
                return True
            time.sleep(0.1)
        self.limiter.kill(unit)
        time.sleep(0.5)
        ok = self.limiter.is_empty(cgroup)
        if not ok:
            log.error("Quedaron procesos en %s tras la limpieza", unit)
        return ok

    @staticmethod
    def _parse(stdout: str, nonce: bytes) -> dict | None:
        for line in reversed(stdout.splitlines()):
            if line.startswith("CIA-RESULT "):
                body, _, mac = line[len("CIA-RESULT "):].rpartition(" ")
                want = hmac.new(nonce, body.encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(want, mac):
                    return None
                try:
                    return json.loads(body)
                except ValueError:
                    return None
        return None

    # Autodiagnóstico ------------------------------------------------------
    def self_test(self, stress: list[str] | None = None) -> dict:
        checks: list[dict] = []

        def check(name, ok, detail=""):
            checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})

        report = {"ok": False, "checks": checks, "ts": time.time(), "limiter": self.limiter.name}
        if not self.cfg.sandbox_enabled:
            check("sandbox.enabled", False, "Ejecución desactivada en la configuración")
            return self._finish(report)
        check("bwrap", os.access(self.bwrap, os.X_OK), self.bwrap)
        try:
            check("python", True, self.python_path())
        except RuntimeError as e:
            check("python", False, e)
            return self._finish(report)
        if self.cfg.sandbox_seccomp and not seccomp.supported():
            check("seccomp", False, "Arquitectura no soportada; desactive seccomp explícitamente si lo acepta")
            return self._finish(report)
        res = self.run("", {}, None, _probe={"stress": stress or []}, _skip_enabled_check=True)
        report["exec"] = res.summary()
        if res.status != "ok" or not res.result:
            check("probe", False, f"{res.status}: {res.reason} {res.stderr[-300:]}")
            return self._finish(report)
        p = res.result["probe"]
        report["probe"] = p
        check("limites_cgroup", res.limits.get("verified") or self.allow_unverified_limits,
              {k: v for k, v in res.limits.items() if k != "cgroup"})
        check("rutas_sensibles_invisibles", not p["visible_sensitive"], p["visible_sensitive"] or "ninguna visible")
        check("red_bloqueada", all(n["blocked"] for n in p["network"]) and p["abstract_unix_blocked"],
              [n["dest"] for n in p["network"] if not n["blocked"]] or "sin conectividad")
        writable = [k for k, v in p["writable"].items() if v]
        check("escritura_solo_tmp", writable == ["/tmp/cia_probe"], writable)
        check("pid_namespace", p["pids_visible"] <= 6, f"{p['pids_visible']} procesos visibles")
        st = p["status"]
        check("no_new_privs", st.get("NoNewPrivs") == "1", st.get("NoNewPrivs"))
        check("sin_capacidades", set(st.get("CapEff", "1")) <= {"0"}, st.get("CapEff"))
        if self.cfg.sandbox_seccomp:
            check("seccomp_activo", st.get("Seccomp") == "2" and p["ptrace_blocked"],
                  f"Seccomp={st.get('Seccomp')} ptrace_bloqueado={p['ptrace_blocked']}")
        check("userns_bloqueado", p["userns_blocked"], p["userns_blocked"])
        check("arnes_no_volcable", res.result.get("harness_nondumpable"), "")
        check("entorno_limpio", set(p["env_keys"]) <= {"PATH", "HOME", "LANG", "PWD", "PYTHONDONTWRITEBYTECODE"},
              p["env_keys"])
        check("limpieza_procesos", res.cleanup_verified, res.cleanup_verified)
        if "mem" in (stress or []):
            check("limite_memoria", *mem_stress_verdict(p.get("stress_mem"), res.oom_kills))
        if "fork" in (stress or []):
            fork = p.get("stress_fork") or {}
            check("limite_tareas", str(fork.get("stdout", "")).startswith("limite"),
                  str(fork.get("stdout", "")).strip()[:120] or "sin salida")
        return self._finish(report)

    def _finish(self, report: dict) -> dict:
        failed = [c["name"] for c in report["checks"] if not c["ok"]]
        report["ok"] = not failed
        self.selftest_report = report
        self.enabled = report["ok"]
        self.disabled_reason = "" if report["ok"] else "falló el autodiagnóstico del aislamiento: " + ", ".join(failed)
        return report


def mem_stress_verdict(stress_mem: dict | None, oom_kills: int | None) -> tuple[bool, str]:
    """La prueba de memoria solo pasa si el kernel confirma el OOM del proceso que excedió el límite.

    Un código -9 sin oom_kill registrado (o un -15, o "sobrevivio") NO prueba que el límite funcione.
    """
    if not stress_mem:
        return False, "sin resultado de la prueba de memoria"
    code, out = stress_mem.get("code"), str(stress_mem.get("stdout", ""))
    detail = f"código={code} memory.events oom_kill={oom_kills if oom_kills is not None else 'no disponible'}"
    if "sobrevivio" in out:
        return False, "el proceso superó el límite sin ser terminado; " + detail
    if code != -9:
        return False, "el proceso no terminó por SIGKILL; " + detail
    if not oom_kills:
        return False, "SIGKILL sin oom_kill registrado en memory.events; causa no verificada; " + detail
    return True, detail
