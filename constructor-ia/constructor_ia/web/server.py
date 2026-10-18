"""Servidor HTTP (biblioteca estándar) con API JSON, SSE y archivos estáticos locales.

Controles: autenticación obligatoria, cookie HttpOnly/SameSite=Strict, token
CSRF en cabecera para toda operación de cambio, verificación de Origin y Host
(contra DNS rebinding), límites de tamaño, de conexiones y de clientes SSE,
cabeceras de seguridad estrictas (CSP sin scripts en línea).
"""
from __future__ import annotations

import difflib
import hmac
import http.cookies
import ipaddress
import json
import logging
import re
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import VERSION
from ..acceptance import SpecError, validate_mission
from ..app import App
from ..auth import Session
from ..events import event_dict
from .. import webproject
from ..files import MISSION_ID_RE, FileStoreError

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
MAX_BODY = 256 * 1024
COOKIE = "cia_session"
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json",
                ".png": "image/png", ".ico": "image/x-icon"}
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "connect-src 'self'; manifest-src 'self'; font-src 'self'; base-uri 'none'; "
                                "form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def host_name(header: str | None) -> str:
    host = (header or "").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else ""
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def host_allowed(allowed_hosts: list[str], header: str | None) -> bool:
    host = host_name(header)
    if not host:
        return False
    for allowed in allowed_hosts:
        if allowed.startswith(".") and host.endswith(allowed) and len(host) > len(allowed):
            return True
        if host == allowed:
            return True
    return False


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


ROUTES: list[tuple[str, re.Pattern, str, bool]] = []


def route(method: str, pattern: str, auth: bool = True):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn.__name__, auth))
        return fn
    return deco


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, addr, handler, app: App):
        self.app = app
        ip = ipaddress.ip_address(addr[0])
        self.address_family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        self._freebind = not ip.is_loopback
        self.conn_sem = threading.BoundedSemaphore(app.cfg.max_connections)
        self.sse_sem = threading.BoundedSemaphore(app.cfg.max_sse_clients)
        self.stopping = threading.Event()
        super().__init__(addr, handler)

    def server_bind(self):
        if self._freebind:
            # Permite enlazar la IP de Tailscale aunque tailscale0 aún no esté listo al arrancar.
            self.socket.setsockopt(socket.SOL_IP, 15, 1)  # IP_FREEBIND
        super().server_bind()

    def process_request(self, request, client_address):
        if not self.conn_sem.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.conn_sem.release()
            raise

    def handle_error(self, request, client_address):
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            log.debug("Conexión cerrada por el cliente: %s", type(exc).__name__)
        else:
            log.exception("Error en conexión HTTP")

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.app.db.close()
            self.conn_sem.release()


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = "ConstructorIA"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30

    # Utilidades --------------------------------------------------------
    @property
    def app(self) -> App:
        return self.server.app

    def log_message(self, fmt, *args):  # sin query strings ni cookies en el registro
        log.debug("%s %s", self.client_address[0], fmt % args)

    def _ip(self) -> str:
        return self.client_address[0]

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None, cache: str = "no-store"):
        self.send_response(status)
        for k, v in SECURITY_HEADERS.items():
            if k == "Content-Security-Policy":
                v += self._frame_src()
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _frame_src(self) -> str:
        """Permite enmarcar SOLO el origen separado de la vista previa (otro puerto)."""
        cfg = self.app.cfg
        if not (cfg.web_enabled and cfg.web_preview_enabled and self.app.preview_available):
            return ""
        origins = []
        if cfg.web_preview_origin:
            origins.append(cfg.web_preview_origin)
        name = host_name(self.headers.get("Host"))
        if name and re.match(r"^[a-z0-9.-]+$|^[0-9a-f:]+$", name):
            origins.append(f"http://{'[' + name + ']' if ':' in name else name}:{cfg.web_preview_port}")
        return f"; frame-src {' '.join(origins)}" if origins else ""

    def _json(self, status: int, obj, extra: dict | None = None):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def _host_ok(self) -> bool:
        return host_allowed(self.app.cfg.allowed_hosts, self.headers.get("Host"))

    def _session(self) -> tuple[Session | None, str | None]:
        raw = self.headers.get("Cookie")
        if not raw:
            return None, None
        try:
            c = http.cookies.SimpleCookie()
            c.load(raw)
        except http.cookies.CookieError:
            return None, None
        token = c[COOKIE].value if COOKIE in c else None
        return self.app.auth.get_session(token), token

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin:
            return urlsplit(origin).netloc.lower() == (self.headers.get("Host") or "").lower()
        site = self.headers.get("Sec-Fetch-Site")
        return site in (None, "same-origin", "none")

    def _body(self) -> dict:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HttpError(415, "Se requiere application/json")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HttpError(400, "Content-Length inválido") from None
        if length > MAX_BODY:
            raise HttpError(413, "Petición demasiado grande")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw)
        except ValueError:
            raise HttpError(400, "JSON inválido") from None
        if not isinstance(data, dict):
            raise HttpError(400, "Se esperaba un objeto JSON")
        return data

    def _query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    def _cookie_header(self, token: str, max_age: int) -> str:
        parts = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={max_age}"]
        if self.app.cfg.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    # Despacho ----------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        path = urlsplit(self.path).path
        if path == "/" or path.startswith("/static/"):
            self._dispatch("GET")
        else:
            self._json(405, {"error": "Método no permitido"})

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._json(405, {"error": "Método no permitido"})

    do_DELETE = do_PATCH = do_PUT

    def _dispatch(self, method: str):
        try:
            if not self._host_ok():
                self._json(421, {"error": "Host no permitido. Agregue el nombre a server.allowed_hosts."})
                return
            path = urlsplit(self.path).path
            if path.startswith("/api/") and self.headers.get("Sec-Fetch-Site") in ("same-site", "cross-site"):
                # Otro puerto del mismo equipo (vista previa, Node-RED, Vigilanti…) es "same-site" y
                # recibiría la cookie: la API solo atiende peticiones del propio origen.
                self._json(403, {"error": "Petición desde otro origen bloqueada"})
                return
            if method == "GET" and (path == "/" or path.startswith("/static/") or path in ("/favicon.ico",)):
                self._static(path)
                return
            for m, pat, name, needs_auth in ROUTES:
                match = pat.match(path)
                if match and m == method:
                    break
            else:
                raise HttpError(404, "No encontrado")
            session = None
            if needs_auth:
                session, _ = self._session()
                if session is None:
                    raise HttpError(401, "Sesión requerida")
            if method == "POST":
                if not self._origin_ok():
                    raise HttpError(403, "Origen no permitido")
                if needs_auth:
                    sent = self.headers.get("X-CSRF-Token") or ""
                    if not hmac.compare_digest(sent.encode(), session.csrf.encode()):
                        raise HttpError(403, "Token CSRF inválido")
            getattr(self, name)(session, *match.groups())
        except HttpError as e:
            self._json(e.status, {"error": e.message, **e.extra})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("Error no controlado en %s %s", method, urlsplit(self.path).path)
            try:
                self._json(500, {"error": "Error interno"})
            except OSError:
                pass

    def _static(self, path: str):
        name = "index.html" if path == "/" else path.rsplit("/", 1)[-1]
        if path == "/favicon.ico":
            name = "favicon.svg"
        if not re.match(r"^[a-z0-9_-]{1,40}\.[a-z]{2,4}$", name):
            raise HttpError(404, "No encontrado")
        p = STATIC_DIR / name
        if p.suffix not in STATIC_TYPES or not p.is_file() or p.is_symlink():
            raise HttpError(404, "No encontrado")
        self._send(200, p.read_bytes(), STATIC_TYPES[p.suffix], cache="no-cache")

    @route("GET", "/api/health", auth=False)
    def api_health(self, _s):
        # Sin información interna: solo indica que el proceso responde.
        self._json(200, {"ok": True})

    # Autenticación -----------------------------------------------------
    @route("POST", "/api/login", auth=False)
    def api_login(self, _s):
        if not self._origin_ok():
            raise HttpError(403, "Origen no permitido")
        data = self._body()
        ip = self._ip()
        wait = self.app.auth.limiter.check(ip)
        if wait > 0:
            raise HttpError(429, f"Demasiados intentos. Espere {int(wait) + 1} s.", retry_after=int(wait) + 1)
        username = str(data.get("username", ""))[:64].strip().lower()
        password = str(data.get("password", ""))[:256]
        uid = self.app.auth.authenticate(username, password)
        if uid is None:
            self.app.auth.limiter.failure(ip)
            self.app.db.audit("login_fallido", username=username[:32], ip=ip)
            raise HttpError(401, "Usuario o contraseña incorrectos")
        self.app.auth.limiter.success(ip)
        token, csrf = self.app.auth.create_session(uid, ip, self.headers.get("User-Agent", ""))
        self.app.db.audit("login", username=username, ip=ip)
        self._json(200, {"username": username, "csrf": csrf},
                   {"Set-Cookie": self._cookie_header(token, self.app.auth.max_age)})

    @route("POST", "/api/logout")
    def api_logout(self, s: Session):
        self.app.auth.destroy_session(s.id)
        self.app.db.audit("logout", username=s.username, ip=self._ip())
        self._json(200, {"ok": True}, {"Set-Cookie": self._cookie_header("", 0)})

    @route("GET", "/api/me")
    def api_me(self, s: Session):
        cfg = self.app.cfg
        self._json(200, {"username": s.username, "csrf": s.csrf, "version": VERSION,
                         "web": {"enabled": cfg.web_enabled,
                                 "preview": cfg.web_preview_enabled and self.app.preview_available,
                                 "preview_port": cfg.web_preview_port, "preview_origin": cfg.web_preview_origin or None}})

    # Estado ------------------------------------------------------------
    @route("GET", "/api/status")
    def api_status(self, s):
        self._json(200, self.app.status())

    @route("POST", "/api/selftest")
    def api_selftest(self, s: Session):
        report = self.app.executor.self_test()
        self.app.db.kv_set("sandbox_selftest", report)
        self.app.db.audit("autodiagnostico", username=s.username, ip=self._ip(), detail=str(report["ok"]))
        self.app._status_cache = (0.0, {})
        self._json(200, {"ok": report["ok"], "checks": report["checks"]})

    @route("GET", "/api/activity")
    def api_activity(self, s):
        rows = self.app.db.all("SELECT ts, username, action, target, ip, detail FROM audit ORDER BY id DESC LIMIT 100")
        self._json(200, {"items": [dict(r) for r in rows]})

    # Misiones ----------------------------------------------------------
    def _mission(self, mid: str):
        if not MISSION_ID_RE.match(mid):
            raise HttpError(404, "Misión inexistente")
        row = self.app.missions.get(mid)
        if row is None:
            raise HttpError(404, "Misión inexistente")
        return row

    @route("GET", "/api/missions")
    def api_missions(self, s):
        q = self._query()
        try:
            limit = max(1, min(int(q.get("limit", 50)), 200))
            offset = max(0, int(q.get("offset", 0)))
        except ValueError:
            raise HttpError(400, "Parámetros inválidos") from None
        state = q.get("state")
        sql = ("SELECT id, title, state, attempt, max_attempts, model, created_at, started_at, finished_at,"
               " updated_at, error FROM missions")
        args: list = []
        if state:
            if not re.match(r"^[a-z]{3,20}$", state):
                raise HttpError(400, "Estado inválido")
            sql += " WHERE state=?"
            args.append(state)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        rows = self.app.db.all(sql, (*args, limit, offset))
        self._json(200, {"items": [dict(r) for r in rows], "now": time.time()})

    @route("POST", "/api/missions")
    def api_create_mission(self, s: Session):
        data = self._body()
        try:
            m = validate_mission(data, self.app.cfg.max_attempts_limit, self.app.cfg.max_attempts)
        except SpecError as e:
            raise HttpError(400, str(e)) from None
        conv_id = data.get("conversation_id")
        if conv_id is not None:
            if not isinstance(conv_id, int) or not self.app.db.one("SELECT 1 FROM conversations WHERE id=?", (conv_id,)):
                raise HttpError(400, "Conversación inexistente")
        pending = self.app.db.one("SELECT COUNT(*) FROM missions WHERE state='pendiente'")[0]
        if pending >= 20:
            raise HttpError(429, "La cola está llena (20 misiones pendientes)")
        mid = self.app.missions.create(m, self.app.cfg.builder.name, s.username, conv_id)
        self.app.db.audit("mision_creada", username=s.username, target=mid, ip=self._ip(),
                          detail="ejecución aislada autorizada")
        self.app.mission_worker.notify()
        self._json(201, {"id": mid})

    @route("GET", r"/api/missions/([A-Za-z0-9-]{1,30})")
    def api_mission(self, s, mid):
        row = self._mission(mid)
        m = dict(row)
        m["spec"] = json.loads(m.pop("spec_json"))
        attempts = []
        for a in self.app.db.all("SELECT * FROM attempts WHERE mission_id=? ORDER BY n", (mid,)):
            a = dict(a)
            a["result"] = json.loads(a.pop("result_json")) if a.get("result_json") else None
            a["sandbox"] = json.loads(a.pop("sandbox_json")) if a.get("sandbox_json") else None
            attempts.append(a)
        files = [dict(r) for r in self.app.db.all(
            "SELECT id, attempt_n, rel_path, kind, size, sha256, created_at FROM files WHERE mission_id=? ORDER BY rel_path",
            (mid,))]
        approvals = [dict(r) for r in self.app.db.all("SELECT * FROM approvals WHERE mission_id=? ORDER BY id", (mid,))]
        self._json(200, {"mission": m, "attempts": attempts, "files": files, "approvals": approvals,
                         "now": time.time()})

    @route("GET", r"/api/missions/([A-Za-z0-9-]{1,30})/events")
    def api_mission_events(self, s, mid):
        self._mission(mid)
        rows = self.app.db.all("SELECT * FROM events WHERE mission_id=? ORDER BY id DESC LIMIT 500", (mid,))
        self._json(200, {"items": [event_dict(r) for r in reversed(rows)]})

    @route("POST", r"/api/missions/([A-Za-z0-9-]{1,30})/cancel")
    def api_cancel(self, s: Session, mid):
        self._mission(mid)
        result = self.app.missions.request_cancel(mid)
        if result == "solicitada":
            self.app.mission_worker.cancel_current(mid)
        elif result == "no_aplica":
            raise HttpError(409, "La misión ya terminó")
        self.app.db.audit("mision_cancelada", username=s.username, target=mid, ip=self._ip(), detail=result)
        self._json(200, {"result": result})

    @route("POST", r"/api/missions/([A-Za-z0-9-]{1,30})/requeue")
    def api_requeue(self, s: Session, mid):
        self._mission(mid)
        try:
            new = self.app.missions.requeue(mid, s.username)
        except ValueError as e:
            raise HttpError(409, str(e)) from None
        self.app.db.audit("mision_reencolada", username=s.username, target=mid, ip=self._ip(), detail=new)
        self.app.mission_worker.notify()
        self._json(201, {"id": new})

    @route("GET", r"/api/missions/([A-Za-z0-9-]{1,30})/diff")
    def api_diff(self, s, mid):
        self._mission(mid)
        q = self._query()
        try:
            a, b = int(q["a"]), int(q["b"])
        except (KeyError, ValueError):
            raise HttpError(400, "Parámetros a y b requeridos") from None
        if not (1 <= a <= 20 and 1 <= b <= 20):
            raise HttpError(400, "Intentos inválidos")
        try:
            ca = self.app.files.read_rel(mid, f"intento-{a}/solucion.py")
            cb = self.app.files.read_rel(mid, f"intento-{b}/solucion.py")
        except FileStoreError as e:
            raise HttpError(409, str(e)) from None
        if ca is None or cb is None:
            raise HttpError(404, "Alguna de las versiones no tiene código")
        diff = list(difflib.unified_diff(ca.splitlines(), cb.splitlines(), f"intento-{a}/solucion.py",
                                         f"intento-{b}/solucion.py", lineterm=""))
        self._json(200, {"diff": diff[:5000]})

    # Archivos ----------------------------------------------------------
    def _file(self, fid: str):
        try:
            return self.app.files.read(int(fid))
        except FileStoreError as e:
            raise HttpError(404, str(e)) from None

    @route("GET", r"/api/files/(\d{1,9})")
    def api_file(self, s, fid):
        info, data = self._file(fid)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        self._json(200, {"file": info, "content": text})

    @route("GET", r"/api/files/(\d{1,9})/download")
    def api_download(self, s: Session, fid):
        info, data = self._file(fid)
        name = info["mission_id"] + "_" + re.sub(r"[^A-Za-z0-9_.-]", "_", info["rel_path"].replace("/", "_"))
        self.app.db.audit("descarga", username=s.username, target=f"{info['mission_id']}/{info['rel_path']}",
                          ip=self._ip())
        self._send(200, data, "application/octet-stream",
                   {"Content-Disposition": f'attachment; filename="{name}"'})

    # Aprobaciones ------------------------------------------------------
    @route("GET", "/api/approvals")
    def api_approvals(self, s):
        rows = self.app.db.all("SELECT a.*, m.title FROM approvals a JOIN missions m ON m.id=a.mission_id"
                               " ORDER BY a.id DESC LIMIT 200")
        self._json(200, {"items": [dict(r) for r in rows]})

    @route("POST", r"/api/missions/([A-Za-z0-9-]{1,30})/approvals")
    def api_request_approval(self, s: Session, mid):
        row = self._mission(mid)
        if row["state"] != "aprobada":
            raise HttpError(409, "Solo se puede solicitar instalación de misiones aprobadas por las pruebas")
        data = self._body()
        kind = data.get("kind")
        if kind not in ("instalacion", "cambio"):
            raise HttpError(400, "Tipo inválido")
        detail = str(data.get("detail", "")).strip()[:2000]
        if len(detail) < 5:
            raise HttpError(400, "Describa dónde y para qué se usaría")
        cur = self.app.db.execute(
            "INSERT INTO approvals(mission_id, kind, detail, state, requested_by, requested_at) VALUES(?,?,?,?,?,?)",
            (mid, kind, detail, "pendiente", s.username, time.time()))
        self.app.db.audit("aprobacion_solicitada", username=s.username, target=mid, ip=self._ip())
        self.app.bus.publish("aprobacion", "Solicitud de aprobación creada", mission_id=mid)
        self._json(201, {"id": cur.lastrowid})

    @route("POST", r"/api/approvals/(\d{1,9})/decide")
    def api_decide(self, s: Session, aid):
        data = self._body()
        decision = data.get("decision")
        if decision not in ("aprobar", "rechazar") or data.get("confirm") is not True:
            raise HttpError(400, "Decisión inválida o sin confirmar")
        row = self.app.db.one("SELECT * FROM approvals WHERE id=?", (int(aid),))
        if row is None:
            raise HttpError(404, "Solicitud inexistente")
        if row["state"] != "pendiente":
            raise HttpError(409, "La solicitud ya fue decidida")
        state = "aprobada" if decision == "aprobar" else "rechazada"
        self.app.db.execute("UPDATE approvals SET state=?, decided_by=?, decided_at=?, note=? WHERE id=? AND state='pendiente'",
                            (state, s.username, time.time(), str(data.get("note", ""))[:1000], row["id"]))
        if state == "aprobada":
            self._write_package(row)
        self.app.db.audit("aprobacion_" + state, username=s.username, target=row["mission_id"], ip=self._ip())
        self.app.bus.publish("aprobacion", f"Solicitud {state}", mission_id=row["mission_id"])
        self._json(200, {"state": state, "nota": "La aprobación NO ejecuta nada en el sistema. Se generó un "
                         "paquete con suma SHA-256 para aplicarlo manualmente por SSH."})

    def _write_package(self, approval) -> None:
        mid = approval["mission_id"]
        code = self.app.files.read_rel(mid, "final/solucion.py")
        if code is None:
            return
        import hashlib

        digest = hashlib.sha256(code.encode()).hexdigest()
        manifest = (f"Constructor IA — paquete aprobado\nMisión: {mid}\nSolicitud: {approval['id']} ({approval['kind']})\n"
                    f"Detalle: {approval['detail']}\nArchivo: final/solucion.py\nSHA-256: {digest}\n\n"
                    "Esta aprobación no instaló nada. Para aplicarlo, descargue el archivo, verifique la suma con\n"
                    "  sha256sum <archivo>\ny cópielo manualmente al destino revisado por usted.\n")
        self.app.files.save(mid, f"paquete/aprobacion-{approval['id']}.txt", manifest, "paquete")

    # Taller Web --------------------------------------------------------
    def _web_prefix(self, mid: str, which: str | None) -> tuple[str, str]:
        row = self._mission(mid)
        if json.loads(row["spec_json"]).get("mode") != "web":
            raise HttpError(409, "La misión no es de tipo «Página web / Tablero»")
        if which in (None, "", "final"):
            if row["final_attempt"]:
                return "final/web/", "final"
            which = str(row["attempt"])
        if not str(which).isdigit() or not 1 <= int(which) <= 20:
            raise HttpError(400, "Intento inválido")
        return f"intento-{int(which)}/web/", f"intento-{int(which)}"

    def _web_files(self, mid: str, prefix: str) -> dict[str, bytes]:
        rows = self.app.db.all("SELECT id, rel_path FROM files WHERE mission_id=? AND substr(rel_path, 1, ?)=?",
                               (mid, len(prefix), prefix))
        out = {}
        for r in rows:
            rel = r["rel_path"][len(prefix):]
            if not webproject.valid_path(rel):
                continue
            try:
                out[rel] = self.app.files.read(r["id"])[1]
            except FileStoreError as e:
                raise HttpError(409, str(e)) from None
        if not out:
            raise HttpError(404, "Ese intento no tiene archivos del proyecto")
        return out

    @route("GET", r"/api/missions/([A-Za-z0-9-]{1,30})/zip")
    def api_zip(self, s: Session, mid):
        prefix, label = self._web_prefix(mid, self._query().get("attempt"))
        files = self._web_files(mid, prefix)
        data = webproject.build_zip(files, f"{mid}-{label}")
        self.app.db.audit("descarga_zip", username=s.username, target=f"{mid}/{label}", ip=self._ip())
        self._send(200, data, "application/zip",
                   {"Content-Disposition": f'attachment; filename="{mid}-{label}.zip"'})

    @route("POST", r"/api/missions/([A-Za-z0-9-]{1,30})/preview")
    def api_preview(self, s: Session, mid):
        cfg = self.app.cfg
        if not (cfg.web_enabled and cfg.web_preview_enabled and self.app.preview_available):
            raise HttpError(409, "Vista previa deshabilitada; use la descarga ZIP")
        data = self._body()
        which = data.get("attempt")
        prefix, label = self._web_prefix(mid, None if which is None else str(which))
        files = self._web_files(mid, prefix)
        if "index.html" not in files:
            raise HttpError(409, "El proyecto no tiene index.html")
        token = self.app.previews.create(mid, prefix)
        self.app.db.audit("vista_previa", username=s.username, target=f"{mid}/{label}", ip=self._ip())
        self._json(201, {"path": f"/p/{token}/index.html", "port": cfg.web_preview_port,
                         "origin": cfg.web_preview_origin or None, "label": label,
                         "expires_in": cfg.web_preview_minutes * 60})

    # Conversaciones ----------------------------------------------------
    def _conv(self, cid: str):
        row = self.app.db.one("SELECT * FROM conversations WHERE id=?", (int(cid),))
        if row is None:
            raise HttpError(404, "Conversación inexistente")
        return row

    def _queue_reply(self, cid: int, kind: str) -> int:
        pending = self.app.db.one("SELECT COUNT(*) FROM messages WHERE status IN ('pendiente','generando')")[0]
        if pending >= 4:
            raise HttpError(429, "Hay demasiadas respuestas en espera; aguarde a que terminen")
        cur = self.app.db.execute(
            "INSERT INTO messages(conversation_id, role, kind, content, status, model, created_at) VALUES(?,?,?,?,?,?,?)",
            (cid, "assistant", kind, "", "pendiente", self.app.cfg.chat.name, time.time()))
        self.app.chat_worker.notify()
        return cur.lastrowid

    @route("GET", "/api/conversations")
    def api_conversations(self, s):
        rows = self.app.db.all("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 100")
        self._json(200, {"items": [dict(r) for r in rows]})

    @route("POST", "/api/conversations")
    def api_new_conversation(self, s: Session):
        data = self._body()
        title = str(data.get("title") or "Nueva conversación").strip()[:100] or "Nueva conversación"
        mid = data.get("mission_id")
        if mid is not None:
            if not isinstance(mid, str):
                raise HttpError(400, "Misión inválida")
            self._mission(mid)
        now = time.time()
        cur = self.app.db.execute("INSERT INTO conversations(title, mission_id, created_by, created_at, updated_at)"
                                  " VALUES(?,?,?,?,?)", (title, mid, s.username, now, now))
        self._json(201, {"id": cur.lastrowid})

    @route("GET", r"/api/conversations/(\d{1,9})")
    def api_conversation(self, s, cid):
        conv = self._conv(cid)
        rows = self.app.db.all("SELECT id, role, kind, content, status, model, error, meta_json, created_at, finished_at"
                               " FROM messages WHERE conversation_id=? ORDER BY id", (conv["id"],))
        msgs = []
        for r in rows:
            r = dict(r)
            r["meta"] = json.loads(r.pop("meta_json")) if r.get("meta_json") else None
            msgs.append(r)
        self._json(200, {"conversation": dict(conv), "messages": msgs})

    @route("POST", r"/api/conversations/(\d{1,9})/messages")
    def api_post_message(self, s: Session, cid):
        conv = self._conv(cid)
        data = self._body()
        kind = data.get("kind", "texto")
        if kind not in ("texto", "borrador"):
            raise HttpError(400, "Tipo de mensaje inválido")
        content = str(data.get("content", "")).replace("\x00", "").strip()
        if kind == "borrador" and not content:
            content = "Generá un borrador de misión a partir de esta conversación."
        if not content or len(content) > 8000:
            raise HttpError(400, "El mensaje debe tener entre 1 y 8000 caracteres")
        now = time.time()
        self.app.db.execute("INSERT INTO messages(conversation_id, role, kind, content, status, created_at, finished_at)"
                            " VALUES(?,?,?,?,?,?,?)", (conv["id"], "user", kind, content, "lista", now, now))
        if conv["title"] == "Nueva conversación":
            self.app.db.execute("UPDATE conversations SET title=? WHERE id=?", (content[:60], conv["id"]))
        reply = self._queue_reply(conv["id"], kind)
        self._json(201, {"reply_id": reply})

    @route("POST", r"/api/conversations/(\d{1,9})/explain")
    def api_explain(self, s: Session, cid):
        conv = self._conv(cid)
        data = self._body()
        fid = data.get("file_id")
        if not isinstance(fid, int):
            raise HttpError(400, "Archivo inválido")
        info, _ = self._file(str(fid))
        now = time.time()
        self.app.db.execute(
            "INSERT INTO messages(conversation_id, role, kind, content, status, meta_json, created_at, finished_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (conv["id"], "user", "texto", f"Explicá en términos simples el archivo {info['rel_path']} "
             f"de la misión {info['mission_id']}: qué hace y cómo funciona.", "lista",
             json.dumps({"file_id": fid}), now, now))
        reply = self._queue_reply(conv["id"], "explicacion")
        self._json(201, {"reply_id": reply})

    # Eventos en vivo (SSE) ---------------------------------------------
    @route("GET", "/api/stream")
    def api_stream(self, s: Session):
        if not self.server.sse_sem.acquire(blocking=False):
            raise HttpError(503, "Demasiadas conexiones en vivo")
        try:
            try:
                after = int(self.headers.get("Last-Event-ID") or self._query().get("after") or 0)
            except ValueError:
                after = 0
            if after <= 0:
                after = self.app.bus.latest_id()
            self.send_response(200)
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(b"retry: 5000\n\n")
            self.wfile.flush()
            started = time.monotonic()
            _, token = self._session()
            while not self.server.stopping.is_set() and time.monotonic() - started < 1800:
                events = self.app.bus.since(after)
                for ev in events:
                    after = ev["id"]
                    self.wfile.write(f"id: {ev['id']}\nevent: ev\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                if events:
                    self.wfile.flush()
                    continue
                self.app.bus.wait(after, 15)
                if not self.app.bus.since(after, 1):
                    if self.app.auth.get_session(token) is None:
                        break  # sesión cerrada o expirada
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        finally:
            self.server.sse_sem.release()


def serve(app: App) -> list[Server]:
    servers = []
    for addr in app.cfg.listen:
        srv = Server((addr, app.cfg.port), Handler, app)
        threading.Thread(target=srv.serve_forever, name=f"http-{addr}", daemon=True).start()
        servers.append(srv)
        log.info("Escuchando en http://%s:%d", addr if ":" not in addr else f"[{addr}]", app.cfg.port)
    return servers
