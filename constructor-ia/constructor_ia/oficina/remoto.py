"""Etapa 3: inferencia remota acotada, candado por destino y planificador (OFICINA.md).

- ClienteRemoto es el OllamaClient de siempre (sin cambios en ollama.py) con una LISTA BLANCA de rutas: solo
  /api/version, /api/tags, /api/ps (lecturas) y /api/chat (inferencia). /api/pull, /api/delete, /api/create y
  cualquier otra ruta se rechazan ANTES de abrir la conexión. Ollama no tiene permisos por ruta: la lista blanca es
  del lado del coordinador y la barrera del lado de la red es la ACL de Tailscale.
- compuerta(destino): un InferenceGate POR DESTINO (URL). Un turno de inferencia por trabajador; destinos distintos
  no se bloquean entre sí. El candado global de la aplicación no se toca.
- elegir(): el planificador, por regla fija y con motivo. Un trabajador sirve si está disponible AHORA (latido
  vigente), tiene el modelo y el permiso, y no tiene un turno ocupado (la cola lo garantiza). Un destino LOCAL de la
  VivoBook, además, necesita memoria disponible ≥ el umbral del piloto (3900 MiB); si no hay ninguno, la tarea queda
  pendiente con el motivo.
"""
from __future__ import annotations

import http.client
import ipaddress
import threading
from urllib.parse import urlsplit

from ..ollama import InferenceGate, OllamaClient, OllamaError

RUTAS = {("GET", "/api/version"), ("GET", "/api/tags"), ("GET", "/api/ps"), ("POST", "/api/chat")}
UMBRAL_LOCAL_MB = 3900


class RutaNoPermitida(OllamaError):
    pass


class _ConexionAcotada(http.client.HTTPConnection):
    def request(self, method, url, body=None, headers=None, **kw):
        ruta = urlsplit(url).path
        if (method.upper(), ruta) not in RUTAS:
            raise RutaNoPermitida(f"ruta no permitida hacia el trabajador: {method} {ruta}")
        return super().request(method, url, body=body, headers=headers or {}, **kw)

    def putrequest(self, method, url, *a, **kw):
        if (method.upper(), urlsplit(url).path) not in RUTAS:
            raise RutaNoPermitida(f"ruta no permitida hacia el trabajador: {method} {urlsplit(url).path}")
        return super().putrequest(method, url, *a, **kw)


class ClienteRemoto(OllamaClient):
    def _conn(self, timeout: float) -> http.client.HTTPConnection:
        return _ConexionAcotada(self.host, self.port, timeout=timeout)


_COMPUERTAS: dict[str, InferenceGate] = {}
_LOCK = threading.Lock()


def compuerta(destino: str) -> InferenceGate:
    with _LOCK:
        return _COMPUERTAS.setdefault(destino.rstrip("/"), InferenceGate())


def memoria_disponible_mb() -> int | None:
    try:
        for linea in open("/proc/meminfo", encoding="ascii"):
            if linea.startswith("MemAvailable:"):
                return int(linea.split()[1]) // 1024
    except OSError:
        return None
    return None


def es_local(url: str) -> bool:
    h = urlsplit(url).hostname or ""
    try:
        return h == "localhost" or ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def elegir(tarea: dict, trabajadores: list[dict], memoria_mb=memoria_disponible_mb,
           umbral_mb: int = UMBRAL_LOCAL_MB) -> tuple[dict | None, str]:
    """(trabajador, motivo). Orden: el primero compatible por id; los remotos no dependen de la RAM de la VivoBook."""
    motivos = []
    for t in sorted(trabajadores, key=lambda x: x["id"]):
        if not t.get("disponible_ahora"):
            motivos.append(f"{t['id']}: no disponible ahora ({t.get('motivo')})")
        elif tarea["modelo"] not in t["modelos"]:
            motivos.append(f"{t['id']}: no tiene {tarea['modelo']}")
        elif tarea["permiso"] not in t["permisos"]:
            motivos.append(f"{t['id']}: sin permiso «{tarea['permiso']}»")
        elif es_local(t["url"]) and (memoria_mb() or 0) < umbral_mb:
            motivos.append(f"{t['id']}: destino local con {memoria_mb()} MiB disponibles (< {umbral_mb})")
        else:
            return t, f"{t['id']}: disponible, con {tarea['modelo']} y permiso «{tarea['permiso']}»"
    return None, "ningún trabajador compatible; la tarea queda pendiente: " + ("; ".join(motivos) or "no hay trabajadores")
