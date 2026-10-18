"""Taller Web: archivos múltiples, verificación en sandbox, reparación con reutilización, ZIP y vista previa."""
import io
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from constructor_ia import acceptance, config, webproject
from constructor_ia.sandbox.executor import Executor
from constructor_ia.sandbox.limiter import NullLimiter
from constructor_ia.web.preview import preview_csp, serve_preview
from constructor_ia.web.server import serve

from .helpers import bwrap_works, free_port, make_cfg, wait_for
from .test_engine import make_app
from .test_web import PASSWORD, Client

INDEX = ('<!DOCTYPE html>\n<html lang="es">\n<head>\n<meta charset="utf-8">\n'
         '<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>Tablero</title>\n'
         '<link rel="stylesheet" href="css/estilo.css">\n</head>\n<body>\n<main>\n<h1>Mensajes</h1>\n'
         '<textarea id="texto"></textarea>\n<button id="agregar" type="button">Agregar</button>\n'
         '<p>Total de palabras: <span id="total">0</span></p>\n<ul id="lista"></ul>\n</main>\n'
         '<script src="js/app.js" defer></script>\n</body>\n</html>\n')
CSS = "body { background: #101418; color: #e6e6e6; font-family: sans-serif; }\nul { padding: 0; }\n"
JS = ("const CLAVE = 'mensajes';\nconst lista = document.getElementById('lista');\n"
      "const total = document.getElementById('total');\nconst re = /\\s+/;\n"
      "function palabras(t) { return t.trim() ? t.trim().split(re).length : 0; }\n"
      "function cargar() { try { return JSON.parse(localStorage.getItem(CLAVE)) || []; } catch (e) { return []; } }\n"
      "function pintar(ms) {\n  lista.replaceChildren();\n  for (const m of ms) {\n"
      "    const li = document.createElement('li');\n    li.textContent = `${m} (${palabras(m)})`;\n"
      "    lista.append(li);\n  }\n  total.textContent = ms.reduce((a, m) => a + palabras(m), 0);\n}\n"
      "document.getElementById('agregar').addEventListener('click', () => {\n"
      "  const t = document.getElementById('texto');\n  const ms = [t.value, ...cargar()];\n"
      "  localStorage.setItem(CLAVE, JSON.stringify(ms));\n  t.value = '';\n  pintar(ms);\n});\npintar(cargar());\n")
GOOD = {"index.html": INDEX, "css/estilo.css": CSS, "js/app.js": JS}
CHECKS = [{"tipo": "selector", "valor": "textarea", "visible": True},
          {"tipo": "selector", "valor": "button", "visible": True},
          {"tipo": "selector", "valor": "ul, ol", "visible": True},
          {"tipo": "js_contiene", "valor": "localStorage", "visible": True},
          {"tipo": "texto", "valor": "palabras", "visible": False}]
SPEC = {"mode": "web", "entry": "index.html", "checks": CHECKS}


def model_answer(files, deleted=()):
    out = "Aquí está el proyecto.\n\n"
    for path, content in files.items():
        lang = path.rsplit(".", 1)[1]
        out += f"### archivo: {path}\n```{lang}\n{content}```\n\n"
    for d in deleted:
        out += f"### eliminar: {d}\n"
    return out


class WebProjectUnitTests(unittest.TestCase):
    def test_extract_multiple_files(self):
        files, deleted, problems = webproject.extract_files(model_answer(GOOD, ["viejo.js"]), 12, 100_000)
        self.assertEqual(set(files), set(GOOD))
        self.assertEqual(files["index.html"], INDEX)
        self.assertEqual(deleted, ["viejo.js"])
        self.assertEqual(problems, [])

    def test_rejects_dangerous_paths_and_types(self):
        for bad in ("../fuera.html", "./../fuera.html", "..//x.html", "/etc/passwd", "a/b/c/d.html", "Index.HTML", "x.py", "run.sh",
                    "css/../../x.css", ".htaccess", "a b.html", "~/x.js"):
            files, _, problems = webproject.extract_files(f"### archivo: {bad}\n```\nx\n```\n", 12, 100_000)
            self.assertEqual(files, {}, bad)
            if " " not in bad:  # con espacios ni siquiera se reconoce la cabecera: no se extrae nada
                self.assertTrue(problems, bad)

    def test_limits(self):
        big = {"index.html": "x" * 5000}
        files, _, problems = webproject.extract_files(model_answer(big), 12, 1000)
        self.assertEqual(files, {})
        self.assertIn("supera", problems[0])
        many = {f"f{i}.js": "x" for i in range(5)}
        files, _, problems = webproject.extract_files(model_answer(many), 3, 1000)
        self.assertEqual(len(files), 3)
        self.assertIn("demasiados", problems[0])
        _, _, problems = webproject.extract_files("### archivo: index.html\n```html\n<html>", 12, 1000)
        self.assertIn("sin cerrar", problems[0])
        self.assertIn("falta el archivo de entrada index.html",
                      webproject.check_limits({"a.js": "x"}, "index.html", 12, 1000))

    def test_merge_reuses_previous(self):
        merged = webproject.merge(GOOD, {"js/app.js": "nuevo"}, ["css/estilo.css"])
        self.assertEqual(merged["index.html"], INDEX)
        self.assertEqual(merged["js/app.js"], "nuevo")
        self.assertNotIn("css/estilo.css", merged)

    def test_zip(self):
        data = webproject.build_zip({k: v.encode() for k, v in GOOD.items()}, "M20260101-abcdef-final")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            self.assertEqual(sorted(names), sorted(f"M20260101-abcdef-final/{k}" for k in GOOD))
            self.assertEqual(zf.read("M20260101-abcdef-final/index.html").decode(), INDEX)
            for info in zf.infolist():
                self.assertEqual(info.external_attr >> 16, 0o644)
        with self.assertRaises(webproject.WebProjectError):
            webproject.build_zip({"../x.html": b"x"}, "r")

    def test_failure_report_hides_hidden_requirements(self):
        result = {"cases": [{"ok": False, "visible": False, "name": "requisito 5: texto palabras",
                             "detail": "el texto «palabras» no aparece"},
                            {"ok": False, "visible": True, "name": "referencias", "detail": "falta js/app.js"}]}
        rep = webproject.failure_report(result, [], 2000)
        self.assertIn("falta js/app.js", rep)
        self.assertNotIn("palabras", rep)

    def test_repair_prompt_includes_previous_files(self):
        msgs = webproject.repair_prompt("tablero", SPEC, GOOD, "- referencias: x", {"js/app.js"}, 20_000)
        text = msgs[1]["content"]
        self.assertIn("### archivo: js/app.js", text)
        self.assertIn("SOLO los archivos", text)
        small = webproject.repair_prompt("tablero", SPEC, GOOD, "r", {"js/app.js"}, 3500)[1]["content"]
        self.assertIn("### archivo: js/app.js", small, "los archivos con fallos tienen prioridad")


class WebValidationTests(unittest.TestCase):
    base = {"title": "Tablero", "description": "Tablero oscuro", "mode": "web", "checks": CHECKS,
            "authorize_execution": True, "cases_reviewed": True}

    def test_valid(self):
        m = acceptance.validate_mission(dict(self.base), 6, 3)
        self.assertEqual(m["spec"]["mode"], "web")
        self.assertEqual(m["spec"]["entry"], "index.html")
        self.assertEqual(len(m["spec"]["checks"]), 5)

    def test_invalid(self):
        for over in ({"checks": []}, {"checks": [{"tipo": "shell", "valor": "ls"}]},
                     {"checks": [{"tipo": "selector", "valor": "div > p; }"}]},
                     {"checks": [{"tipo": "texto", "valor": ""}]}, {"tests_py": "def test_x(sol): pass"},
                     {"authorize_execution": False}):
            with self.subTest(over=over), self.assertRaises(acceptance.SpecError):
                acceptance.validate_mission(dict(self.base, **over), 6, 3)

    def test_config(self):
        d = Path(tempfile.mkdtemp())

        def load(text):
            (d / "c.toml").write_text(text)
            return config.load(d / "c.toml", base_dir=d)

        self.assertEqual(load("").web_preview_port, 8767)
        for bad in ("[web]\npreview_port = 8766\n", "[web]\npreview_port = 1880\n",
                    '[web]\nnode = "/home/diego/.nvm/node"\n', '[web]\npreview_origin = "javascript:x"\n'):
            with self.assertRaises(config.ConfigError, msg=bad):
                load(bad)
        self.assertEqual(load('[web]\npreview_origin = "https://vivo.tail1.ts.net:8444"\n').web_preview_origin,
                         "https://vivo.tail1.ts.net:8444")

    def test_preview_csp(self):
        csp = preview_csp(["http://127.0.0.1:8766"])
        self.assertIn("sandbox allow-scripts allow-same-origin allow-forms allow-modals", csp)
        for forbidden in ("allow-top-navigation", "allow-popups", "unsafe-eval", "*;"):
            self.assertNotIn(forbidden, csp)
        self.assertIn("script-src 'self'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertIn("frame-ancestors http://127.0.0.1:8766", csp)
        self.assertIn("form-action 'none'", csp)


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class WebSandboxTests(unittest.TestCase):
    def ex(self, node="/usr/bin/node"):
        cfg = make_cfg(sandbox_timeout_s=20)
        cfg.web_node = node
        ex = Executor(cfg, limiter=NullLimiter(), allow_unverified_limits=True)
        ex.enabled = True
        return ex

    def run_web(self, files, node="/usr/bin/node", spec=SPEC):
        r = self.ex(node).run("", spec, None, files=files)
        self.assertEqual(r.status, "ok", r.reason)
        return r.result, {c["name"]: c for c in r.result["cases"]}

    def test_good_project_passes(self):
        for node in ("/usr/bin/node", "/usr/bin/no-existe"):
            with self.subTest(node=node):
                res, cases = self.run_web(GOOD, node)
                self.assertEqual(res["failed"], 0, [c for c in res["cases"] if not c["ok"]])
                self.assertEqual(res["total"], 6 + len(CHECKS))

    @unittest.skipUnless(Path("/usr/bin/node").exists(), "node no instalado en /usr/bin")
    def test_node_check_inside_sandbox(self):
        _, cases = self.run_web(GOOD)
        self.assertIn("node --check", cases["js:js/app.js"]["detail"])
        bad = dict(GOOD, **{"js/app.js": "const x = {;\n"})
        _, cases = self.run_web(bad)
        self.assertFalse(cases["js:js/app.js"]["ok"])
        self.assertIn("SyntaxError", cases["js:js/app.js"]["detail"])
        self.assertIn("js/app.js", cases["js:js/app.js"]["detail"])
        modular = {"index.html": INDEX.replace('src="js/app.js" defer', 'type="module" src="js/app.js"'),
                   "css/estilo.css": CSS, "js/app.js": "import { f } from './util.js';\nf();\n",
                   "js/util.js": "export function f() { return document.getElementById('lista'); }\n"}
        res, cases = self.run_web(modular, spec={"mode": "web", "checks": [CHECKS[0]]})
        self.assertTrue(cases["js:js/app.js"]["ok"], cases["js:js/app.js"])

    def test_heuristic_js_errors(self):
        for code in ("const x = {;\n".replace(";", ""), "let s = 'sin cerrar;\n", "f(1, 2;\n", "/* abierto\n",
                     "const t = `a ${b`;\n"):
            with self.subTest(code=code):
                _, cases = self.run_web(dict(GOOD, **{"js/app.js": code}), node="/usr/bin/no-existe")
                self.assertFalse(cases["js:js/app.js"]["ok"], code)

    def test_policy_reference_and_structure_failures(self):
        bad_html = (INDEX.replace('href="css/estilo.css"', 'href="https://cdn.example.com/x.css"')
                    .replace('<button id="agregar"', '<button onclick="alert(1)" id="agregar"')
                    .replace("</main>", '<img src="/abs.png"><img src="falta.png"><div id="lista"></div></main>')
                    .replace("<title>Tablero</title>", "")
                    .replace("<ul id=\"lista\"></ul>", "<ul id=\"lista\"><li>x</ul>")
                    + "<script>alert(1)</script>")
        bad_js = JS + "document.getElementById('no-existe');\nfetch('https://evil.example/robar');\n"
        files = {"index.html": bad_html, "css/estilo.css": CSS + "a { color: red;\n", "js/app.js": bad_js}
        res, cases = self.run_web(files)
        self.assertGreater(res["failed"], 0)
        politica = cases["politica_recursos_locales"]["detail"]
        self.assertIn("cdn.example.com", politica)
        self.assertIn("evil.example", politica)
        self.assertIn("onclick", cases["html:index.html"]["detail"])
        self.assertIn("script(s) en línea", cases["html:index.html"]["detail"])
        refs = cases["referencias"]["detail"]
        for frag in ("ruta absoluta", "falta.png", "no-existe", "id duplicado"):
            self.assertIn(frag, refs)
        self.assertIn("<title>", cases["estructura"]["detail"])
        self.assertFalse(cases["css:css/estilo.css"]["ok"])

    def test_operator_requirements(self):
        spec = {"mode": "web", "checks": [{"tipo": "selector", "valor": "input[type=email]"},
                                          {"tipo": "selector", "valor": "button#agregar"},
                                          {"tipo": "selector", "valor": "span#total"},
                                          {"tipo": "css_contiene", "valor": "grid"}]}
        _, cases = self.run_web(GOOD, spec=spec)
        oks = [cases[k]["ok"] for k in sorted(cases) if k.startswith("requisito")]
        self.assertEqual(oks, [False, True, True, False])

    def test_generated_js_is_never_executed(self):
        # Ni siquiera dentro del sandbox: node --check solo analiza la sintaxis.
        js = "require('fs').writeFileSync('/tmp/ejecutado', 'x');\nwhile (true) {}\n"
        t0 = time.monotonic()
        res, cases = self.run_web(dict(GOOD, **{"js/app.js": js}))
        self.assertTrue(cases["js:js/app.js"]["ok"])
        self.assertLess(time.monotonic() - t0, 10)


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class WebMissionEngineTests(unittest.TestCase):
    def test_repair_reuses_files_and_keeps_versions(self):
        first = dict(GOOD)
        del first["js/app.js"]  # falta el archivo referenciado
        app, fake = make_app([model_answer(first), model_answer({"js/app.js": JS})])
        self.addCleanup(fake.close)
        self.addCleanup(app.stop)
        mission = {"title": "Tablero", "description": "Tablero oscuro de mensajes", "spec": SPEC, "tests_py": "",
                   "max_attempts": 3}
        mid = app.missions.create(mission, "qwen3:4b-instruct", "diego")
        app.mission_worker.notify()
        wait_for(lambda: app.missions.get(mid)["state"] in ("aprobada", "fallida"), timeout=60)
        m = app.missions.get(mid)
        self.assertEqual(m["state"], "aprobada", m["error"])
        self.assertEqual(m["final_attempt"], 2)
        paths = {r[0] for r in app.db.all("SELECT rel_path FROM files WHERE mission_id=?", (mid,))}
        self.assertTrue({"intento-1/web/index.html", "intento-1/web/css/estilo.css"} <= paths)
        self.assertNotIn("intento-1/web/js/app.js", paths)
        self.assertTrue({f"intento-2/web/{p}" for p in GOOD} <= paths, "el intento 2 reutiliza index y css")
        self.assertTrue({f"final/web/{p}" for p in GOOD} <= paths)
        req2 = fake.requests[1]
        self.assertEqual(req2["options"]["num_ctx"], app.cfg.web_model.num_ctx)
        prompt2 = req2["messages"][1]["content"]
        self.assertIn("### archivo: index.html", prompt2)
        self.assertIn("js/app.js", prompt2)
        self.assertIn("inexistente", prompt2)
        events = [r[0] for r in app.db.all("SELECT message FROM events WHERE mission_id=?", (mid,))]
        self.assertTrue(any("2 reutilizados" in e for e in events), events)

    def test_bad_paths_never_written(self):
        answer = ("### archivo: ../../../home/diego/.bashrc\n```\necho pwned\n```\n" + model_answer(GOOD))
        app, fake = make_app([answer, model_answer(GOOD)])
        self.addCleanup(fake.close)
        self.addCleanup(app.stop)
        mid = app.missions.create({"title": "T", "description": "d", "spec": SPEC, "tests_py": "", "max_attempts": 2},
                                  "m", "diego")
        app.mission_worker.notify()
        wait_for(lambda: app.missions.get(mid)["state"] in ("aprobada", "fallida"), timeout=60)
        paths = [r[0] for r in app.db.all("SELECT rel_path FROM files WHERE mission_id=?", (mid,))]
        self.assertFalse([p for p in paths if "bashrc" in p or ".." in p])
        self.assertFalse(list(Path(app.cfg.data_dir).rglob("*bashrc*")))


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class PreviewServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.fake = make_app([model_answer(GOOD)] * 3, start=False)
        cls.app.cfg.port = free_port()
        cls.app.cfg.web_preview_port = free_port()
        cls.app.auth.create_user("diego", PASSWORD)
        cls.app.start()
        cls.servers = serve(cls.app) + serve_preview(cls.app)
        cls.app.preview_available = True
        cls.port, cls.pport = cls.app.cfg.port, cls.app.cfg.web_preview_port
        c = cls.client()
        st, d = c.req("POST", "/api/missions", {"title": "Tablero", "description": "Tablero oscuro", "mode": "web",
                                                "checks": CHECKS, "authorize_execution": True, "cases_reviewed": True})
        assert st == 201, d
        cls.mid = d["id"]
        wait_for(lambda: c.req("GET", f"/api/missions/{cls.mid}")[1]["mission"]["state"] in ("aprobada", "fallida"),
                 timeout=60)

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
        c = Client(cls.port)
        c.login()
        return c

    def preview_get(self, path, host=None):
        c = Client(self.pport, host=host or f"127.0.0.1:{self.pport}")
        c.cookie = "cia_session=lo-que-sea"  # el navegador envía la cookie a otro puerto del mismo host
        return c.req("GET", path, raw=True)

    def test_mission_approved(self):
        st, d = self.client().req("GET", f"/api/missions/{self.mid}")
        self.assertEqual(d["mission"]["state"], "aprobada", d["mission"]["error"])

    def test_preview_flow_and_headers(self):
        c = self.client()
        csrf = c.csrf
        c.csrf = None
        self.assertEqual(c.req("POST", f"/api/missions/{self.mid}/preview", {})[0], 403)
        c.csrf = csrf
        st, p = c.req("POST", f"/api/missions/{self.mid}/preview", {"attempt": "final"})
        self.assertEqual(st, 201, p)
        self.assertEqual(p["port"], self.pport)
        r, body = self.preview_get(p["path"])
        self.assertEqual(r.status, 200)
        self.assertEqual(body.decode(), INDEX)
        csp = r.headers["Content-Security-Policy"]
        self.assertIn("sandbox", csp)
        self.assertIn(f"frame-ancestors http://127.0.0.1:{self.port}", csp)
        self.assertIsNone(r.headers.get("Set-Cookie"))
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")
        r, body = self.preview_get(p["path"].replace("index.html", "js/app.js"))
        self.assertEqual(r.status, 200)
        self.assertIn("javascript", r.headers["Content-Type"])
        base = p["path"].rsplit("/", 1)[0]
        for bad in (base + "/../../api/me", base + "/%2e%2e/x", base + "/no-existe.html", "/p/" + "A" * 43 + "/index.html",
                    "/api/me", "/", base + "/respuesta_modelo.md", base + "/../resultado.json"):
            self.assertEqual(self.preview_get(bad)[0].status, 404, bad)
        self.assertEqual(self.preview_get(p["path"], host="evil.example")[0].status, 421)
        st, _ = Client(self.pport).req("POST", p["path"], {})
        self.assertEqual(st, 405)

    def test_panel_frames_only_preview_origin_and_blocks_same_site(self):
        c = self.client()
        r, _ = c.req("GET", "/", raw=True)
        self.assertIn(f"frame-src http://127.0.0.1:{self.pport}", r.headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        # Desde la vista previa (otro puerto = mismo sitio) la cookie llegaría: la API la rechaza.
        for path in ("/api/me", f"/api/missions/{self.mid}", "/api/stream"):
            st, d = c.req("GET", path, headers={"Sec-Fetch-Site": "same-site"})
            self.assertEqual(st, 403, path)
        st, _ = c.req("POST", "/api/conversations", {}, headers={"Sec-Fetch-Site": "same-site"})
        self.assertEqual(st, 403)
        self.assertEqual(c.req("GET", "/api/me", headers={"Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_zip(self):
        c = self.client()
        r, data = c.req("GET", f"/api/missions/{self.mid}/zip?attempt=final", raw=True)
        self.assertEqual(r.status, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertEqual(sorted(zf.namelist()), sorted(f"{self.mid}-final/{p}" for p in GOOD))
        r, data = c.req("GET", f"/api/missions/{self.mid}/zip?attempt=1", raw=True)
        self.assertEqual(r.status, 200)
        self.assertEqual(c.req("GET", f"/api/missions/{self.mid}/zip?attempt=9")[0], 404)
        self.assertEqual(c.req("GET", f"/api/missions/{self.mid}/zip?attempt=../x")[0], 400)
        self.assertEqual(Client(self.port).req("GET", f"/api/missions/{self.mid}/zip")[0], 401)

    def test_preview_disabled_falls_back_to_zip(self):
        c = self.client()
        self.app.preview_available = False
        try:
            st, d = c.req("POST", f"/api/missions/{self.mid}/preview", {})
            self.assertEqual(st, 409)
            self.assertIn("ZIP", d["error"])
            r, _ = c.req("GET", "/", raw=True)
            self.assertNotIn("frame-src", r.headers["Content-Security-Policy"])
        finally:
            self.app.preview_available = True

    def test_expired_token(self):
        token = self.app.previews.create(self.mid, "final/web/")
        self.assertEqual(self.preview_get(f"/p/{token}/index.html")[0].status, 200)
        mid, prefix = self.app.previews.resolve(token)
        self.app.previews._tokens[token] = (mid, prefix, time.time() - 1)
        self.assertEqual(self.preview_get(f"/p/{token}/index.html")[0].status, 404)

    def test_non_web_mission_has_no_zip(self):
        c = self.client()
        st, d = c.req("POST", "/api/missions", {"title": "S", "description": "s", "mode": "funcion",
                                                "function": "f", "cases": [{"args": [], "expect": 1}],
                                                "authorize_execution": True, "cases_reviewed": True})
        self.assertEqual(c.req("GET", f"/api/missions/{d['id']}/zip")[0], 409)


if __name__ == "__main__":
    unittest.main()
