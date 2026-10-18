"""Pruebas HTTP de extremo a extremo contra el servidor real (puerto efímero)."""
import http.client
import json
import socket
import threading
import time
import unittest

from constructor_ia import logsetup
from constructor_ia.web.server import serve

from .helpers import bwrap_works, fn_code, free_port, wait_for
from .test_engine import make_app

PASSWORD = "clave-de-prueba-123"


class Client:
    def __init__(self, port, host=None):
        self.port = port
        self.host = host or f"127.0.0.1:{port}"
        self.cookie = None
        self.csrf = None

    def req(self, method, path, body=None, headers=None, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Host": self.host}
        if body is not None:
            h["Content-Type"] = "application/json"
        if self.cookie:
            h["Cookie"] = self.cookie
        if self.csrf and method == "POST":
            h["X-CSRF-Token"] = self.csrf
        h.update(headers or {})
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        if "Set-Cookie" in r.headers:
            self.cookie = r.headers["Set-Cookie"].split(";")[0]
        if raw:
            return r, data
        try:
            return r.status, json.loads(data) if data else {}
        except ValueError:
            return r.status, data

    def login(self, user="diego", pw=PASSWORD):
        st, d = self.req("POST", "/api/login", {"username": user, "password": pw})
        if st == 200:
            self.csrf = d["csrf"]
        return st, d


@unittest.skipUnless(bwrap_works(), "bubblewrap no disponible")
class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.fake = make_app([fn_code("return a + b")] * 5, start=False)
        cls.app.cfg.port = free_port()
        logsetup.setup(cls.app.cfg.log_dir, 1_000_000, 2)
        cls.app.auth.create_user("diego", PASSWORD)
        cls.app.start()
        cls.servers = serve(cls.app)
        cls.port = cls.app.cfg.port
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        for s in cls.servers:
            s.stopping.set()
            s.shutdown()
            s.server_close()
        cls.app.stop()
        cls.fake.close()

    def client(self):
        c = Client(self.port)
        self.assertEqual(c.login()[0], 200)
        return c

    def new_mission(self, c, **over):
        body = {"title": "Sumar", "description": "Sumar a y b", "mode": "funcion", "function": "sumar",
                "cases": [{"args": [1, 2], "expect": 3}, {"args": [2, 2], "expect": 4, "visible": False}],
                "authorize_execution": True, "cases_reviewed": True}
        body.update(over)
        return c.req("POST", "/api/missions", body)

    def test_static_and_security_headers(self):
        r, data = Client(self.port).req("GET", "/", raw=True)
        self.assertEqual(r.status, 200)
        self.assertIn(b"Constructor IA", data)
        self.assertIn("script-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(Client(self.port).req("GET", "/static/app.js", raw=True)[0].status, 200)
        for bad in ("/static/../constructor_ia/config.py", "/static/..%2fconfig.py", "/static/x.py", "/etc/passwd"):
            self.assertIn(Client(self.port).req("GET", bad, raw=True)[0].status, (404,))

    def test_unauthenticated_denied(self):
        anon = Client(self.port)
        for path in ("/api/missions", "/api/status", "/api/files/1", "/api/files/1/download", "/api/stream",
                     "/api/conversations", "/api/approvals", "/api/me", "/api/missions/M20260101-abcdef"):
            self.assertEqual(anon.req("GET", path)[0], 401, path)
        self.assertEqual(self.new_mission(anon)[0], 401)

    def test_login_failures_and_rate_limit(self):
        c = Client(self.port)
        self.assertEqual(c.login(pw="incorrecta-xx")[0], 401)
        self.assertEqual(c.login(user="nadie")[0], 401)
        st, _ = c.req("POST", "/api/login", {"username": "diego", "password": PASSWORD},
                      headers={"Origin": "https://evil.example"})
        self.assertEqual(st, 403)
        self.app.auth.limiter.success("127.0.0.1")

    def test_host_header_rebinding_blocked(self):
        c = Client(self.port, host="evil.example.com")
        self.assertEqual(c.req("GET", "/api/me")[0], 421)
        self.assertEqual(Client(self.port, host=f"vivobook.tail1234.ts.net").req("GET", "/api/me")[0], 401)

    def test_csrf_and_origin(self):
        c = self.client()
        token = c.csrf
        c.csrf = None
        self.assertEqual(self.new_mission(c)[0], 403)
        c.csrf = "falso"
        self.assertEqual(self.new_mission(c)[0], 403)
        c.csrf = token
        st, _ = c.req("POST", "/api/conversations", {}, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        st, _ = c.req("POST", "/api/conversations", None, headers={"Content-Type": "text/plain"})
        self.assertEqual(st, 415)

    def test_mission_flow_files_and_download(self):
        c = self.client()
        st, d = self.new_mission(c)
        self.assertEqual(st, 201, d)
        mid = d["id"]
        wait_for(lambda: c.req("GET", f"/api/missions/{mid}")[1]["mission"]["state"] in ("aprobada", "fallida"))
        st, d = c.req("GET", f"/api/missions/{mid}")
        self.assertEqual(d["mission"]["state"], "aprobada")
        self.assertEqual(d["attempts"][0]["passed"], 2)
        final = next(f for f in d["files"] if f["rel_path"] == "final/solucion.py")
        st, f = c.req("GET", f"/api/files/{final['id']}")
        self.assertIn("return a + b", f["content"])
        r, data = c.req("GET", f"/api/files/{final['id']}/download", raw=True)
        self.assertEqual(r.status, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        self.assertIn(b"return a + b", data)
        self.assertEqual(c.req("GET", "/api/files/999999")[0], 404)
        st, ev = c.req("GET", f"/api/missions/{mid}/events")
        self.assertTrue(any(e["kind"] == "pruebas_aprobadas" for e in ev["items"]))
        # Aprobación humana: no ejecuta nada, solo genera un paquete.
        st, a = c.req("POST", f"/api/missions/{mid}/approvals", {"kind": "instalacion", "detail": "Usar en script X"})
        self.assertEqual(st, 201)
        self.assertEqual(c.req("POST", f"/api/approvals/{a['id']}/decide", {"decision": "aprobar"})[0], 400)
        st, res = c.req("POST", f"/api/approvals/{a['id']}/decide", {"decision": "aprobar", "confirm": True})
        self.assertEqual(st, 200)
        self.assertIn("NO ejecuta", res["nota"])
        self.assertEqual(c.req("POST", f"/api/approvals/{a['id']}/decide", {"decision": "rechazar", "confirm": True})[0], 409)
        # Tampering: si el archivo cambia en disco, se rechaza la lectura.
        p = self.app.files.mission_dir(mid) / "final" / "solucion.py"
        p.write_text("import os\nos.system('x')\n")
        self.assertEqual(c.req("GET", f"/api/files/{final['id']}")[0], 404)

    def test_invalid_inputs(self):
        c = self.client()
        self.assertEqual(self.new_mission(c, authorize_execution=False)[0], 400)
        self.assertEqual(self.new_mission(c, function="os.system")[0], 400)
        self.assertEqual(c.req("GET", "/api/missions/../../etc")[0], 404)
        self.assertEqual(c.req("GET", "/api/missions?state=x'or'1")[0], 400)
        self.assertEqual(c.req("POST", "/api/missions", None, headers={"Content-Type": "application/json",
                                                                        "Content-Length": "999999999"})[0], 413)

    def test_cancel_and_conversation(self):
        c = self.client()
        st, d = c.req("POST", "/api/conversations", {"title": "prueba"})
        cid = d["id"]
        self.assertEqual(c.req("POST", f"/api/conversations/{cid}/messages", {"content": "hola"})[0], 201)
        st, conv = c.req("GET", f"/api/conversations/{cid}")
        self.assertEqual(conv["messages"][0]["role"], "user")
        self.assertEqual(c.req("POST", "/api/missions/M20990101-abcdef/cancel", {})[0], 404)

    def test_sse_stream(self):
        c = self.client()
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        sock.sendall(f"GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nCookie: {c.cookie}\r\n\r\n".encode())
        threading.Timer(0.5, lambda: self.app.bus.publish("prueba", "evento SSE de prueba")).start()
        buf = b""
        deadline = time.monotonic() + 10
        while b"evento SSE de prueba" not in buf and time.monotonic() < deadline:
            buf += sock.recv(4096)
        sock.close()
        self.assertIn(b"text/event-stream", buf)
        self.assertIn(b"evento SSE de prueba", buf)

    def test_status_has_no_secrets(self):
        c = self.client()
        st, s = c.req("GET", "/api/status")
        self.assertEqual(st, 200)
        self.assertTrue(s["ollama"]["available"])
        self.assertIn("sandbox", s)
        dump = json.dumps(s)
        for secret in (PASSWORD, c.csrf, c.cookie.split("=", 1)[1]):
            self.assertNotIn(secret, dump)

    def test_logout_invalidates(self):
        c = self.client()
        cookie = c.cookie
        self.assertEqual(c.req("POST", "/api/logout", {})[0], 200)
        c.cookie = cookie
        self.assertEqual(c.req("GET", "/api/me")[0], 401)

    def test_zz_logs_have_no_secrets(self):
        c = self.client()
        c.req("POST", "/api/login", {"username": "diego", "password": PASSWORD})
        logs = "".join(p.read_text() for p in self.app.cfg.log_dir.glob("*.log*"))
        db_audit = json.dumps([dict(r) for r in self.app.db.all("SELECT * FROM audit")])
        for secret in (PASSWORD, c.csrf, c.cookie.split("=", 1)[1]):
            self.assertNotIn(secret, logs)
            self.assertNotIn(secret, db_audit)


if __name__ == "__main__":
    unittest.main()
