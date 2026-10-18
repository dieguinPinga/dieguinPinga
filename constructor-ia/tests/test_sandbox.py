"""Pruebas del ejecutor aislado con Bubblewrap REAL y entradas adversariales.

En este entorno de CI no hay gestor systemd de usuario, por lo que el
limitador de cgroups se sustituye por NullLimiter (los límites de memoria,
tareas y CPU se verifican en la VivoBook con `constructor-ia selftest --stress`).
"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from constructor_ia.sandbox.executor import Executor, mem_stress_verdict
from constructor_ia.sandbox.limiter import LimiterError, NullLimiter, SystemdScopeLimiter

from .helpers import bwrap_works, make_cfg

SPEC = {"mode": "funcion", "function": "sumar", "float_tol": 1e-9, "cases": [
    {"args": [1, 2], "expect": 3, "visible": True},
    {"args": [10, -4], "expect": 6, "visible": False},
    {"args": [0.1, 0.2], "expect": 0.3, "visible": True},
]}


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class SandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_cfg(sandbox_timeout_s=15, sandbox_case_timeout_s=2)
        cls.ex = Executor(cls.cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        report = cls.ex.self_test()
        assert report["ok"], report

    def run_code(self, code, spec=SPEC, tests_py=None, cancel=None):
        return self.ex.run(code, spec, tests_py, cancel)

    def test_correct_solution_passes(self):
        r = self.run_code("def sumar(a, b):\n    return a + b\n")
        self.assertEqual(r.status, "ok", r.reason)
        self.assertEqual((r.result["passed"], r.result["total"]), (3, 3))
        self.assertTrue(r.cleanup_verified)

    def test_wrong_solution_fails(self):
        r = self.run_code("def sumar(a, b):\n    return a - b\n")
        self.assertEqual(r.result["failed"], 3)
        self.assertEqual(r.result["cases"][0]["got"], "-1")

    def test_syntax_and_import_errors_reported(self):
        r = self.run_code("def sumar(a, b):\n    return a +\n")
        self.assertEqual(r.result["passed"], 0)
        self.assertIn("SyntaxError", r.result["load_error"])
        r = self.run_code("raise RuntimeError('boom')\n")
        self.assertIn("boom", r.result["load_error"])

    def test_exceptions_and_raises(self):
        spec = {"mode": "funcion", "function": "dividir", "cases": [
            {"args": [6, 3], "expect": 2.0}, {"args": [1, 0], "raises": "ZeroDivisionError"}]}
        r = self.run_code("def dividir(a, b):\n    return a / b\n", spec)
        self.assertEqual(r.result["passed"], 2, r.result)

    def test_infinite_loop_case_timeout(self):
        t0 = time.monotonic()
        r = self.run_code("def sumar(a, b):\n    while True:\n        pass\n")
        self.assertEqual(r.status, "ok")
        self.assertTrue(all(c["status"] == "timeout" for c in r.result["cases"]))
        self.assertLess(time.monotonic() - t0, 15)

    def test_module_level_hang_is_bounded(self):
        r = self.run_code("import time\ntime.sleep(1000)\n")
        self.assertIn(r.status, ("ok", "timeout"))
        if r.status == "ok":
            self.assertEqual(r.result["passed"], 0)

    def test_cannot_read_hidden_cases_or_host(self):
        # La solución intenta leer pruebas, memoria del arnés y archivos del anfitrión.
        code = '''
import os
def sumar(a, b):
    found = []
    for p in ["/work/tests", "/home", "/root", "/etc/passwd", "/run", "/var", "/sys"]:
        if os.path.exists(p):
            found.append(p)
    ppid = os.getppid()
    for p in [f"/proc/{ppid}/mem", f"/proc/{ppid}/environ", f"/proc/{ppid}/fd/0"]:
        try:
            open(p, "rb").read(10)
            found.append(p)
        except OSError:
            pass
    return found
'''
        spec = {"mode": "funcion", "function": "sumar", "cases": [{"args": [1, 2], "expect": []}]}
        r = self.run_code(code, spec)
        self.assertEqual(r.result["passed"], 1, r.result["cases"][0])

    def test_no_network(self):
        code = '''
import socket
def sumar(a, b):
    out = []
    for host, port in [("1.1.1.1", 80), ("127.0.0.1", 11435), ("127.0.0.1", 11434), ("127.0.0.1", 1880)]:
        try:
            socket.create_connection((host, port), timeout=1).close()
            out.append(f"{host}:{port}")
        except OSError:
            pass
    return out
'''
        spec = {"mode": "funcion", "function": "sumar", "cases": [{"args": [0, 0], "expect": []}]}
        r = self.run_code(code, spec)
        self.assertEqual(r.result["passed"], 1, r.result["cases"][0])

    def test_cannot_write_outside_tmp(self):
        code = '''
def sumar(a, b):
    ok = []
    for p in ["/work/code/solucion.py", "/work/harness/harness.py", "/usr/x", "/x", "/tmp/x"]:
        try:
            with open(p, "a") as fh:
                fh.write("hack")
            ok.append(p)
        except OSError:
            pass
    return ok
'''
        spec = {"mode": "funcion", "function": "sumar", "cases": [{"args": [0, 0], "expect": ["/tmp/x"]}]}
        r = self.run_code(code, spec)
        self.assertEqual(r.result["passed"], 1, r.result["cases"][0])

    def test_forged_result_is_rejected(self):
        # Intento de falsificar el resultado: imprimir una línea CIA-RESULT y matar al arnés.
        code = '''
import os, signal, sys
print('CIA-RESULT {"passed": 3, "failed": 0, "total": 3, "cases": []} ' + "0"*64)
sys.stdout.flush()
os.write(2, b'CIA-RESULT {"passed":3,"failed":0,"total":3} x\\n')
os.kill(os.getppid(), signal.SIGKILL)
def sumar(a, b):
    return 0
'''
        r = self.run_code(code)
        self.assertFalse(r.status == "ok" and r.result.get("failed") == 0 and r.result.get("passed") == 3)

    def test_process_escape_and_exit_tricks(self):
        code = '''
import os, subprocess, sys
def sumar(a, b):
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True)
    os._exit(0)
'''
        r = self.run_code(code)
        self.assertEqual(r.result["passed"], 0)
        self.assertTrue(r.cleanup_verified)

    def test_ptrace_and_userns_blocked(self):
        code = '''
import ctypes, os
def sumar(a, b):
    libc = ctypes.CDLL(None, use_errno=True)
    res = [libc.ptrace(0, 0, None, None)]
    try:
        os.unshare(os.CLONE_NEWUSER)
        res.append("userns")
    except OSError:
        res.append("bloqueado")
    return res
'''
        spec = {"mode": "funcion", "function": "sumar", "cases": [{"args": [0, 0], "expect": [-1, "bloqueado"]}]}
        r = self.run_code(code, spec)
        self.assertEqual(r.result["passed"], 1, r.result["cases"][0])

    def test_output_flood_is_bounded(self):
        code = "import sys\ndef sumar(a, b):\n    sys.stderr.write('x' * 50_000_000)\n    return a + b\n"
        r = self.run_code(code)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.result["passed"], 3)

    def test_script_mode(self):
        spec = {"mode": "script", "cases": [{"stdin": "2 3\n", "stdout": "5\n", "visible": True},
                                            {"stdin": "10 5\n", "stdout": "15", "visible": False}]}
        r = self.run_code("a, b = map(int, input().split())\nprint(a + b)\n", spec)
        self.assertEqual(r.result["passed"], 2, r.result)
        r = self.run_code("print('mal')\n", spec)
        self.assertEqual(r.result["failed"], 2)

    def test_operator_tests(self):
        tests_py = ("def test_suma(sol):\n    assert sol.sumar(2, 2) == 4\n\n"
                    "def test_error(sol):\n    try:\n        sol.sumar('a', 1)\n    except TypeError:\n        return\n"
                    "    raise AssertionError('debía fallar')\n")
        r = self.run_code("def sumar(a, b):\n    return a + b\n", tests_py=tests_py)
        self.assertEqual(r.result["passed"], 5, r.result)

    def test_cancel_kills_run(self):
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        t0 = time.monotonic()
        r = self.run_code("import time\ntime.sleep(30)\n", cancel=cancel)
        self.assertEqual(r.status, "cancelled")
        self.assertLess(time.monotonic() - t0, 5)
        self.assertTrue(r.cleanup_verified)

    def test_global_timeout(self):
        cfg = make_cfg(sandbox_timeout_s=5, sandbox_case_timeout_s=30)
        ex = Executor(cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        ex.enabled = True
        t0 = time.monotonic()
        r = ex.run("def sumar(a, b):\n    while True: pass\n", SPEC, None)
        self.assertEqual(r.status, "timeout")
        self.assertLess(time.monotonic() - t0, 10)

    def test_hmac_tamper(self):
        self.assertIsNone(Executor._parse('CIA-RESULT {"passed":1} deadbeef', b"k" * 32))

    def test_run_dirs_removed(self):
        self.run_code("def sumar(a, b):\n    return a + b\n")
        self.assertEqual([p for p in self.cfg.runs_dir.iterdir()], [])


class BlockedExecutionTests(unittest.TestCase):
    def test_disabled_executor_refuses(self):
        ex = Executor(make_cfg(), limiter=NullLimiter())
        r = ex.run("print('hola')", SPEC, None)
        self.assertEqual(r.status, "blocked")

    @unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
    def test_unverified_limits_block_execution(self):
        # Sin modo de prueba, un limitador que no verifica impide ejecutar.
        ex = Executor(make_cfg(), limiter=NullLimiter(), allow_unverified_limits=False)
        report = ex.self_test()
        self.assertFalse(report["ok"])
        self.assertFalse(ex.enabled)
        self.assertEqual(ex.run("x=1", SPEC, None).status, "blocked")

    @unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
    def test_limiter_failure_blocks_before_payload(self):
        class Failing(NullLimiter):
            def verify(self, pid, unit, wait_s=5.0, alive=None):
                raise LimiterError("Límites de cgroup no aplicados: memory.max=max")
        ex = Executor(make_cfg(), limiter=Failing(), allow_unverified_limits=True)
        r = ex.run("def sumar(a,b): return a+b", SPEC, None, _skip_enabled_check=True)
        self.assertEqual(r.status, "blocked")
        self.assertIn("memory.max", r.reason)

    def test_disabled_by_config(self):
        ex = Executor(make_cfg(sandbox_enabled=False), limiter=NullLimiter(), allow_unverified_limits=True)
        self.assertFalse(ex.self_test()["ok"])


if __name__ == "__main__":
    unittest.main()


class _FakeCgroupLimiter(NullLimiter):
    """Limitador de prueba con un cgroup simulado: memory.events lo escribe la prueba."""

    name = "fake-cgroup-test-only"

    def __init__(self, cg_dir, oom_kill):
        self.cg = cg_dir
        (cg_dir / "memory.events").write_text(f"low 0\nhigh 0\nmax 3\noom 1\noom_kill {oom_kill}\noom_group_kill 0\n")
        self.pid = None
        self.reads_while_alive = 0

    def verify(self, pid, unit, wait_s=5.0, alive=None):
        self.pid = pid
        return {"verified": True, "cgroup": str(self.cg)}

    def memory_events(self, cg):
        if self.pid and os.path.exists(f"/proc/{self.pid}"):
            with open(f"/proc/{self.pid}/stat") as fh:
                if fh.read().split(") ")[1][0] != "Z":
                    self.reads_while_alive += 1
        return SystemdScopeLimiter(1, 1, 1).memory_events(cg)


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class OomRegressionTests(unittest.TestCase):
    """Regresión: VivoBook, selftest --stress mem → 'El arnés no produjo un resultado válido (código -15)'."""

    def executor(self, oom_kill):
        cfg = make_cfg(sandbox_timeout_s=15, sandbox_case_timeout_s=2)
        lim = _FakeCgroupLimiter(Path(tempfile.mkdtemp()), oom_kill)
        ex = Executor(cfg, limiter=lim)
        ex.enabled = True
        return ex, lim

    # El arnés muere por SIGTERM, como cuando systemd detiene la unidad tras un OOM.
    KILL_HARNESS = "import os, signal\nos.kill(os.getppid(), signal.SIGTERM)\ndef sumar(a, b):\n    return a + b\n"

    def test_supervisor_killed_with_oom_evidence_is_oom_not_ok(self):
        ex, _ = self.executor(oom_kill=1)
        r = ex.run(self.KILL_HARNESS, SPEC, None)
        self.assertEqual(r.status, "oom")
        self.assertIsNone(r.result)
        self.assertIn("oom_kill=1", r.reason)
        self.assertEqual(r.oom_kills, 1)
        self.assertTrue(r.cleanup_verified)

    def test_supervisor_killed_without_oom_is_error(self):
        ex, _ = self.executor(oom_kill=0)
        r = ex.run(self.KILL_HARNESS, SPEC, None)
        self.assertEqual(r.status, "error")
        self.assertIn("oom_kill=0", r.reason)
        self.assertIsNone(r.result)

    def test_unreadable_memory_events_is_error(self):
        ex = Executor(make_cfg(sandbox_timeout_s=15), limiter=NullLimiter(), allow_unverified_limits=True)
        ex.enabled = True
        r = ex.run(self.KILL_HARNESS, SPEC, None)
        self.assertEqual(r.status, "error")
        self.assertIn("no disponible", r.reason)

    def test_oom_killed_child_is_reported_and_confirmed(self):
        # Con OOMPolicy=continue el kernel mata solo al hijo; el arnés sigue y firma el resultado.
        ex, lim = self.executor(oom_kill=1)
        code = "import os, signal\ndef sumar(a, b):\n    os.kill(os.getpid(), signal.SIGKILL)\n"
        t0 = time.monotonic()
        r = ex.run(code, SPEC, None)
        self.assertEqual(r.status, "ok", r.reason)
        self.assertEqual(r.result["passed"], 0)
        self.assertEqual(r.result["oom_kills"], 1)
        self.assertIn("confirmado: límite de memoria", r.result["cases"][0]["detail"])
        self.assertGreater(lim.reads_while_alive, 0, "memory.events se leyó con el arnés aún vivo")
        self.assertLess(time.monotonic() - t0, 10, "el anfitrión cierra stdin y el arnés no queda esperando")
        self.assertTrue(r.cleanup_verified)

    def test_normal_run_not_delayed_by_handshake(self):
        ex, _ = self.executor(oom_kill=0)
        t0 = time.monotonic()
        r = ex.run("def sumar(a, b):\n    return a + b\n", SPEC, None)
        self.assertEqual((r.status, r.result["passed"], r.oom_kills), ("ok", 3, 0))
        self.assertLess(time.monotonic() - t0, 5)


class MemStressVerdictTests(unittest.TestCase):
    def test_verdicts(self):
        ok = {"code": -9, "stdout": ""}
        self.assertTrue(mem_stress_verdict(ok, 1)[0])
        self.assertFalse(mem_stress_verdict(ok, 0)[0], "SIGKILL sin oom_kill no verifica el límite")
        self.assertFalse(mem_stress_verdict(ok, None)[0])
        self.assertFalse(mem_stress_verdict({"code": -15, "stdout": ""}, 1)[0], "-15 nunca es éxito")
        self.assertFalse(mem_stress_verdict({"code": 0, "stdout": "sobrevivio\n"}, 0)[0])
        self.assertFalse(mem_stress_verdict(None, 1)[0])

    def test_wrap_and_memory_events_parsing(self):
        lim = SystemdScopeLimiter(256, 32, 50)
        self.assertIn("OOMPolicy=continue", lim.wrap(["x"], "cia-job-0123456789abcdef"))
        d = Path(tempfile.mkdtemp())
        (d / "memory.events").write_text("low 0\nhigh 0\nmax 12\noom 2\noom_kill 1\noom_group_kill 0\n")
        self.assertEqual(lim.memory_events(str(d))["oom_kill"], 1)
        self.assertIsNone(lim.memory_events(str(d / "no-existe")))
        self.assertIsNone(lim.memory_events(None))
