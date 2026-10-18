"""Pruebas del motor y la cola con Ollama simulado y sandbox real (bwrap)."""
import json
import threading
import time
import unittest

from constructor_ia.app import App
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter

from .fake_ollama import FakeOllama
from .helpers import bwrap_works, fn_code, make_cfg, wait_for

MISSION = {"title": "Sumar", "description": "Sumar a y b", "spec": {
    "mode": "funcion", "function": "sumar", "float_tol": 1e-9,
    "cases": [{"args": [1, 2], "expect": 3, "visible": True}, {"args": [5, 5], "expect": 10, "visible": False}]},
    "tests_py": "", "max_attempts": 3}


def make_app(responses, delay=0.0, start=True, cfg=None):
    fake = FakeOllama(responses, delay=delay)
    cfg = cfg or make_cfg(fake.url, sandbox_timeout_s=15, sandbox_case_timeout_s=2)
    cfg.ollama_url = fake.url
    ex = Executor(cfg, limiter=NullLimiter(), allow_unverified_limits=True)
    from constructor_ia.ollama import OllamaClient
    app = App(cfg, executor=ex, ollama=OllamaClient(fake.url))
    if start:
        app.start()
    return app, fake


def state(app, mid):
    return app.missions.get(mid)["state"]


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class EngineTests(unittest.TestCase):
    def tearDown(self):
        for obj in getattr(self, "_cleanup", []):
            obj()

    def app(self, *a, **k):
        app, fake = make_app(*a, **k)
        self._cleanup = getattr(self, "_cleanup", []) + [app.stop, fake.close]
        return app, fake

    def test_approved_first_try_and_not_rerun(self):
        app, fake = self.app([fn_code("return a + b")])
        mid = app.missions.create(MISSION, "qwen3:4b-instruct", "diego")
        app.mission_worker.notify()
        wait_for(lambda: state(app, mid) in ("aprobada", "fallida"))
        m = app.missions.get(mid)
        self.assertEqual(m["state"], "aprobada", m["error"])
        self.assertEqual(m["final_attempt"], 1)
        self.assertEqual(len(fake.requests), 1)
        req = fake.requests[0]
        self.assertEqual(req["model"], "qwen3:4b-instruct")
        self.assertEqual(req["options"]["num_ctx"], 4096)
        files = {r["rel_path"] for r in app.db.all("SELECT rel_path FROM files WHERE mission_id=?", (mid,))}
        self.assertTrue({"intento-1/solucion.py", "intento-1/respuesta_modelo.md", "intento-1/resultado.json",
                         "final/solucion.py", "pruebas/especificacion.json"} <= files)
        att = app.db.one("SELECT * FROM attempts WHERE mission_id=?", (mid,))
        self.assertEqual((att["passed"], att["total"]), (2, 2))
        kinds = [r["kind"] for r in app.db.all("SELECT kind FROM events WHERE mission_id=? ORDER BY id", (mid,))]
        for k in ("generacion", "codigo_recibido", "validacion", "pruebas", "pruebas_aprobadas", "terminada"):
            self.assertIn(k, kinds)
        # No se vuelve a ejecutar: no se puede reclamar ni reencolar.
        self.assertIsNone(app.missions.claim_next("x"))
        with self.assertRaises(ValueError):
            app.missions.requeue(mid, "diego")
        time.sleep(0.5)
        self.assertEqual(len(fake.requests), 1)

    def test_repair_with_real_errors(self):
        app, fake = self.app([fn_code("return a - b"), fn_code("return a + b")])
        mid = app.missions.create(MISSION, "m", "diego")
        app.mission_worker.notify()
        wait_for(lambda: state(app, mid) in ("aprobada", "fallida"))
        self.assertEqual(state(app, mid), "aprobada")
        self.assertEqual(app.missions.get(mid)["final_attempt"], 2)
        repair_prompt = fake.requests[1]["messages"][1]["content"]
        self.assertIn("return a - b", repair_prompt)
        self.assertIn("obtenido -1", repair_prompt)
        self.assertNotIn("esperado 10", repair_prompt, "no se filtra el esperado de casos ocultos")
        self.assertIn("Caso oculto", repair_prompt)

    def test_fails_after_max_attempts(self):
        app, fake = self.app(["No sé.", fn_code("return a +"), fn_code("return 0")])
        mid = app.missions.create(MISSION, "m", "diego")
        app.mission_worker.notify()
        wait_for(lambda: state(app, mid) in ("aprobada", "fallida"))
        m = app.missions.get(mid)
        self.assertEqual(m["state"], "fallida")
        self.assertIn("3 intentos", m["error"])
        atts = app.db.all("SELECT test_status FROM attempts WHERE mission_id=? ORDER BY n", (mid,))
        self.assertEqual([a[0] for a in atts], ["no_ejecutadas", "no_ejecutadas", "fallidas"])

    def test_cancel_during_generation(self):
        app, fake = self.app([fn_code("return a + b") * 20], delay=0.2)
        mid = app.missions.create(MISSION, "m", "diego")
        app.mission_worker.notify()
        wait_for(lambda: state(app, mid) == "generando")
        time.sleep(0.5)
        self.assertEqual(app.missions.request_cancel(mid), "solicitada")
        app.mission_worker.cancel_current(mid)
        wait_for(lambda: state(app, mid) == "cancelada", timeout=10)
        wait_for(lambda: fake.active == 0, timeout=10)
        self.assertIsNone(app.mission_worker.current)
        self.assertIsNone(app.gate.holder)

    def test_cancel_pending_and_queue_order(self):
        app, fake = self.app([fn_code("return a + b")] * 3, delay=0.05)
        ids = [app.missions.create(MISSION, "m", "diego") for _ in range(3)]
        self.assertEqual(app.missions.request_cancel(ids[2]), "cancelada")
        app.mission_worker.notify()
        wait_for(lambda: state(app, ids[1]) == "aprobada", timeout=60)
        self.assertEqual(state(app, ids[0]), "aprobada")
        self.assertEqual(state(app, ids[2]), "cancelada")
        self.assertEqual(fake.max_active, 1, "nunca hubo dos inferencias simultáneas")
        started = [app.missions.get(i)["started_at"] for i in ids[:2]]
        self.assertLess(started[0], started[1])

    def test_ollama_down(self):
        app, fake = self.app([])
        fake.close()
        mid = app.missions.create(MISSION, "m", "diego")
        app.mission_worker.notify()
        wait_for(lambda: state(app, mid) == "fallida")
        self.assertIn("Ollama", app.missions.get(mid)["error"])
        self.assertIsNotNone(app.db.kv_get("last_coordinator_error"))

    def test_blocked_executor_never_approves(self):
        app, fake = self.app([fn_code("return a + b")], start=False)
        app.executor.allow_unverified_limits = False
        app.start()
        self.assertFalse(app.executor.enabled)
        mid = app.missions.create(MISSION, "m", "diego")
        app.mission_worker.notify()
        wait_for(lambda: state(app, mid) == "fallida")
        self.assertIn("bloqueada", app.missions.get(mid)["error"])

    def test_recovery_after_restart(self):
        app, fake = self.app([fn_code("return a + b")], delay=0.3)
        mid = app.missions.create(MISSION, "m", "diego")
        app.mission_worker.notify()
        wait_for(lambda: state(app, mid) == "generando")
        # Simula una caída: un nuevo proceso sobre la misma base sin pasar por stop().
        app2, fake2 = make_app([fn_code("return a + b")], start=False, cfg=app.cfg)
        self._cleanup += [fake2.close]
        app.mission_worker.shutdown()
        recovered = app2.missions.recover_interrupted()
        self.assertIn(mid, recovered)
        self.assertEqual(state(app2, mid), "interrumpida")
        self.assertIsNone(app2.missions.claim_next("w"), "una misión interrumpida no se re-ejecuta sola")
        new = app2.missions.requeue(mid, "diego")
        self.assertNotEqual(new, mid)
        self.assertEqual(state(app2, new), "pendiente")

    def test_shutdown_marks_interrupted_not_cancelled(self):
        app, fake = self.app([fn_code("return a + b") * 30], delay=0.2)
        mid = app.missions.create(MISSION, "m", "diego")
        app.mission_worker.notify()
        wait_for(lambda: state(app, mid) == "generando")
        app.stop()
        self.assertEqual(state(app, mid), "interrumpida")

    def test_chat_and_draft(self):
        draft = json.dumps({"title": "Contar vocales", "description": "Cuenta vocales", "mode": "funcion",
                            "function": "contar", "cases": [{"args": ["hola"], "expect": 2}]})
        app, fake = self.app(["<think>pensando</think>¡Hola! ¿En qué te ayudo?", draft])
        now = time.time()
        cid = app.db.execute("INSERT INTO conversations(title, created_by, created_at, updated_at) VALUES(?,?,?,?)",
                             ("c", "diego", now, now)).lastrowid
        for kind, text in (("texto", "hola"), ("borrador", "armá la misión")):
            app.db.execute("INSERT INTO messages(conversation_id, role, kind, content, status, created_at)"
                           " VALUES(?,?,?,?,?,?)", (cid, "user", kind, text, "lista", now))
            app.db.execute("INSERT INTO messages(conversation_id, role, kind, content, status, created_at)"
                           " VALUES(?,?,?,?,?,?)", (cid, "assistant", kind, "", "pendiente", now))
            app.chat_worker.notify()
            wait_for(lambda: app.db.one("SELECT COUNT(*) FROM messages WHERE status='pendiente' OR status='generando'")[0] == 0)
        rows = app.db.all("SELECT * FROM messages WHERE role='assistant' ORDER BY id")
        self.assertEqual(rows[0]["content"], "¡Hola! ¿En qué te ayudo?")
        self.assertEqual(json.loads(rows[1]["meta_json"])["draft"]["function"], "contar")
        self.assertEqual(fake.requests[0]["model"], "qwen3:1.7b")
        self.assertFalse(fake.requests[0]["think"])
        # Ninguna conversación crea misiones ni ejecuta código.
        self.assertEqual(app.db.one("SELECT COUNT(*) FROM missions")[0], 0)


if __name__ == "__main__":
    unittest.main()
