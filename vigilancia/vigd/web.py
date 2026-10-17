"""Servidor web (biblioteca estándar): dashboard, API JSON, SSE y archivos con Range."""
import contextlib
import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import auth, db, media
from .config import Paths
from .video import SegmentStore

log = logging.getLogger("web")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
STATIC = {
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "icon.svg": "image/svg+xml",
    "login.js": "text/javascript; charset=utf-8",
}
CSP = ("default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
MAX_SSE = 12
COMPONENTS = ("captura", "audio", "video", "worker")


def parse_range(header, size):
    """Devuelve (inicio, fin) inclusivo, None si no hay rango válido, o 'invalid' para 416."""
    if not header:
        return None
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header)
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    s, e = m.groups()
    if s == "":
        n = int(e)
        if n == 0:
            return "invalid"
        start, end = max(0, size - n), size - 1
    else:
        start = int(s)
        end = min(int(e), size - 1) if e else size - 1
    if start >= size or start > end:
        return "invalid"
    return start, end


def day_start(s, plus_days=0):
    t = time.strptime(s, "%Y-%m-%d")
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + plus_days, 0, 0, 0, 0, 0, -1))


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.w = cfg["web"]
        if not self.w["password_hash"]:
            raise SystemExit("Falta web.password_hash. Generalo con:  python -m vigd hash-password")
        self.paths = Paths(cfg["general"]["data_dir"])
        self.paths.ensure()
        db.init(self.paths.db).close()
        self.secret = auth.load_secret(self.paths.secret)
        self.sse = 0
        self.sse_lock = threading.Lock()
        self.fails = {}
        self.snap_lock = threading.Lock()
        vc = cfg["video"]
        self.store = SegmentStore(self.paths.segments, vc["segment_seconds"], vc["buffer_seconds"]) \
            if vc["enabled"] else None


class Handler(BaseHTTPRequestHandler):
    server_version = "vigd"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: App = None

    # ---------- utilidades ----------
    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)

    def end_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")  # no-referrer haría que el navegador envíe Origin: null
        super().end_headers()

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error_json(self, status, msg):
        self.send_json({"error": msg}, status)

    def redirect(self, location, cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def session_ok(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return False
        try:
            c = cookies.SimpleCookie(raw)
        except cookies.CookieError:
            return False
        m = c.get("vigsess")
        return bool(m) and auth.check_token(self.app.secret, m.value, self.app.w["user"])

    def token_ok(self):
        tok = self.app.w["api_token"]
        h = self.headers.get("Authorization", "")
        return bool(tok) and h.startswith("Bearer ") and hmac.compare_digest(h[7:].encode(), tok.encode())

    def same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urllib.parse.urlsplit(origin).netloc == self.headers.get("Host")

    def db(self):
        return contextlib.closing(db.connect(self.app.paths.db))

    def serve_file(self, path, mime, cache="private, max-age=3600"):
        try:
            size = os.path.getsize(path)
            f = open(path, "rb")
        except OSError:
            return self.send_error_json(404, "archivo no encontrado")
        with f:
            rng = parse_range(self.headers.get("Range"), size)
            if rng == "invalid":
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = rng if rng else (0, size - 1)
            length = end - start + 1 if size else 0
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", cache)
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            f.seek(start)
            remaining = length
            try:
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # ---------- enrutamiento ----------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path, qs = url.path, urllib.parse.parse_qs(url.query)
        if path == "/login":
            return self.serve_file(os.path.join(STATIC_DIR, "login.html"), "text/html; charset=utf-8", "no-store")
        if path == "/favicon.ico":
            return self.serve_file(os.path.join(STATIC_DIR, "icon.svg"), STATIC["icon.svg"], "max-age=86400")
        m = re.fullmatch(r"/static/([a-z.]+)", path)
        if m and m.group(1) in STATIC:
            return self.serve_file(os.path.join(STATIC_DIR, m.group(1)), STATIC[m.group(1)], "no-cache")
        authed = self.session_ok()
        if path == "/":
            if not authed:
                return self.redirect("/login")
            return self.serve_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8", "no-store")
        if not path.startswith("/api/"):
            return self.send_error_json(404, "no encontrado")
        if not (authed or self.token_ok()):
            return self.send_error_json(401, "no autenticado")
        try:
            if path == "/api/events":
                return self.api_events(qs)
            m = re.fullmatch(r"/api/events/(\d{1,12})", path)
            if m:
                with self.db() as c:
                    ev = db.get_event(c, int(m.group(1)))
                return self.send_json(ev) if ev else self.send_error_json(404, "evento inexistente")
            m = re.fullmatch(r"/api/media/(\d{1,12})", path)
            if m:
                return self.api_media(int(m.group(1)))
            if path == "/api/stats":
                return self.api_stats(qs)
            if path == "/api/status":
                return self.send_json(self.status())
            if path == "/api/stream":
                return self.api_stream()
            if path == "/api/snapshot":
                return self.api_snapshot()
        except ValueError as e:
            return self.send_error_json(400, str(e))
        return self.send_error_json(404, "no encontrado")

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
            return self.send_error_json(413, "cuerpo demasiado grande")
        body = self.rfile.read(length) if length else b""
        if not self.same_origin():
            return self.send_error_json(403, "origen no permitido")
        if path == "/login":
            return self.login(body)
        if path == "/logout":
            return self.redirect("/login", "vigsess=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        if not self.session_ok():
            return self.send_error_json(401, "no autenticado")
        if self.headers.get("X-Vig") != "1":
            return self.send_error_json(403, "falta cabecera X-Vig")
        m = re.fullmatch(r"/api/events/(\d{1,12})/(star|delete|transcribe)", path)
        if not m:
            return self.send_error_json(404, "no encontrado")
        eid, action = int(m.group(1)), m.group(2)
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return self.send_error_json(400, "JSON inválido")
        with self.db() as c:
            ev = c.execute("SELECT id, status FROM events WHERE id = ?", (eid,)).fetchone()
            if not ev:
                return self.send_error_json(404, "evento inexistente")
            if action == "star":
                db.update_event(c, eid, starred=1 if data.get("starred") else 0)
            elif action == "delete":
                if ev["status"] in ("recording", "processing"):
                    return self.send_error_json(409, "el evento todavía se está grabando o procesando")
                db.delete_event(c, self.app.paths, eid)
                log.info("Evento %d borrado desde la web", eid)
                return self.send_json({"deleted": eid})
            elif action == "transcribe":
                has_audio = c.execute("SELECT 1 FROM media WHERE event_id=? AND kind='audio'", (eid,)).fetchone()
                if not has_audio:
                    return self.send_error_json(409, "el evento no tiene audio")
                db.update_event(c, eid, transcript_status="pending")
            return self.send_json(db.get_event(c, eid))

    # ---------- acciones ----------
    def login(self, body):
        ip = self.client_address[0]
        now = time.time()
        recent = [t for t in self.app.fails.get(ip, []) if now - t < 900]
        if len(recent) >= 8:
            return self.redirect("/login?err=bloqueado")
        form = urllib.parse.parse_qs(body.decode(errors="replace"))
        user = (form.get("user") or [""])[0]
        pw = (form.get("password") or [""])[0]
        w = self.app.w
        if hmac.compare_digest(user.encode(), w["user"].encode()) and auth.verify_password(pw, w["password_hash"]):
            self.app.fails.pop(ip, None)
            tok = auth.make_token(self.app.secret, w["user"], w["session_hours"])
            cookie = (f"vigsess={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age={int(w['session_hours'] * 3600)}"
                      + ("; Secure" if w["secure_cookie"] else ""))
            log.info("Inicio de sesión desde %s", ip)
            return self.redirect("/", cookie)
        recent.append(now)
        self.app.fails[ip] = recent
        log.warning("Inicio de sesión fallido desde %s", ip)
        time.sleep(1)
        return self.redirect("/login?err=1")

    def api_events(self, qs):
        g = lambda k: (qs.get(k) or [""])[0].strip()
        date_from = day_start(g("from")) if g("from") else None
        date_to = day_start(g("to"), 1) if g("to") else None
        before = g("before")
        with self.db() as c:
            items = db.list_events(c, before_id=int(before) if before else None, limit=int(g("limit") or 30),
                                   etype=g("type"), date_from=date_from, date_to=date_to, q=g("q")[:200])
        return self.send_json({"items": items})

    def api_media(self, mid):
        with self.db() as c:
            m = c.execute("SELECT relpath, mime FROM media WHERE id = ?", (mid,)).fetchone()
        if not m:
            return self.send_error_json(404, "archivo inexistente")
        try:
            p = self.app.paths.abs(m["relpath"])
        except ValueError:
            log.error("Ruta inválida en la base: %r", m["relpath"])
            return self.send_error_json(400, "ruta inválida")
        return self.serve_file(p, m["mime"])

    def status(self):
        now = time.time()
        out = {}
        with self.db() as c:
            for r in c.execute("SELECT * FROM status"):
                age = now - r["updated_at"]
                state = r["state"]
                if state == "ok" and age > 15:
                    state = "sin datos"
                out[r["component"]] = {"state": state, "age_s": round(age, 1), "detail": json.loads(r["detail"])}
            q = dict(c.execute("SELECT transcript_status, COUNT(*) FROM events GROUP BY transcript_status").fetchall())
        for comp in COMPONENTS:
            out.setdefault(comp, {"state": "sin datos", "age_s": None, "detail": {}})
        out["web"] = {"state": "ok", "age_s": 0, "detail": {"sse_clientes": self.app.sse}}
        return {"now": now, "components": out, "transcripciones": q,
                "retencion_dias": self.app.cfg["general"]["retention_days"]}

    def api_stats(self, qs):
        days = max(1, min(int((qs.get("days") or ["7"])[0]), 60))
        since = day_start(time.strftime("%Y-%m-%d"), -(days - 1))
        with self.db() as c:
            per_day = [dict(r) for r in c.execute(
                "SELECT date(started_at,'unixepoch','localtime') AS day, COUNT(*) AS total, "
                "SUM(has_voice) AS voz, SUM(has_sound) AS sonido, SUM(has_motion) AS movimiento, "
                "SUM(impulsive) AS impulsivo FROM events WHERE started_at >= ? GROUP BY day ORDER BY day", (since,))]
            per_hour = [dict(r) for r in c.execute(
                "SELECT CAST(strftime('%H', started_at,'unixepoch','localtime') AS INTEGER) AS hour, COUNT(*) AS total "
                "FROM events WHERE started_at >= ? GROUP BY hour ORDER BY hour", (time.time() - 86400,))]
            storage = [dict(r) for r in c.execute(
                "SELECT kind, COUNT(*) AS files, COALESCE(SUM(bytes),0) AS bytes FROM media GROUP BY kind")]
            totals = dict(c.execute("SELECT COUNT(*) AS total, COALESCE(SUM(starred),0) AS favoritos, "
                                    "MIN(started_at) AS desde FROM events").fetchone())
        return self.send_json({"days": days, "per_day": per_day, "per_hour": per_hour,
                               "storage": storage, "totals": totals})

    def api_snapshot(self):
        st = self.app.store
        if st is None:
            return self.send_error_json(404, "video deshabilitado")
        out = os.path.join(self.app.paths.tmp, "snapshot-web.jpg")
        with self.app.snap_lock:
            fresh = os.path.exists(out) and time.time() - os.path.getmtime(out) < 3
            if not fresh:
                segs = st.list()
                seg = segs[-2] if len(segs) >= 2 else (segs[-1] if segs else None)
                if not seg:
                    return self.send_error_json(404, "todavía no hay video en el buffer")
                try:
                    media.last_frame(seg[1], out)
                except Exception as e:
                    return self.send_error_json(503, f"no se pudo extraer el cuadro: {e}")
        return self.serve_file(out, "image/jpeg", "no-store")

    def api_stream(self):
        with self.app.sse_lock:
            if self.app.sse >= MAX_SSE:
                return self.send_error_json(503, "demasiadas conexiones en vivo")
            self.app.sse += 1
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            last = time.time()
            last_status = 0.0
            c = db.connect(self.app.paths.db)
            try:
                while True:
                    now = time.time()
                    for ev in db.list_events(c, since_updated=last - 0.001, limit=50):
                        last = max(last, ev["updated_at"])
                        self.wfile.write(f"event: ev\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                    if now - last_status >= 3:
                        self.wfile.write(f"event: status\ndata: {json.dumps(self.status())}\n\n".encode())
                        last_status = now
                    self.wfile.flush()
                    time.sleep(1)
            finally:
                c.close()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self.app.sse_lock:
                self.app.sse -= 1


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run(cfg):
    app = App(cfg)
    Handler.app = app
    port = cfg["web"]["port"]
    binds = cfg["web"]["bind"]
    if "0.0.0.0" in binds or "::" in binds:
        log.warning("Escuchando en todas las interfaces: asegurate de que el puerto no esté expuesto a Internet")
    servers = []
    for host in binds:
        srv = Server((host, port), Handler)
        servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True, name=f"web-{host}").start()
        log.info("Dashboard en http://%s:%d/", host, port)
    import signal
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    for s in servers:
        s.shutdown()
