"""Vista previa del Taller Web: superficie NO confiable, en un origen separado.

* Otro puerto (web.preview_port, 8767 por defecto) → otro origen que el panel:
  el JavaScript generado no puede leer el DOM, las cookies HttpOnly ni las
  respuestas del panel.
* Solo sirve archivos estáticos ya registrados en la base (con su SHA-256) de
  un intento concreto, mediante un token aleatorio con vencimiento. No hay
  sesiones, no se emiten cookies, no se ejecuta nada en el servidor.
* CSP restrictiva con `sandbox` (también al abrirla fuera del iframe): sin
  ventanas emergentes, sin navegación del documento superior, sin conexiones
  ni recursos de otros orígenes, sin formularios que salgan del proyecto.
* La API del panel rechaza peticiones con Sec-Fetch-Site same-site/cross-site,
  así que el navegador no puede usar la cookie del panel desde aquí.
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

from .. import webproject
from ..files import FileStoreError

log = logging.getLogger(__name__)
PATH_RE = re.compile(r"^/p/([A-Za-z0-9_-]{32,64})/([a-z0-9_./-]{0,200})$")


class PreviewTokens:
    """Tokens en memoria: un reinicio del servidor los invalida (se generan de nuevo desde el panel)."""

    def __init__(self, minutes: int):
        self.ttl = minutes * 60
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[str, str, float]] = {}

    def create(self, mission_id: str, prefix: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._tokens = {k: v for k, v in self._tokens.items() if v[2] > now}
            if len(self._tokens) >= 200:
                oldest = min(self._tokens, key=lambda k: self._tokens[k][2])
                del self._tokens[oldest]
            self._tokens[token] = (mission_id, prefix, now + self.ttl)
        return token

    def resolve(self, token: str) -> tuple[str, str] | None:
        with self._lock:
            v = self._tokens.get(token)
        if v is None or v[2] < time.time():
            return None
        return v[0], v[1]


def preview_csp(frame_ancestors: list[str]) -> str:
    return ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "font-src 'self' data:; media-src 'self' data: blob:; connect-src 'self'; worker-src 'self'; "
            "manifest-src 'self'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'; "
            f"frame-ancestors {' '.join(frame_ancestors) or chr(39) + 'none' + chr(39)}; "
            "sandbox allow-scripts allow-same-origin allow-forms allow-modals")


def panel_origins(cfg) -> list[str]:
    """Orígenes del panel autorizados a enmarcar la vista previa."""
    out = []
    for h in ["127.0.0.1", "localhost", *cfg.listen, *[a for a in cfg.allowed_hosts if not a.startswith(".")]]:
        o = f"http://{'[' + h + ']' if ':' in h else h}:{cfg.port}"
        if o not in out:
            out.append(o)
    if any(a.endswith("ts.net") for a in cfg.allowed_hosts):
        out.append("https://*.ts.net:*")
    out.extend(cfg.web_panel_origins)
    return out


class PreviewHandler(BaseHTTPRequestHandler):
    server_version = "ConstructorIA-Preview"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 20

    def log_message(self, fmt, *args):  # sin tokens ni rutas en el registro
        pass

    def _plain(self, status: int, text: str):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; sandbox")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        app = self.server.app
        from .server import host_allowed

        if not host_allowed(app.cfg.allowed_hosts, self.headers.get("Host")):
            self._plain(421, "Host no permitido")
            return
        m = PATH_RE.match(urlsplit(self.path).path)
        if not m:
            self._plain(404, "No encontrado")
            return
        token, rel = m.group(1), m.group(2)
        if rel == "" or rel.endswith("/"):
            rel += "index.html"
        target = app.previews.resolve(token)
        if target is None or not webproject.valid_path(rel):
            self._plain(404, "No encontrado o vista previa vencida")
            return
        mission_id, prefix = target
        try:
            content = app.files.read_rel(mission_id, prefix + rel)
        except FileStoreError:
            content = None
        if content is None:
            self._plain(404, "No encontrado")
            return
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", webproject.content_type(rel))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", preview_csp(panel_origins(app.cfg)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), usb=(), payment=(), "
                                               "clipboard-read=(), display-capture=()")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_HEAD = do_GET

    def do_POST(self):
        self._plain(405, "Método no permitido")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST


def serve_preview(app) -> list:
    from .server import Server

    servers = []
    for addr in app.cfg.listen:
        srv = Server((addr, app.cfg.web_preview_port), PreviewHandler, app)
        threading.Thread(target=srv.serve_forever, name=f"preview-{addr}", daemon=True).start()
        servers.append(srv)
        log.info("Vista previa (origen separado) en http://%s:%d", addr if ":" not in addr else f"[{addr}]",
                 app.cfg.web_preview_port)
    return servers
