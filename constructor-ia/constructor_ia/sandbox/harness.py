"""Arnés de pruebas CONFIABLE que corre dentro del sandbox.

Protocolo:
  * Lee por stdin una línea JSON con {nonce, mode, spec, tests_py, ...}.
    Los casos (incluidos los ocultos) solo existen en la memoria de este
    proceso: no hay archivo de pruebas visible para el código generado.
  * Se marca como no volcable (PR_SET_DUMPABLE=0) para que el proceso hijo,
    aunque comparta UID, no pueda leer su memoria ni sus descriptores vía /proc.
  * Ejecuta la solución en procesos hijos con límites rlimit propios.
  * Escribe una única línea final "CIA-RESULT <json> <hmac>" firmada con el
    nonce recibido, que el anfitrión verifica.

Solo usa la biblioteca estándar; se ejecuta con `python3 -I -S`.
"""
import ctypes
import hashlib
import hmac
import json
import math
import os
import resource
import select
import signal
import socket
import subprocess
import sys
import threading
import time

PY = sys.executable
CHILD = "/work/harness/child_runner.py"
CODE = "/work/code/solucion.py"
OUT_CAP = 64 * 1024
ERR_CAP = 16 * 1024


def set_nondumpable() -> bool:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE = 4
    except Exception:
        return False


def child_limits(cpu_s: int):
    def apply():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))
    return apply


class Drain(threading.Thread):
    """Lee un descriptor hasta EOF guardando como máximo `cap` bytes."""

    def __init__(self, fh, cap):
        super().__init__(daemon=True)
        self.fh, self.cap, self.data, self.overflow = fh, cap, bytearray(), False
        self.start()

    def run(self):
        while True:
            try:
                chunk = self.fh.read1(65536) if hasattr(self.fh, "read1") else self.fh.read(65536)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            room = self.cap - len(self.data)
            if room > 0:
                self.data += chunk[:room]
            if len(chunk) > room:
                self.overflow = True

    def text(self):
        t = self.data.decode("utf-8", "replace")
        return t + ("\n[... salida truncada]" if self.overflow else "")


def run_bounded(cmd, stdin_bytes, timeout, cpu_s):
    """Ejecuta un proceso con entrada fija, tiempo máximo y salida acotada."""
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd="/tmp", preexec_fn=child_limits(cpu_s), start_new_session=True)
    out, err = Drain(p.stdout, OUT_CAP), Drain(p.stderr, ERR_CAP)
    try:
        p.stdin.write(stdin_bytes)
        p.stdin.close()
    except OSError:
        pass
    timed_out = False
    try:
        p.wait(timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_group(p)
        p.wait()
    out.join(1)
    err.join(1)
    return {"code": p.returncode, "timeout": timed_out, "stdout": out.text(), "stderr": err.text(),
            "overflow": out.overflow, "ms": int((time.monotonic() - t0) * 1000)}


def kill_group(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        p.kill()
    except OSError:
        pass


class Child:
    """Proceso hijo que importa solucion.py y responde llamadas JSON."""

    def __init__(self, timeout, cpu_s):
        self.timeout = timeout
        self.p = subprocess.Popen([PY, "-I", "-S", CHILD, CODE], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/tmp",
                                  preexec_fn=child_limits(cpu_s), start_new_session=True)
        self.err = Drain(self.p.stderr, ERR_CAP)
        self.buf = b""
        self.next_id = 0
        self.ready = None

    def read_line(self, timeout):
        deadline = time.monotonic() + timeout
        fd = self.p.stdout.fileno()
        while b"\n" not in self.buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", None
            r, _, _ = select.select([fd], [], [], remaining)
            if not r:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                return "eof", None
            self.buf += chunk
            if len(self.buf) > 2_000_000:
                return "overflow", None
        line, self.buf = self.buf.split(b"\n", 1)
        try:
            return "ok", json.loads(line)
        except ValueError:
            return "garbage", None

    def start(self):
        status, msg = self.read_line(self.timeout * 2)
        if status != "ok":
            self.ready = {"ready": False, "error": self.describe_death(status), "exc": status}
        else:
            self.ready = msg
        return self.ready

    def call(self, fn, args, kwargs):
        self.next_id += 1
        rid = self.next_id
        req = json.dumps({"id": rid, "fn": fn, "args": args, "kwargs": kwargs}) + "\n"
        t0 = time.monotonic()
        try:
            self.p.stdin.write(req.encode())
            self.p.stdin.flush()
        except OSError:
            return "dead", None, 0
        status, msg = self.read_line(self.timeout)
        ms = int((time.monotonic() - t0) * 1000)
        if status == "ok" and (not isinstance(msg, dict) or msg.get("id") != rid):
            status = "garbage"
        return status, msg, ms

    def describe_death(self, status):
        if status == "timeout":
            return f"Tiempo agotado ({self.timeout} s)"
        if status in ("eof", "dead"):
            try:
                code = self.p.wait(1)
            except subprocess.TimeoutExpired:
                code = None
            if code is not None and code < 0:
                sig = -code
                name = {9: "SIGKILL (posible límite de memoria o tiempo de CPU)", 24: "SIGXCPU (tiempo de CPU)",
                        25: "SIGXFSZ (tamaño de archivo)", 11: "SIGSEGV"}.get(sig, f"señal {sig}")
                return f"El proceso de la solución terminó por {name}"
            return f"El proceso de la solución terminó inesperadamente (código {code})"
        return "Respuesta inválida del proceso de la solución"

    def stop(self):
        kill_group(self.p)
        try:
            self.p.wait(2)
        except subprocess.TimeoutExpired:
            pass
        self.err.join(1)


def short(value, limit=400):
    try:
        s = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        s = repr(value)
    return s if len(s) <= limit else s[:limit] + "…"


def equal(a, b, tol):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b if isinstance(a, bool) and isinstance(b, bool) else False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, float) or isinstance(b, float):
            if math.isnan(a) and math.isnan(b):
                return True
            return math.isclose(a, b, rel_tol=tol, abs_tol=tol)
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equal(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equal(a[k], b[k], tol) for k in a)
    return a == b


def run_function_cases(spec, timeout, cpu_s):
    results = []
    fn = spec["function"]
    tol = float(spec.get("float_tol", 1e-9))
    child, load_error = None, None
    restarts = 0
    for i, case in enumerate(spec.get("cases", [])):
        entry = {"n": i + 1, "kind": "caso", "visible": bool(case.get("visible", True)),
                 "args": case.get("args", []), "kwargs": case.get("kwargs", {})}
        if child is None:
            if restarts > 8:
                entry.update(ok=False, status="error", detail="Demasiados reinicios del proceso de la solución")
                results.append(entry)
                continue
            child = Child(timeout, cpu_s)
            ready = child.start()
            restarts += 1
            if not ready.get("ready"):
                load_error = ready.get("error", "Error al cargar la solución")
                child.stop()
                child = None
                entry.update(ok=False, status="error", detail="No se pudo cargar solucion.py:\n" + load_error)
                results.append(entry)
                continue
        status, msg, ms = child.call(fn, entry["args"], entry["kwargs"])
        entry["ms"] = ms
        if status != "ok":
            entry.update(ok=False, status="timeout" if status == "timeout" else "error",
                         detail=child.describe_death(status))
            stderr = child.err.text()
            child.stop()
            if stderr:
                entry["stderr"] = stderr[-2000:]
            child = None
        elif "raises" in case and case["raises"]:
            want = case["raises"]
            if msg.get("ok"):
                entry.update(ok=False, status="fallo", got=short(msg.get("value")), expected=f"excepción {want}",
                             detail=f"Se esperaba la excepción {want} y se devolvió un valor")
            elif msg.get("exc") == want:
                entry.update(ok=True, status="ok", got=f"excepción {want}")
            else:
                entry.update(ok=False, status="fallo", got=f"excepción {msg.get('exc')}", expected=f"excepción {want}",
                             detail=f"Se esperaba {want} y se lanzó {msg.get('exc')}: {msg.get('msg', '')}",
                             tb=msg.get("tb"))
        elif not msg.get("ok"):
            entry.update(ok=False, status="error", got=f"excepción {msg.get('exc')}",
                         expected=short(case.get("expect")),
                         detail=f"{msg.get('exc')}: {msg.get('msg', '')}", tb=msg.get("tb"))
        else:
            ok = equal(msg.get("value"), case.get("expect"), tol)
            entry.update(ok=ok, status="ok" if ok else "fallo", got=short(msg.get("value")),
                         expected=short(case.get("expect")))
            if not ok:
                entry["detail"] = "Resultado distinto del esperado"
        results.append(entry)
    if child is not None:
        stderr = child.err.text()
        child.stop()
        if stderr and results:
            results[-1].setdefault("stderr", stderr[-2000:])
    return results, load_error


def normalize_output(text):
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def run_script_cases(spec, timeout, cpu_s):
    results = []
    for i, case in enumerate(spec.get("cases", [])):
        entry = {"n": i + 1, "kind": "script", "visible": bool(case.get("visible", True)),
                 "stdin": case.get("stdin", "")}
        r = run_bounded([PY, "-I", "-S", CODE], case.get("stdin", "").encode(), timeout, cpu_s)
        entry["ms"] = r["ms"]
        got, want = normalize_output(r["stdout"]), normalize_output(case.get("stdout", ""))
        entry.update(got=got[:2000], expected=want[:2000])
        if r["timeout"]:
            entry.update(ok=False, status="timeout", detail=f"Tiempo agotado ({timeout} s)")
        elif r["code"] != 0:
            entry.update(ok=False, status="error", detail=f"El programa terminó con código {r['code']}",
                         stderr=r["stderr"][-2000:])
        elif r["overflow"]:
            entry.update(ok=False, status="fallo", detail="Salida demasiado grande")
        else:
            ok = got == want
            entry.update(ok=ok, status="ok" if ok else "fallo")
            if not ok:
                entry["detail"] = "La salida no coincide con la esperada"
        results.append(entry)
    return results


class RemoteError(Exception):
    pass


def run_operator_tests(tests_py, timeout, cpu_s):
    """Pruebas Python escritas por el operador (confiables, ejecutadas en el arnés)."""
    results = []
    ns = {"__name__": "pruebas_operador"}
    try:
        exec(compile(tests_py, "pruebas_operador.py", "exec"), ns)
    except Exception as e:  # noqa: BLE001
        return [{"n": 1, "kind": "prueba", "visible": False, "ok": False, "status": "error",
                 "detail": f"Las pruebas del operador no compilan: {type(e).__name__}: {e}"}]
    names = sorted(k for k, v in ns.items() if k.startswith("test_") and callable(v))
    import builtins

    for i, name in enumerate(names):
        child = Child(timeout, cpu_s)
        ready = child.start()
        entry = {"n": i + 1, "kind": "prueba", "name": name, "visible": False}
        if not ready.get("ready"):
            child.stop()
            entry.update(ok=False, status="error", detail="No se pudo cargar solucion.py:\n" + ready.get("error", ""))
            results.append(entry)
            continue

        class Proxy:
            def __getattr__(self, fn):
                def call(*args, **kwargs):
                    status, msg, _ = child.call(fn, list(args), kwargs)
                    if status != "ok":
                        raise RemoteError(child.describe_death(status))
                    if msg.get("ok"):
                        return msg.get("value")
                    exc = getattr(builtins, str(msg.get("exc")), None)
                    if isinstance(exc, type) and issubclass(exc, Exception):
                        raise exc(msg.get("msg", ""))
                    raise RemoteError(f"{msg.get('exc')}: {msg.get('msg', '')}")
                return call

        t0 = time.monotonic()
        try:
            ns[name](Proxy())
            entry.update(ok=True, status="ok")
        except AssertionError as e:
            entry.update(ok=False, status="fallo", detail=f"Aserción fallida: {e}"[:800])
        except RemoteError as e:
            entry.update(ok=False, status="error", detail=str(e)[:800])
        except Exception as e:  # noqa: BLE001
            entry.update(ok=False, status="error", detail=f"{type(e).__name__}: {e}"[:800])
        entry["ms"] = int((time.monotonic() - t0) * 1000)
        child.stop()
        results.append(entry)
    return results


# --- Sondas de aislamiento (autodiagnóstico) -------------------------------

def probe(opts, timeout, cpu_s):
    r = {}
    r["uid"] = os.getuid()
    r["hostname"] = socket.gethostname()
    r["root_listing"] = sorted(os.listdir("/"))
    sensitive = ["/home", "/root", "/etc/passwd", "/etc/shadow", "/etc/ssh", "/run", "/var", "/sys", "/media",
                 "/mnt", "/srv", "/opt", "/snap", "/boot", "/home/diego", "/home/diego/vigilancia",
                 "/home/diego/.ollama", "/home/diego/.openclaw", "/run/user", "/var/run/docker.sock",
                 "/var/lib/mysql", "/run/mysqld", "/work/tests", "/dev/sda", "/dev/nvme0n1", "/dev/mem"]
    r["visible_sensitive"] = [p for p in sensitive if os.path.lexists(p)]
    blocked = []
    for host, port in [("1.1.1.1", 53), ("127.0.0.1", 11434), ("127.0.0.1", 11435), ("127.0.0.1", 18789),
                       ("127.0.0.1", 1880), ("127.0.0.1", 3306), ("100.100.100.100", 53)]:
        try:
            s = socket.create_connection((host, port), timeout=1)
            s.close()
            blocked.append({"dest": f"{host}:{port}", "blocked": False})
        except OSError as e:
            blocked.append({"dest": f"{host}:{port}", "blocked": True, "err": e.__class__.__name__})
    r["network"] = blocked
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect("\0/tmp/.X11-unix/X0")
        r["abstract_unix_blocked"] = False
    except OSError:
        r["abstract_unix_blocked"] = True
    writes = {}
    for p in ["/usr/cia_probe", "/work/code/cia_probe", "/work/harness/cia_probe", "/cia_probe", "/tmp/cia_probe"]:
        try:
            with open(p, "w") as fh:
                fh.write("x")
            writes[p] = True
        except OSError:
            writes[p] = False
    r["writable"] = writes
    r["pids_visible"] = len([d for d in os.listdir("/proc") if d.isdigit()])
    status = {}
    with open("/proc/self/status") as fh:
        for line in fh:
            k, _, v = line.partition(":")
            if k in ("NoNewPrivs", "Seccomp", "CapEff", "CapPrm", "CapBnd"):
                status[k] = v.strip()
    r["status"] = status
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        rc = libc.ptrace(0, 0, None, None)
        r["ptrace_blocked"] = rc != 0
        r["ptrace_errno"] = ctypes.get_errno()
    except Exception as e:  # noqa: BLE001
        r["ptrace_blocked"] = None
        r["ptrace_errno"] = str(e)
    try:
        os.unshare(os.CLONE_NEWUSER)
        r["userns_blocked"] = False
    except (OSError, AttributeError):
        r["userns_blocked"] = True
    r["dev"] = sorted(os.listdir("/dev"))
    r["env_keys"] = sorted(os.environ)
    stress = opts.get("stress") or []
    if "mem" in stress:
        code = "x = bytearray(%d)\nfor i in range(0, len(x), 4096): x[i] = 1\nprint('sobrevivio')" % (
            int(opts.get("mem_bytes", 600 * 1024 * 1024)))
        r["stress_mem"] = run_bounded([PY, "-I", "-S", "-c", code], b"", timeout, 60)
    if "fork" in stress:
        code = ("import os,time\nn=0\ntry:\n    for _ in range(400):\n        if os.fork()==0:\n"
                "            time.sleep(30); os._exit(0)\n        n+=1\nexcept OSError as e:\n"
                "    print('limite', n, e.errno)\nelse:\n    print('sin_limite', n)\n")
        r["stress_fork"] = run_bounded([PY, "-I", "-S", "-c", code], b"", timeout, 60)
    if "orphan" in stress:
        # Deja un nieto en otra sesión durmiendo: el anfitrión debe limpiarlo.
        subprocess.Popen([PY, "-I", "-S", "-c", "import time; time.sleep(600)"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r["stress_orphan"] = "lanzado"
    if "spin" in stress:
        while True:
            pass
    return r


def main():
    line = sys.stdin.buffer.readline(4_000_000)
    req = json.loads(line)
    sys.stdin.close()
    nonce = bytes.fromhex(req["nonce"])
    nondump = set_nondumpable()
    timeout = int(req.get("case_timeout", 5))
    cpu_s = timeout + 1
    t0 = time.monotonic()
    result = {"harness_nondumpable": nondump}
    mode = req.get("mode")
    if mode == "probe":
        result["probe"] = probe(req.get("probe", {}), timeout, cpu_s)
    else:
        spec = req["spec"]
        cases, load_error = [], None
        if spec.get("mode") == "script":
            cases = run_script_cases(spec, timeout, cpu_s)
        else:
            cases, load_error = run_function_cases(spec, timeout, cpu_s)
        if req.get("tests_py"):
            cases += run_operator_tests(req["tests_py"], timeout, cpu_s)
        result.update(cases=cases, load_error=load_error, total=len(cases),
                      passed=sum(1 for c in cases if c.get("ok")),
                      failed=sum(1 for c in cases if not c.get("ok")))
    result["duration_ms"] = int((time.monotonic() - t0) * 1000)
    body = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    mac = hmac.new(nonce, body.encode(), hashlib.sha256).hexdigest()
    sys.stdout.write("CIA-RESULT " + body + " " + mac + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
