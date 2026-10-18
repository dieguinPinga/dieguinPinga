"""Revisiones de proyecto (v0.4): continuar misiones terminadas con nuevas instrucciones humanas."""
import json
import time
import unittest

from constructor_ia.revisions import RevisionError

from .helpers import bwrap_works, fn_code, free_port, wait_for
from .test_engine import MISSION, make_app
from .test_taller_web import CSS, GOOD, INDEX, JS, SPEC as WEB_SPEC, model_answer
from .test_web import PASSWORD, Client


def state(app, mid):
    return app.missions.get(mid)["state"]


def run_until_done(app, mid, timeout=60):
    app.mission_worker.notify()
    wait_for(lambda: state(app, mid) in ("aprobada", "fallida", "cancelada"), timeout=timeout)
    return app.missions.get(mid)


def sha_map(app, mid):
    return {r[0]: r[1] for r in app.db.all("SELECT rel_path, sha256 FROM files WHERE mission_id=?", (mid,))}


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class PythonRevisionTests(unittest.TestCase):
    def setUp(self):
        self.app, self.fake = make_app([fn_code("return a + b")])
        self.addCleanup(self.fake.close)
        self.addCleanup(self.app.stop)
        self.root = self.app.missions.create(MISSION, "qwen3:4b-instruct", "diego")
        self.assertEqual(run_until_done(self.app, self.root)["state"], "aprobada")

    def revise(self, base, **body):
        body.setdefault("instruction", "Que también acepte un tercer sumando opcional c (por defecto 0)")
        body.setdefault("authorize_execution", True)
        prep = self.app.revisions.prepare(base, body, 6, 3)
        new = self.app.missions.create(prep["mission"], "m", "diego", lineage=prep["lineage"])
        self.app.revisions.store_base(new, prep["mission"]["spec"]["mode"], prep["files"])
        return new

    def test_revision_reuses_code_keeps_tests_and_becomes_current(self):
        before = sha_map(self.app, self.root)
        self.fake.responses.append("```python\ndef sumar(a, b, c=0):\n    return a + b + c\n```")
        rev = self.revise(self.root, cases=[{"args": [1, 2, 3], "expect": 6, "visible": True}], cases_reviewed=True)
        m = run_until_done(self.app, rev)
        self.assertEqual(m["state"], "aprobada", m["error"])
        self.assertEqual((m["project_id"], m["revision"], m["parent_id"], m["base_ref"]), (self.root, 2, self.root, "final"))
        spec = json.loads(m["spec_json"])
        self.assertEqual(len(spec["cases"]), 3, "2 heredadas + 1 nueva")
        self.assertEqual(spec["revision"]["inherited_cases"], 2)
        att = self.app.db.one("SELECT passed, total FROM attempts WHERE mission_id=?", (rev,))
        self.assertEqual((att[0], att[1]), (3, 3), "las pruebas anteriores también se ejecutaron")
        prompt = self.fake.requests[-1]["messages"][1]["content"]
        self.assertIn("MODIFICACIÓN", prompt)
        self.assertIn("tercer sumando", prompt)
        self.assertIn("return a + b", prompt, "recibe el código anterior, no empieza de cero")
        self.assertEqual(self.app.files.read_rel(rev, "base/solucion.py"), self.app.files.read_rel(self.root, "final/solucion.py"))
        self.assertEqual(sha_map(self.app, self.root), before, "la versión anterior es inmutable")
        p = self.app.revisions.project(self.root)
        self.assertEqual(p["current_mission"], rev)
        self.assertEqual([r["revision"] for r in p["revisions"]], [1, 2])

    def test_failed_revision_preserves_last_approved(self):
        self.fake.responses += [fn_code("return a - b")] * 2
        rev = self.revise(self.root, max_attempts=2)
        m = run_until_done(self.app, rev)
        self.assertEqual(m["state"], "fallida")
        self.assertEqual(self.app.db.one("SELECT COUNT(*) FROM attempts WHERE mission_id=?", (rev,))[0], 2,
                         "los intentos son reparaciones DENTRO de la revisión")
        self.assertEqual(self.app.revisions.project(self.root)["current_mission"], self.root)
        # Se puede seguir desde la revisión fallida (su último intento) o desde la vigente.
        self.fake.responses.append(fn_code("return a + b"))
        rev3 = self.revise(rev, instruction="Volvé a sumar correctamente")
        self.assertEqual(self.app.missions.get(rev3)["base_ref"], "intento-2")
        self.assertEqual(run_until_done(self.app, rev3)["state"], "aprobada")
        self.assertEqual(self.app.missions.get(rev3)["revision"], 3)

    def test_history_and_set_current(self):
        self.fake.responses += ["```python\ndef sumar(a, b, c=0):\n    return a + b + c\n```",
                                "```python\ndef sumar(a, b, c=0):\n    return a + b + c\n```"]
        r2 = self.revise(self.root, instruction="Agregá un tercer sumando")
        run_until_done(self.app, r2)
        r3 = self.revise(r2, instruction="Documentá la función")
        run_until_done(self.app, r3)
        spec = json.loads(self.app.missions.get(r3)["spec_json"])
        self.assertEqual([h["revision"] for h in spec["revision"]["history"]], [2])
        self.assertIn("r2: Agregá un tercer sumando", self.fake.requests[-1]["messages"][1]["content"])
        self.app.revisions.set_current(self.root, r2, "diego")
        self.assertEqual(self.app.revisions.project(self.root)["current_mission"], r2)
        with self.assertRaises(RevisionError):
            self.app.revisions.set_current(self.root, "M20990101-abcdef", "diego")

    def test_validation(self):
        for body, frag in (({"instruction": "x"}, "Escribí"), ({"base_ref": "intento-9"}, "no tiene archivos"),
                           ({"base_ref": "../x"}, "inválida"),
                           ({"cases": [{"args": [1], "expect": 1}], "cases_reviewed": False}, "revisó"),
                           ({"authorize_execution": False}, "autorizar")):
            with self.subTest(body=body), self.assertRaises(RevisionError) as ctx:
                self.revise(self.root, **body)
            self.assertIn(frag, str(ctx.exception))
        pending = self.app.missions.create(MISSION, "m", "diego")
        self.app.missions.request_cancel(pending)
        with self.assertRaises(RevisionError):
            self.app.revisions.prepare(pending, {"instruction": "algo nuevo"}, 6, 3)  # cancelada sin archivos


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class WebRevisionTests(unittest.TestCase):
    def test_web_revision_modifies_only_needed_files(self):
        new_js = JS.replace("lista.append(li);", "lista.append(li);\n    li.className = 'item';")
        app, fake = make_app([model_answer(GOOD), model_answer({"js/app.js": new_js})])
        self.addCleanup(fake.close)
        self.addCleanup(app.stop)
        root = app.missions.create({"title": "Tablero", "description": "Tablero de mensajes", "spec": WEB_SPEC,
                                    "tests_py": "", "max_attempts": 2}, "m", "diego")
        self.assertEqual(run_until_done(app, root)["state"], "aprobada")
        before = sha_map(app, root)
        prep = app.revisions.prepare(root, {"instruction": "Agregá una clase CSS a cada mensaje de la lista",
                                            "authorize_execution": True, "cases_reviewed": True,
                                            "checks": [{"tipo": "js_contiene", "valor": "className"}]}, 6, 3)
        rev = app.missions.create(prep["mission"], "m", "diego", lineage=prep["lineage"])
        app.revisions.store_base(rev, "web", prep["files"])
        m = run_until_done(app, rev)
        self.assertEqual(m["state"], "aprobada", m["error"])
        final = app.revisions.version_files(rev, "final")
        self.assertEqual(final["index.html"], INDEX, "reutilizado sin regenerar")
        self.assertEqual(final["css/estilo.css"], CSS)
        self.assertEqual(final["js/app.js"], new_js)
        prompt = fake.requests[-1]["messages"][1]["content"]
        self.assertIn("### archivo: js/app.js", prompt)
        self.assertIn("SOLO los archivos", prompt)
        events = [r[0] for r in app.db.all("SELECT message FROM events WHERE mission_id=?", (rev,))]
        self.assertTrue(any("modificados: js/app.js" in e and "sin cambios: 2" in e for e in events), events)
        self.assertTrue(any("2 reutilizados sin regenerar" in e for e in events), events)
        self.assertEqual(sha_map(app, root), before)
        d = app.revisions.diff(f"{root}:final", f"{rev}:final")
        status = {f["path"]: f["status"] for f in d["files"]}
        self.assertEqual(status, {"index.html": "igual", "css/estilo.css": "igual", "js/app.js": "modificado"})
        self.assertTrue(any("className" in line for f in d["files"] for line in f["diff"]))
        spec = json.loads(m["spec_json"])
        self.assertEqual(len(spec["checks"]), len(WEB_SPEC["checks"]) + 1)

    def test_seeded_project_keeps_data_source_without_secrets(self):
        spec = dict(WEB_SPEC, seed_files={"datos/a.json": json.dumps({"nombre": "a", "columnas": ["x"], "filas": [[1]]})},
                    data_source={"conn": "cueva", "fact_id": 7, "queries": [{"nombre": "a", "sql": "SELECT 1 AS x"}]})
        app, fake = make_app([model_answer(GOOD), model_answer({"datos/a.json": "{}", "js/app.js": JS + "// v2\n"})])
        self.addCleanup(fake.close)
        self.addCleanup(app.stop)
        root = app.missions.create({"title": "T", "description": "d", "spec": spec, "tests_py": "", "max_attempts": 1},
                                   "m", "diego")
        self.assertEqual(run_until_done(app, root)["state"], "aprobada")
        prep = app.revisions.prepare(root, {"instruction": "Agregá un comentario de versión", "authorize_execution": True},
                                     6, 3)
        self.assertEqual(prep["mission"]["spec"]["data_source"]["fact_id"], 7)
        rev = app.missions.create(prep["mission"], "m", "diego", lineage=prep["lineage"])
        app.revisions.store_base(rev, "web", prep["files"])
        self.assertEqual(run_until_done(app, rev)["state"], "aprobada")
        final = app.revisions.version_files(rev, "final")
        self.assertEqual(json.loads(final["datos/a.json"])["filas"], [[1]], "las instantáneas siguen inmutables")
        self.assertNotIn("password", json.dumps(json.loads(app.missions.get(rev)["spec_json"])).lower())


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class RevisionApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from constructor_ia.web.server import serve

        cls.app, cls.fake = make_app([fn_code("return a + b")] * 10, start=False)
        cls.app.cfg.port = free_port()
        cls.app.auth.create_user("diego", PASSWORD)
        cls.app.start()
        cls.servers = serve(cls.app)
        c = cls.client()
        st, d = c.req("POST", "/api/missions", {"title": "Sumar", "description": "Sumar a y b", "mode": "funcion",
                                                "function": "sumar", "cases": [{"args": [1, 2], "expect": 3}],
                                                "authorize_execution": True, "cases_reviewed": True})
        cls.root = d["id"]
        wait_for(lambda: state(cls.app, cls.root) == "aprobada", timeout=60)

    @classmethod
    def tearDownClass(cls):
        for s in cls.servers:
            s.stopping.set()
            s.shutdown()
            s.server_close()
        cls.app.stop()
        cls.fake.close()

    @classmethod
    def client(cls):
        c = Client(cls.app.cfg.port)
        c.login()
        return c

    def test_flow_and_protections(self):
        c = self.client()
        body = {"instruction": "Agregá una docstring", "authorize_execution": True}
        csrf, c.csrf = c.csrf, None
        self.assertEqual(c.req("POST", f"/api/missions/{self.root}/revisions", body)[0], 403)
        c.csrf = csrf
        self.assertEqual(Client(self.app.cfg.port).req("GET", f"/api/projects/{self.root}")[0], 401)
        st, d = c.req("POST", f"/api/missions/{self.root}/revisions", body)
        self.assertEqual(st, 201, d)
        rev = d["id"]
        self.assertEqual(d["revision"], 2)
        wait_for(lambda: state(self.app, rev) in ("aprobada", "fallida"), timeout=60)
        st, p = c.req("GET", f"/api/projects/{self.root}")
        self.assertEqual(st, 200)
        self.assertEqual([(r["revision"], r["state"]) for r in p["revisions"]], [(1, "aprobada"), (2, "aprobada")])
        self.assertEqual(p["current_mission"], rev)
        self.assertTrue(any(e["kind"] == "revision" for e in p["events"]))
        st, d = c.req("POST", f"/api/projects/{self.root}/current", {"mission_id": self.root})
        self.assertEqual(st, 200)
        st, d = c.req("POST", f"/api/missions/{rev}/revisions", dict(body, base="vigente"))
        self.assertEqual(st, 201)
        self.assertEqual(self.app.missions.get(d["id"])["parent_id"], self.root, "parte de la versión vigente elegida")
        st, d = c.req("GET", f"/api/versions/diff?a={self.root}:final&b={rev}:final")
        self.assertEqual(st, 200)
        self.assertEqual(d["files"][0]["path"], "solucion.py")
        other = c.req("POST", "/api/missions", {"title": "Otra", "description": "x", "mode": "funcion", "function": "f",
                                                "cases": [{"args": [], "expect": 1}], "authorize_execution": True,
                                                "cases_reviewed": True})[1]["id"]
        self.assertEqual(c.req("GET", f"/api/versions/diff?a={self.root}:final&b={other}:final")[0], 400)
        self.assertEqual(c.req("GET", f"/api/versions/diff?a={self.root}:../x&b={rev}:final")[0], 400)
        self.assertEqual(c.req("POST", f"/api/projects/{self.root}/current", {"mission_id": other})[0], 409)

    def test_requeue_of_revision_keeps_lineage_and_base(self):
        c = self.client()
        st, d = c.req("POST", f"/api/missions/{self.root}/revisions", {"instruction": "Cambio a cancelar",
                                                                      "authorize_execution": True})
        rev = d["id"]
        self.app.missions.request_cancel(rev)
        self.app.mission_worker.cancel_current(rev)
        wait_for(lambda: state(self.app, rev) in ("cancelada", "aprobada"), timeout=30)
        if state(self.app, rev) != "cancelada":
            self.skipTest("la revisión terminó antes de poder cancelarla")
        st, d = c.req("POST", f"/api/missions/{rev}/requeue", {})
        self.assertEqual(st, 201)
        new = self.app.missions.get(d["id"])
        old = self.app.missions.get(rev)
        self.assertEqual((new["project_id"], new["revision"], new["parent_id"]), (old["project_id"], old["revision"], old["parent_id"]))
        self.assertEqual(self.app.revisions.base_files(new["id"]), self.app.revisions.base_files(rev))


if __name__ == "__main__":
    unittest.main()
