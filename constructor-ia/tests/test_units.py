"""Pruebas unitarias: configuración, autenticación, validación, extracción, seccomp, limitador."""
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from constructor_ia import acceptance, auth, codeextract, config, logsetup, prompts
from constructor_ia.db import Database
from constructor_ia.sandbox import seccomp
from constructor_ia.sandbox.limiter import LimiterError, SystemdScopeLimiter
from constructor_ia.workers import parse_draft


def write_cfg(text: str) -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "c.toml"
    p.write_text(text)
    return p


class ConfigTests(unittest.TestCase):
    def test_defaults_loopback_8766(self):
        cfg = config.load(Path("/nonexistent.toml"), base_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(cfg.listen, ["127.0.0.1"])
        self.assertEqual(cfg.port, 8766)
        self.assertEqual(cfg.ollama_url, "http://127.0.0.1:11435")
        self.assertEqual(cfg.builder.num_ctx, 4096)

    def test_rejects_wildcard_and_lan(self):
        for addr in ("0.0.0.0", "::", "192.168.1.10"):
            with self.assertRaises(config.ConfigError):
                config.load(write_cfg(f'[server]\nlisten = ["{addr}"]\n'))

    def test_accepts_tailscale_ip(self):
        cfg = config.load(write_cfg('[server]\nlisten = ["127.0.0.1", "100.101.102.103"]\n'))
        self.assertIn("100.101.102.103", cfg.listen)

    def test_rejects_reserved_ports_and_main_ollama(self):
        for port in (1880, 11434, 11435, 18789, 3306):
            with self.assertRaises(config.ConfigError):
                config.load(write_cfg(f"[server]\nport = {port}\n"))
        with self.assertRaises(config.ConfigError):
            config.load(write_cfg('[ollama]\nurl = "http://127.0.0.1:11434"\n'))
        with self.assertRaises(config.ConfigError):
            config.load(write_cfg('[ollama]\nurl = "http://10.0.0.5:11435"\n'))

    def test_bounds(self):
        with self.assertRaises(config.ConfigError):
            config.load(write_cfg("[ollama.builder]\nnum_ctx = 100000\n"))
        with self.assertRaises(config.ConfigError):
            config.load(write_cfg("[sandbox]\nmemory_mb = 4096\n"))
        with self.assertRaises(config.ConfigError):
            config.load(write_cfg("[engine]\nmax_attempts = 9\nmax_attempts_limit = 6\n"))

    def test_example_config_is_valid(self):
        example = Path(__file__).resolve().parent.parent / "config" / "config.example.toml"
        cfg = config.load(example, base_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(cfg.port, 8766)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "t.db")
        self.db.migrate()
        self.auth = auth.Auth(self.db, 12, 7)

    def test_password_hash(self):
        h = auth.hash_password("contraseña-larga-1")
        self.assertTrue(auth.verify_password("contraseña-larga-1", h))
        self.assertFalse(auth.verify_password("otra", h))
        self.assertNotIn("contraseña", h)

    def test_user_and_sessions(self):
        with self.assertRaises(ValueError):
            self.auth.create_user("diego", "corta")
        with self.assertRaises(ValueError):
            self.auth.create_user("../x", "suficientemente-larga")
        self.auth.create_user("diego", "suficientemente-larga")
        self.assertIsNone(self.auth.authenticate("diego", "mala-contraseña"))
        self.assertIsNone(self.auth.authenticate("nadie", "suficientemente-larga"))
        uid = self.auth.authenticate("diego", "suficientemente-larga")
        token, csrf = self.auth.create_session(uid, "127.0.0.1", "ua")
        s = self.auth.get_session(token)
        self.assertEqual(s.username, "diego")
        self.assertEqual(s.csrf, csrf)
        # El token no se guarda en claro.
        self.assertIsNone(self.db.one("SELECT 1 FROM sessions WHERE token_hash=?", (token,)))
        self.assertIsNone(self.auth.get_session(token + "x"))
        self.auth.set_password("diego", "otra-contraseña-larga")
        self.assertIsNone(self.auth.get_session(token), "cambiar la contraseña invalida las sesiones")

    def test_session_expiry(self):
        self.auth.create_user("diego", "suficientemente-larga")
        token, _ = self.auth.create_session(1, "ip", "ua")
        self.db.execute("UPDATE sessions SET last_seen=last_seen-13*3600")
        self.assertIsNone(self.auth.get_session(token))

    def test_limiter_backoff(self):
        lim = auth.LoginLimiter(per_ip_free=3, global_max=100)
        for _ in range(3):
            self.assertEqual(lim.check("a"), 0)
            lim.failure("a")
        self.assertGreater(lim.check("a"), 0)
        self.assertEqual(lim.check("b"), 0)
        glob = auth.LoginLimiter(per_ip_free=100, global_max=5)
        for i in range(5):
            glob.failure(f"ip{i}")
        self.assertGreater(glob.check("nueva"), 0, "límite global (todo llega desde 127.0.0.1 tras un proxy)")


class AcceptanceTests(unittest.TestCase):
    base = {"title": "Sumar", "description": "Sumar dos números", "mode": "funcion", "function": "sumar",
            "cases": [{"args": [1, 2], "expect": 3}], "authorize_execution": True, "cases_reviewed": True}

    def v(self, **over):
        d = dict(self.base, **over)
        return acceptance.validate_mission(d, 6, 3)

    def test_valid(self):
        m = self.v()
        self.assertEqual(m["spec"]["function"], "sumar")
        self.assertEqual(m["max_attempts"], 3)

    def test_requires_authorization_and_review(self):
        with self.assertRaises(acceptance.SpecError):
            self.v(authorize_execution=False)
        with self.assertRaises(acceptance.SpecError):
            self.v(cases_reviewed="true")

    def test_requires_tests(self):
        with self.assertRaises(acceptance.SpecError):
            self.v(cases=[])

    def test_bad_inputs(self):
        for over in ({"function": "import os"}, {"function": "class"}, {"mode": "bash"}, {"max_attempts": 50},
                     {"cases": [{"args": "1,2", "expect": 3}]}, {"cases": [{"args": [1]}]},
                     {"cases": [{"args": [float("nan")], "expect": 1}]}, {"title": "x" * 500},
                     {"cases": [{"args": [1], "raises": "os.system"}]},
                     {"tests_py": "def nada(): pass"}, {"tests_py": "def test_x(:"},
                     {"cases": [{"args": [1], "expect": 1}] * 41}):
            with self.subTest(over=str(over)[:60]), self.assertRaises(acceptance.SpecError):
                self.v(**over)

    def test_script_mode(self):
        m = self.v(mode="script", cases=[{"stdin": "1 2\n", "stdout": "3\n"}])
        self.assertEqual(m["spec"]["cases"][0]["stdout"], "3\n")


class ExtractTests(unittest.TestCase):
    def test_fenced(self):
        code, note = codeextract.extract_code("Hola\n```python\ndef f():\n    return 1\n```\nchau")
        self.assertIn("def f", code)
        self.assertEqual(note, "")

    def test_unclosed_fence(self):
        code, note = codeextract.extract_code("```python\ndef f():\n    return 1\n")
        self.assertIn("def f", code)
        self.assertIn("sin cerrar", note)

    def test_no_code(self):
        code, _ = codeextract.extract_code("No sé hacerlo, lo siento.")
        self.assertIsNone(code)

    def test_validate(self):
        spec = {"mode": "funcion", "function": "f"}
        self.assertEqual(codeextract.validate_code("def f():\n    return 1\n", spec), [])
        self.assertTrue(codeextract.validate_code("def f(:\n", spec)[0].startswith("Error de sintaxis"))
        self.assertIn("No se define", codeextract.validate_code("def g():\n    pass\n", spec)[0])
        self.assertIn("numpy", codeextract.validate_code("import numpy\ndef f(): pass\n", spec)[0])
        self.assertTrue(codeextract.validate_code("x=" + "(" * 300 + ")" * 300, {"mode": "script"}) is not None)


class PromptTests(unittest.TestCase):
    def test_hidden_cases_not_revealed(self):
        spec = {"mode": "funcion", "function": "f", "cases": [
            {"args": [1], "expect": 111111, "visible": True}, {"args": [2], "expect": 999999, "visible": False}]}
        msgs = prompts.initial("tarea", spec, 6000)
        self.assertIn("111111", msgs[1]["content"])
        self.assertNotIn("999999", msgs[1]["content"])
        result = {"cases": [{"n": 2, "kind": "caso", "visible": False, "ok": False, "status": "fallo",
                             "got": "5", "expected": "999999", "args": [2]}]}
        rep = prompts.failure_report(result, [], 2000)
        self.assertNotIn("999999", rep)
        self.assertIn("oculto", rep)

    def test_budget(self):
        self.assertGreater(prompts.budget_chars(4096, 1536), 7000)
        msgs = prompts.repair("d" * 50000, {"mode": "script", "cases": []}, "c" * 50000, "r", 8000)
        self.assertLess(len(msgs[1]["content"]), 9000)


class SeccompTests(unittest.TestCase):
    def test_filter_structure(self):
        if not seccomp.supported():
            self.skipTest("no x86_64")
        prog = seccomp.build_filter()
        self.assertEqual(len(prog) % 8, 0)
        ins = [struct.unpack("=HBBI", prog[i:i + 8]) for i in range(0, len(prog), 8)]
        self.assertEqual(ins[-1], (0x06, 0, 0, seccomp.RET_ERRNO_EPERM))
        self.assertEqual(ins[-2], (0x06, 0, 0, seccomp.RET_ALLOW))
        # Todos los saltos condicionales apuntan al retorno EPERM.
        for i, (code, jt, jf, k) in enumerate(ins):
            if code in (0x15, 0x35) and i > 1:
                self.assertEqual(i + 1 + jt, len(ins) - 1)
        nrs = {k for code, _, _, k in ins[5:-2]}
        self.assertIn(101, nrs)  # ptrace
        self.assertIn(272, nrs)  # unshare


class LimiterTests(unittest.TestCase):
    def test_wrap(self):
        lim = SystemdScopeLimiter(256, 32, 50)
        cmd = lim.wrap(["/usr/bin/bwrap", "x"], "cia-job-0123456789abcdef")
        self.assertIn("--scope", cmd)
        self.assertIn("--user", cmd)
        self.assertIn(f"MemoryMax={256 * 1024 * 1024}", cmd)
        self.assertIn("TasksMax=32", cmd)
        self.assertIn("CPUQuota=50%", cmd)
        self.assertEqual(cmd[-2:], ["/usr/bin/bwrap", "x"])
        with self.assertRaises(LimiterError):
            lim.wrap(["x"], "cia-job-; rm -rf /")

    def test_verify_reads_real_cgroup_files(self):
        root = Path(tempfile.mkdtemp())
        cg = root / "user.slice" / "cia-job-0123456789abcdef.scope"
        cg.mkdir(parents=True)
        lim = SystemdScopeLimiter(256, 32, 50)
        lim.cgroup_of = lambda pid: cg
        (cg / "memory.max").write_text(str(256 * 1024 * 1024))
        (cg / "pids.max").write_text("32")
        (cg / "cpu.max").write_text("50000 100000")
        info = lim.verify(1, "cia-job-0123456789abcdef", wait_s=0.5)
        self.assertTrue(info["verified"])
        (cg / "memory.max").write_text("max")
        with self.assertRaises(LimiterError):
            lim.verify(1, "cia-job-0123456789abcdef", wait_s=0.5)
        (cg / "memory.max").write_text(str(256 * 1024 * 1024))
        (cg / "cpu.max").write_text("max 100000")
        with self.assertRaises(LimiterError):
            lim.verify(1, "cia-job-0123456789abcdef", wait_s=0.5)
        lim.cgroup_of = lambda pid: root / "otro.scope"
        with self.assertRaises(LimiterError):
            lim.verify(1, "cia-job-0123456789abcdef", wait_s=0.3)


class MiscTests(unittest.TestCase):
    def test_redact(self):
        s = logsetup.redact('login password=hunter2 token: abc csrf="zzz" cia_session=tok123')
        for secret in ("hunter2", "abc", "zzz", "tok123"):
            self.assertNotIn(secret, s)

    def test_parse_draft(self):
        d = parse_draft('Aquí va:\n{"title": "T", "description": "D", "mode": "funcion", "function": "f",'
                        ' "cases": [{"args": [1], "expect": 2}, {"bad": 1}]}')
        self.assertEqual(d["function"], "f")
        self.assertEqual(len(d["cases"]), 1)
        self.assertIsNone(parse_draft("sin json"))
        self.assertEqual(parse_draft('{"function": "rm -rf"}')["function"], "")


if __name__ == "__main__":
    unittest.main()
