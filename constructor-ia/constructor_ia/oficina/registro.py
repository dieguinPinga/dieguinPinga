"""Registro de trabajadores y comprobación de disponibilidad (etapa 1 de la oficina, OFICINA.md).

Un trabajador es una computadora que ofrece inferencias Ollama a través del tailnet. Se registra con:
- una URL http del tailnet (100.64.0.0/10 o un nombre *.ts.net) con puerto explícito, sin ruta ni credenciales;
- su identidad de Tailscale comprobada con «tailscale whois» contra el nodo esperado (decisión: ACL restrictiva más
  comprobación de identidad, sin token adicional);
- los modelos que declara, sus recursos y su capacidad: UN turno de inferencia simultáneo (decisión del MVP);
- permisos: solo «inferencia». La ejecución remota de código no existe en esta etapa.

La comprobación de disponibilidad solo LEE: /api/version, /api/tags y /api/ps (OllamaClient, sin cambios). Nunca
pide una inferencia ni usa /api/pull, /api/delete o /api/create. Cada comprobación queda registrada. Un trabajador
está «disponible» si responde, su identidad sigue siendo la registrada y tiene todos los modelos que declaró; su
latido vence a los LATIDO_S segundos de la última comprobación exitosa.

Las URL, los nombres de nodo y las identidades viven solo en la base local de la oficina, nunca en el repositorio.
"""
from __future__ import annotations

import ipaddress
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..db import Database
from ..ollama import OllamaClient, OllamaError

LATIDO_S = 120
CAPACIDAD_MVP = 1
PERMISOS_MVP = ("inferencia",)
RUTAS_DE_LECTURA = ("/api/version", "/api/tags", "/api/ps")
TAILNET = ipaddress.ip_network("100.64.0.0/10")
PUERTO_OLLAMA_PRINCIPAL = 11434

ESQUEMA = """
CREATE TABLE IF NOT EXISTS trabajadores (
  id TEXT PRIMARY KEY, url TEXT NOT NULL, nodo TEXT, identidad_json TEXT, modelos_json TEXT NOT NULL,
  recursos_json TEXT NOT NULL, capacidad INTEGER NOT NULL, permisos_json TEXT NOT NULL, estado TEXT NOT NULL,
  motivo TEXT, ultimo_latido REAL, creado REAL NOT NULL, actualizado REAL NOT NULL);
CREATE TABLE IF NOT EXISTS comprobaciones (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trabajador_id TEXT NOT NULL REFERENCES trabajadores(id), ts REAL NOT NULL,
  disponible INTEGER NOT NULL, latencia_ms INTEGER, version TEXT, modelos_json TEXT, cargados_json TEXT,
  faltan_json TEXT, identidad_json TEXT, motivo TEXT);
"""
ESTADOS = ("registrado", "disponible", "no_disponible", "retirado")


class ErrorRegistro(ValueError):
    pass


def validar_url(url: str, permitir_local: bool = False) -> str:
    """http://<host del tailnet>:<puerto>, sin ruta, consulta ni credenciales. Loopback solo para pruebas, y nunca el
    puerto del Ollama principal."""
    p = urlsplit(url.strip())
    if p.scheme != "http":
        raise ErrorRegistro("la URL tiene que ser http://… (Tailscale Serve cifra el tramo dentro del tailnet)")
    if p.username or p.password:
        raise ErrorRegistro("la URL no puede llevar credenciales")
    if p.path not in ("", "/") or p.query or p.fragment:
        raise ErrorRegistro("la URL no puede llevar ruta ni consulta: solo http://host:puerto")
    if not p.hostname or p.port is None:
        raise ErrorRegistro("falta el host o el puerto explícito")
    host = p.hostname.lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    local = host == "localhost" or (ip is not None and ip.is_loopback)
    if local:
        if not permitir_local:
            raise ErrorRegistro("un trabajador local (loopback) solo se admite en pruebas (--permitir-local)")
        if p.port == PUERTO_OLLAMA_PRINCIPAL:
            raise ErrorRegistro(f"el puerto {PUERTO_OLLAMA_PRINCIPAL} es el Ollama principal: la oficina no lo usa")
    elif not ((ip is not None and ip in TAILNET) or (ip is None and host.endswith(".ts.net"))):
        raise ErrorRegistro("el host tiene que ser del tailnet: una IP 100.64.0.0/10 o un nombre *.ts.net")
    return f"http://{host}:{p.port}"


def whois_tailscale(host: str, timeout: float = 5.0) -> dict:
    """Identidad de un nodo según el demonio local de Tailscale (solo lectura: «tailscale whois --json»)."""
    exe = shutil.which("tailscale")
    if exe is None:
        raise ErrorRegistro("no está el comando tailscale: no se puede comprobar la identidad")
    try:
        r = subprocess.run([exe, "whois", "--json", host], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ErrorRegistro("tailscale whois no respondió") from None
    if r.returncode != 0:
        raise ErrorRegistro(f"tailscale whois falló: {(r.stderr or r.stdout).strip()[:200]}")
    try:
        d = json.loads(r.stdout)
    except ValueError:
        raise ErrorRegistro("tailscale whois devolvió algo ilegible") from None
    nodo = d.get("Node") or {}
    return {"nodo": (nodo.get("ComputedName") or nodo.get("Name") or "").rstrip("."),
            "nombre_dns": (nodo.get("Name") or "").rstrip("."), "etiquetas": list(nodo.get("Tags") or []),
            "usuario": (d.get("UserProfile") or {}).get("LoginName")}


def _coincide(identidad: dict, nodo: str) -> bool:
    n = nodo.lower().rstrip(".")
    return n in {identidad.get("nodo", "").lower(), identidad.get("nombre_dns", "").lower(),
                 identidad.get("nombre_dns", "").lower().split(".")[0]}


def _host(url: str) -> str:
    return urlsplit(url).hostname or ""


def _es_local(url: str) -> bool:
    h = _host(url)
    try:
        return h == "localhost" or ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


class Registro:
    """Trabajadores de la oficina en una base SQLite propia (<datos>/oficina/oficina.db por omisión)."""

    def __init__(self, ruta: Path, whois=whois_tailscale, cliente=OllamaClient, reloj=time.time):
        self.db = Database(Path(ruta))
        self.db.connect().executescript(ESQUEMA)
        self.whois, self.cliente, self.reloj = whois, cliente, reloj

    def cerrar(self):
        self.db.close()

    # Registro ---------------------------------------------------------------------------------------------------
    def registrar(self, tid: str, url: str, modelos: list[str], *, nodo: str | None = None, recursos: dict | None = None,
                  permitir_local: bool = False) -> dict:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", tid or ""):
            raise ErrorRegistro("el id del trabajador: minúsculas, números, «-» o «_», hasta 40 caracteres")
        url = validar_url(url, permitir_local)
        modelos = sorted({m.strip() for m in modelos if m and m.strip()})
        if not modelos:
            raise ErrorRegistro("el trabajador tiene que declarar al menos un modelo")
        if _es_local(url):
            identidad = {"local": True, "nota": "loopback de prueba: sin identidad de Tailscale"}
        else:
            if not nodo:
                raise ErrorRegistro("falta --nodo: el nombre del nodo de Tailscale esperado (se comprueba con whois)")
            identidad = self.whois(_host(url))
            if not _coincide(identidad, nodo):
                raise ErrorRegistro(f"la identidad no coincide: {_host(url)} es «{identidad.get('nodo')}» y se "
                                    f"esperaba «{nodo}»")
        previo = self.obtener(tid)
        if previo and previo["estado"] != "retirado":
            raise ErrorRegistro(f"«{tid}» ya está registrado (estado {previo['estado']}); retiralo antes de volver a "
                                "registrarlo")
        ahora = self.reloj()
        fila = (url, nodo, json.dumps(identidad, ensure_ascii=False), json.dumps(modelos),
                json.dumps(recursos or {}, ensure_ascii=False), CAPACIDAD_MVP, json.dumps(list(PERMISOS_MVP)),
                "registrado", "registrado; falta la primera comprobación de disponibilidad", None, ahora)
        with self.db.tx() as c:
            if previo:
                c.execute("UPDATE trabajadores SET url=?, nodo=?, identidad_json=?, modelos_json=?, recursos_json=?, "
                          "capacidad=?, permisos_json=?, estado=?, motivo=?, ultimo_latido=?, actualizado=? WHERE id=?",
                          fila + (tid,))
            else:
                c.execute("INSERT INTO trabajadores (url, nodo, identidad_json, modelos_json, recursos_json, capacidad, "
                          "permisos_json, estado, motivo, ultimo_latido, actualizado, id, creado) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", fila + (tid, ahora))
        return self.obtener(tid)

    def retirar(self, tid: str, motivo: str = "retirado por el operador") -> dict:
        if not self.obtener(tid):
            raise ErrorRegistro(f"«{tid}» no está registrado")
        self.db.execute("UPDATE trabajadores SET estado='retirado', motivo=?, actualizado=? WHERE id=?",
                        (motivo, self.reloj(), tid))
        return self.obtener(tid)

    # Consulta ---------------------------------------------------------------------------------------------------
    def obtener(self, tid: str) -> dict | None:
        r = self.db.one("SELECT * FROM trabajadores WHERE id=?", (tid,))
        return self._dict(r) if r else None

    def trabajadores(self, incluir_retirados: bool = False) -> list[dict]:
        return [self._dict(r) for r in self.db.all("SELECT * FROM trabajadores ORDER BY id")
                if incluir_retirados or r["estado"] != "retirado"]

    def comprobaciones(self, tid: str) -> list[dict]:
        out = []
        for r in self.db.all("SELECT * FROM comprobaciones WHERE trabajador_id=? ORDER BY id", (tid,)):
            d = dict(r)
            for k in ("modelos_json", "cargados_json", "faltan_json", "identidad_json"):
                d[k[:-5]] = json.loads(d.pop(k) or "null")
            out.append(d)
        return out

    def _dict(self, r) -> dict:
        d = dict(r)
        for k in ("identidad_json", "modelos_json", "recursos_json", "permisos_json"):
            d[k[:-5]] = json.loads(d.pop(k) or "null")
        d["latido_vigente"] = bool(d["ultimo_latido"]) and self.reloj() - d["ultimo_latido"] <= LATIDO_S
        d["disponible_ahora"] = d["estado"] == "disponible" and d["latido_vigente"]
        return d

    # Disponibilidad (solo lectura) ------------------------------------------------------------------------------
    def comprobar(self, tid: str) -> dict:
        """Identidad (si no es local), /api/version, /api/tags y /api/ps. Nunca una inferencia."""
        t = self.obtener(tid)
        if t is None:
            raise ErrorRegistro(f"«{tid}» no está registrado")
        if t["estado"] == "retirado":
            raise ErrorRegistro(f"«{tid}» está retirado")
        identidad, motivo = t["identidad"], None
        version = presentes = cargados = None
        faltan: list[str] = []
        latencia = None
        if not _es_local(t["url"]):
            try:
                identidad = self.whois(_host(t["url"]))
                if not _coincide(identidad, t["nodo"]):
                    motivo = (f"la identidad cambió: {_host(t['url'])} ahora es «{identidad.get('nodo')}», no "
                              f"«{t['nodo']}»")
            except ErrorRegistro as e:
                motivo = f"no se pudo comprobar la identidad: {e}"
        if motivo is None:
            cli = self.cliente(t["url"])
            t0 = time.monotonic()
            try:
                version = cli.version()
                presentes = cli.tags()
                cargados = cli.loaded()
                latencia = int((time.monotonic() - t0) * 1000)
                faltan = [m for m in t["modelos"] if m not in presentes]
                if faltan:
                    motivo = f"faltan modelos declarados: {', '.join(faltan)}"
            except OllamaError as e:
                motivo = f"Ollama no responde: {e}"
        ok = motivo is None
        ahora = self.reloj()
        with self.db.tx() as c:
            c.execute("INSERT INTO comprobaciones (trabajador_id, ts, disponible, latencia_ms, version, modelos_json, "
                      "cargados_json, faltan_json, identidad_json, motivo) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (tid, ahora, int(ok), latencia, version, json.dumps(presentes), json.dumps(cargados),
                       json.dumps(faltan), json.dumps(identidad, ensure_ascii=False), motivo))
            c.execute("UPDATE trabajadores SET estado=?, motivo=?, actualizado=?" + (", ultimo_latido=?" if ok else "")
                      + " WHERE id=?", (("disponible", "responde con todos sus modelos", ahora, ahora, tid) if ok else
                                        ("no_disponible", motivo, ahora, tid)))
        return self.obtener(tid) | {"comprobacion": {"latencia_ms": latencia, "version": version, "cargados": cargados,
                                                     "faltan": faltan, "motivo": motivo}}

    def comprobar_todos(self) -> list[dict]:
        return [self.comprobar(t["id"]) for t in self.trabajadores()]


def texto_estado(trabajadores: list[dict]) -> str:
    if not trabajadores:
        return "No hay trabajadores registrados."
    out = []
    for t in trabajadores:
        c = t.get("comprobacion") or {}
        latido = ("vigente" if t["latido_vigente"] else "vencido") if t["ultimo_latido"] else "sin latido"
        out.append(f"{t['id']}: {t['estado']} ({t['motivo']}) · latido {latido} · modelos {', '.join(t['modelos'])}"
                   f" · capacidad {t['capacidad']} · permisos {', '.join(t['permisos'])}"
                   + (f" · {c['latencia_ms']} ms" if c.get("latencia_ms") is not None else "")
                   + (f" · cargados: {', '.join(c['cargados']) or 'ninguno'}" if c.get("cargados") is not None else ""))
    return "\n".join(out)
