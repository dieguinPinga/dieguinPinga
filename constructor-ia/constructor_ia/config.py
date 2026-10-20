"""Carga y validación de configuración (TOML).

Todos los valores tienen límites seguros; un valor fuera de rango es un error
de configuración y el servidor no arranca (fallo cerrado).
"""
from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


# Puertos que pertenecen a servicios existentes en la VivoBook y que nunca
# deben usarse para escuchar.
RESERVED_PORTS = {1880, 3306, 11434, 11435, 18789}


@dataclass
class ModelConfig:
    name: str
    num_ctx: int = 4096
    num_predict: int = 1536
    temperature: float = 0.2
    timeout_s: int = 300
    think: bool = False


@dataclass
class ExplorerConnection:
    name: str
    database: str
    user: str
    password_file: str
    host: str = "127.0.0.1"
    port: int = 3306
    socket: str = ""


@dataclass
class Config:
    base_dir: Path
    data_dir: Path
    listen: list[str] = field(default_factory=lambda: ["127.0.0.1"])
    port: int = 8766
    allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1", "localhost", ".ts.net"])
    cookie_secure: bool = True
    session_idle_hours: int = 12
    session_max_days: int = 7
    max_connections: int = 32
    max_sse_clients: int = 6

    ollama_url: str = "http://127.0.0.1:11435"
    builder: ModelConfig = field(default_factory=lambda: ModelConfig("qwen3:4b-instruct"))
    chat: ModelConfig = field(default_factory=lambda: ModelConfig("qwen3:1.7b", num_predict=768, temperature=0.5, timeout_s=240))
    web_model: ModelConfig = field(default_factory=lambda: ModelConfig("qwen3:4b-instruct", num_ctx=6144,
                                                                       num_predict=3072, timeout_s=600))

    web_enabled: bool = True
    web_preview_enabled: bool = True
    web_preview_port: int = 8767
    web_preview_origin: str = ""
    web_panel_origins: list[str] = field(default_factory=list)
    web_preview_minutes: int = 120
    web_max_files: int = 12
    web_max_file_kb: int = 100
    web_max_total_kb: int = 400
    web_node: str = "/usr/bin/node"

    explorer_enabled: bool = True
    explorer_connections: list[ExplorerConnection] = field(default_factory=list)
    explorer_model: ModelConfig = field(default_factory=lambda: ModelConfig("qwen3:4b-instruct", num_ctx=6144,
                                                                            num_predict=900, timeout_s=300))
    explorer_max_steps: int = 25
    explorer_max_queries: int = 60
    explorer_max_minutes: int = 45
    explorer_query_timeout_s: int = 10
    explorer_max_rows: int = 200
    explorer_max_result_kb: int = 256
    explorer_max_join_size: int = 20_000_000
    explorer_sample_rows: int = 100_000
    explorer_report_num_predict: int = 1800
    explorer_decide_num_predict: int = 384
    explorer_interpret_num_predict: int = 512
    # 0.4.8: límites de las reparaciones (una reparación improductiva no puede comerse la exploración)
    explorer_repair_timeout_s: int = 90       # máximo por pedido de reparación
    explorer_repair_budget_s: int = 300       # tiempo total de reparaciones por expedición
    explorer_repair_num_predict: int = 192    # tokens de una respuesta de reparación
    # Investigador experimental (0.5): ciclo memoria → pregunta → evidencia → lectura y siguiente pregunta
    investigador_max_ciclos: int = 4
    investigador_max_minutos: int = 15
    investigador_contexto_tokens: int = 1400   # tope del contexto seleccionado de la memoria
    investigador_pedido_num_predict: int = 320
    investigador_lectura_num_predict: int = 720
    # Instrumentos de investigación (0.6): presupuestos OPERATIVOS por ejecución (no son decisiones metodológicas)
    instrumentos_consultas: int = 20
    instrumentos_filas: int = 50_000          # filas transferidas al instrumento (las agregaciones en SQL no cuentan)
    instrumentos_bytes_mb: int = 8
    instrumentos_segundos: int = 60           # cálculo del instrumento (sin contar la espera de MariaDB)
    instrumentos_resultado_kb: int = 4
    instrumentos_codigo_kb: int = 24
    instrumentos_retencion_dias: int = 30     # resultados no anclados: caducan por tiempo…
    instrumentos_cuota_mb: int = 50           # …y por cuota de almacenamiento
    instrumentos_modulos_extra: list = field(default_factory=list)  # capacidades verificadas que se agreguen

    max_attempts: int = 3
    max_attempts_limit: int = 6

    sandbox_enabled: bool = True
    sandbox_python: str = "/usr/bin/python3"
    sandbox_memory_mb: int = 256
    sandbox_tasks: int = 32
    sandbox_cpu_percent: int = 50
    sandbox_timeout_s: int = 20
    sandbox_case_timeout_s: int = 5
    sandbox_tmpfs_mb: int = 32
    sandbox_seccomp: bool = True

    event_retention_days: int = 60
    max_events_per_mission: int = 2000
    log_max_bytes: int = 2_000_000
    log_backups: int = 3

    @property
    def db_path(self) -> Path:
        return self.data_dir / "constructor.db"

    @property
    def missions_dir(self) -> Path:
        return self.data_dir / "missions"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


def _int(d: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = d.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise ConfigError(f"{key} debe ser un entero entre {lo} y {hi}")
    return v


def _float(d: dict, key: str, default: float, lo: float, hi: float) -> float:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ConfigError(f"{key} debe ser un número entre {lo} y {hi}")
    return float(v)


def _bool(d: dict, key: str, default: bool) -> bool:
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise ConfigError(f"{key} debe ser true o false")
    return v


def _str(d: dict, key: str, default: str) -> str:
    v = d.get(key, default)
    if not isinstance(v, str) or not v or len(v) > 200:
        raise ConfigError(f"{key} debe ser un texto no vacío")
    return v


def _model(d: dict, default: ModelConfig) -> ModelConfig:
    return ModelConfig(
        name=_str(d, "name", default.name),
        num_ctx=_int(d, "num_ctx", default.num_ctx, 1024, 16384),
        num_predict=_int(d, "num_predict", default.num_predict, 64, 4096),
        temperature=_float(d, "temperature", default.temperature, 0.0, 1.5),
        timeout_s=_int(d, "timeout_s", default.timeout_s, 10, 900),
        think=_bool(d, "think", default.think),
    )


def _validate_ollama_url(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError("ollama_url debe ser http://127.0.0.1:<puerto> (solo loopback)")
    if parts.port == 11434:
        raise ConfigError("ollama_url no puede apuntar a la instancia principal 11434; usar la instancia CPU 11435")
    return url.rstrip("/")


def load(path: Path | None = None, base_dir: Path | None = None) -> Config:
    base_dir = Path(base_dir or os.environ.get("CIA_BASE_DIR") or Path(__file__).resolve().parent.parent)
    if path is None:
        env = os.environ.get("CIA_CONFIG")
        path = Path(env) if env else base_dir / "config" / "config.toml"
    raw: dict = {}
    if path.exists():
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    server = raw.get("server", {})
    ollama = raw.get("ollama", {})
    engine = raw.get("engine", {})
    sandbox = raw.get("sandbox", {})
    retention = raw.get("retention", {})
    web = raw.get("web", {})
    explorer = raw.get("explorer", {})

    data_dir = Path(server.get("data_dir", base_dir / "data")).expanduser()
    if not data_dir.is_absolute():
        data_dir = base_dir / data_dir
    cfg = Config(base_dir=base_dir, data_dir=data_dir.resolve())

    listen = server.get("listen", cfg.listen)
    if not isinstance(listen, list) or not listen or len(listen) > 4:
        raise ConfigError("server.listen debe ser una lista de 1 a 4 direcciones IP")
    for addr in listen:
        try:
            ip = ipaddress.ip_address(addr)
        except (ValueError, TypeError):
            raise ConfigError(f"Dirección de escucha inválida: {addr!r}") from None
        if ip.is_unspecified:
            raise ConfigError("No se permite escuchar en 0.0.0.0/:: (exposición a toda la red)")
        if not (ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10") or
                ip in ipaddress.ip_network("fd7a:115c:a1e0::/48")):
            raise ConfigError(f"{addr}: solo se admite loopback o una IP Tailscale (100.64.0.0/10)")
    cfg.listen = list(listen)
    cfg.port = _int(server, "port", cfg.port, 1024, 65535)
    if cfg.port in RESERVED_PORTS:
        raise ConfigError(f"El puerto {cfg.port} pertenece a un servicio existente")
    hosts = server.get("allowed_hosts", cfg.allowed_hosts)
    if not isinstance(hosts, list) or not all(isinstance(h, str) and h for h in hosts):
        raise ConfigError("server.allowed_hosts debe ser una lista de textos")
    cfg.allowed_hosts = [h.lower() for h in hosts]
    cfg.cookie_secure = _bool(server, "cookie_secure", cfg.cookie_secure)
    cfg.session_idle_hours = _int(server, "session_idle_hours", cfg.session_idle_hours, 1, 168)
    cfg.session_max_days = _int(server, "session_max_days", cfg.session_max_days, 1, 90)
    cfg.max_connections = _int(server, "max_connections", cfg.max_connections, 4, 128)
    cfg.max_sse_clients = _int(server, "max_sse_clients", cfg.max_sse_clients, 1, 32)

    cfg.ollama_url = _validate_ollama_url(_str(ollama, "url", cfg.ollama_url))
    cfg.builder = _model(ollama.get("builder", {}), cfg.builder)
    cfg.chat = _model(ollama.get("chat", {}), cfg.chat)
    cfg.web_model = _model(ollama.get("web", {}), cfg.web_model)

    cfg.web_enabled = _bool(web, "enabled", cfg.web_enabled)
    cfg.web_preview_enabled = _bool(web, "preview_enabled", cfg.web_preview_enabled)
    cfg.web_preview_port = _int(web, "preview_port", cfg.web_preview_port, 1024, 65535)
    if cfg.web_preview_port in RESERVED_PORTS or cfg.web_preview_port == cfg.port:
        raise ConfigError("web.preview_port debe ser distinto del panel y de los servicios existentes")
    origin = web.get("preview_origin", "")
    if not isinstance(origin, str) or (origin and not re.match(r"^https?://[A-Za-z0-9.\[\]:-]+$", origin)):
        raise ConfigError("web.preview_origin debe ser un origen como https://equipo.tailnet.ts.net:8444")
    cfg.web_preview_origin = origin.rstrip("/")
    panel_origins = web.get("panel_origins", [])
    if not isinstance(panel_origins, list) or not all(
            isinstance(o, str) and re.match(r"^https?://[A-Za-z0-9.*\[\]:-]+$", o) for o in panel_origins):
        raise ConfigError("web.panel_origins debe ser una lista de orígenes https://host:puerto")
    cfg.web_panel_origins = list(panel_origins)
    cfg.web_preview_minutes = _int(web, "preview_minutes", cfg.web_preview_minutes, 5, 1440)
    cfg.web_max_files = _int(web, "max_files", cfg.web_max_files, 1, 40)
    cfg.web_max_file_kb = _int(web, "max_file_kb", cfg.web_max_file_kb, 4, 512)
    cfg.web_max_total_kb = _int(web, "max_total_kb", cfg.web_max_total_kb, 16, 2048)
    cfg.web_node = _str(web, "node", cfg.web_node)
    _load_explorer(cfg, explorer, ollama.get("explorer", {}))
    if not cfg.web_node.startswith("/usr/"):
        raise ConfigError("web.node debe estar bajo /usr (es lo único visible dentro del sandbox)")

    cfg.max_attempts_limit = _int(engine, "max_attempts_limit", cfg.max_attempts_limit, 1, 10)
    cfg.max_attempts = _int(engine, "max_attempts", cfg.max_attempts, 1, cfg.max_attempts_limit)

    cfg.sandbox_enabled = _bool(sandbox, "enabled", cfg.sandbox_enabled)
    cfg.sandbox_python = _str(sandbox, "python", cfg.sandbox_python)
    cfg.sandbox_memory_mb = _int(sandbox, "memory_mb", cfg.sandbox_memory_mb, 64, 1024)
    cfg.sandbox_tasks = _int(sandbox, "tasks_max", cfg.sandbox_tasks, 4, 128)
    cfg.sandbox_cpu_percent = _int(sandbox, "cpu_percent", cfg.sandbox_cpu_percent, 10, 100)
    cfg.sandbox_timeout_s = _int(sandbox, "timeout_s", cfg.sandbox_timeout_s, 5, 120)
    cfg.sandbox_case_timeout_s = _int(sandbox, "case_timeout_s", cfg.sandbox_case_timeout_s, 1, 30)
    cfg.sandbox_tmpfs_mb = _int(sandbox, "tmpfs_mb", cfg.sandbox_tmpfs_mb, 4, 256)
    cfg.sandbox_seccomp = _bool(sandbox, "seccomp", cfg.sandbox_seccomp)

    cfg.event_retention_days = _int(retention, "event_days", cfg.event_retention_days, 1, 3650)
    cfg.max_events_per_mission = _int(retention, "max_events_per_mission", cfg.max_events_per_mission, 100, 20000)
    cfg.log_max_bytes = _int(retention, "log_max_bytes", cfg.log_max_bytes, 100_000, 50_000_000)
    cfg.log_backups = _int(retention, "log_backups", cfg.log_backups, 1, 20)
    ins = raw.get("instrumentos", {})
    cfg.instrumentos_consultas = _int(ins, "consultas", cfg.instrumentos_consultas, 1, 1000)
    cfg.instrumentos_filas = _int(ins, "filas", cfg.instrumentos_filas, 100, 5_000_000)
    cfg.instrumentos_bytes_mb = _int(ins, "bytes_mb", cfg.instrumentos_bytes_mb, 1, 512)
    cfg.instrumentos_segundos = _int(ins, "segundos", cfg.instrumentos_segundos, 5, 3600)
    cfg.instrumentos_resultado_kb = _int(ins, "resultado_kb", cfg.instrumentos_resultado_kb, 1, 256)
    cfg.instrumentos_codigo_kb = _int(ins, "codigo_kb", cfg.instrumentos_codigo_kb, 2, 256)
    cfg.instrumentos_retencion_dias = _int(ins, "retencion_dias", cfg.instrumentos_retencion_dias, 1, 3650)
    cfg.instrumentos_cuota_mb = _int(ins, "cuota_mb", cfg.instrumentos_cuota_mb, 1, 10_000)
    extra = ins.get("modulos_extra", [])
    if not isinstance(extra, list) or not all(isinstance(m, str) and m.replace("_", "").isalnum() for m in extra):
        raise ConfigError("[instrumentos] modulos_extra debe ser una lista de nombres de módulos")
    from .instrumentos.estatico import NEVER_MODULES

    never = sorted(set(m.split(".")[0] for m in extra) & NEVER_MODULES)
    if never:
        raise ConfigError(f"[instrumentos] modulos_extra no puede habilitar {', '.join(never)}")
    cfg.instrumentos_modulos_extra = list(extra)
    inv = raw.get("investigador", {})
    cfg.investigador_max_ciclos = _int(inv, "max_ciclos", cfg.investigador_max_ciclos, 1, 50)
    cfg.investigador_max_minutos = _int(inv, "max_minutos", cfg.investigador_max_minutos, 1, 240)
    cfg.investigador_contexto_tokens = _int(inv, "contexto_tokens", cfg.investigador_contexto_tokens, 300, 6000)
    cfg.investigador_pedido_num_predict = _int(inv, "pedido_num_predict", cfg.investigador_pedido_num_predict, 96, 2048)
    cfg.investigador_lectura_num_predict = _int(inv, "lectura_num_predict", cfg.investigador_lectura_num_predict, 128,
                                                4096)
    return cfg


CONN_NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")
DB_NAME_RE = re.compile(r"^[A-Za-z0-9_$]{1,64}$")
SQL_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _load_explorer(cfg: Config, ex: dict, model: dict) -> None:
    cfg.explorer_enabled = _bool(ex, "enabled", cfg.explorer_enabled)
    cfg.explorer_model = _model(model, cfg.explorer_model)
    cfg.explorer_max_steps = _int(ex, "max_steps", cfg.explorer_max_steps, 1, 200)
    cfg.explorer_max_queries = _int(ex, "max_queries", cfg.explorer_max_queries, 1, 500)
    cfg.explorer_max_minutes = _int(ex, "max_minutes", cfg.explorer_max_minutes, 1, 480)
    cfg.explorer_query_timeout_s = _int(ex, "query_timeout_s", cfg.explorer_query_timeout_s, 1, 60)
    cfg.explorer_max_rows = _int(ex, "max_rows", cfg.explorer_max_rows, 1, 2000)
    cfg.explorer_max_result_kb = _int(ex, "max_result_kb", cfg.explorer_max_result_kb, 4, 4096)
    cfg.explorer_max_join_size = _int(ex, "max_join_size", cfg.explorer_max_join_size, 1000, 2_000_000_000)
    cfg.explorer_sample_rows = _int(ex, "sample_rows", cfg.explorer_sample_rows, 100, 10_000_000)
    cfg.explorer_report_num_predict = _int(ex, "report_num_predict", cfg.explorer_report_num_predict, 300, 4096)
    cfg.explorer_decide_num_predict = _int(ex, "decide_num_predict", cfg.explorer_decide_num_predict, 128, 2048)
    cfg.explorer_interpret_num_predict = _int(ex, "interpret_num_predict", cfg.explorer_interpret_num_predict, 128, 4096)
    cfg.explorer_repair_timeout_s = _int(ex, "repair_timeout_s", cfg.explorer_repair_timeout_s, 10, 600)
    cfg.explorer_repair_budget_s = _int(ex, "repair_budget_s", cfg.explorer_repair_budget_s, 0, 3600)
    cfg.explorer_repair_num_predict = _int(ex, "repair_num_predict", cfg.explorer_repair_num_predict, 64, 2048)
    conns = ex.get("connections", [])
    if not isinstance(conns, list) or len(conns) > 10:
        raise ConfigError("explorer.connections debe ser una lista (máximo 10)")
    seen = set()
    for c in conns:
        if not isinstance(c, dict):
            raise ConfigError("Conexión del explorador inválida")
        name = _str(c, "name", "")
        if not CONN_NAME_RE.match(name) or name in seen:
            raise ConfigError(f"Nombre de conexión inválido o repetido: {name!r}")
        seen.add(name)
        database = _str(c, "database", "")
        if not DB_NAME_RE.match(database) or database.lower() in ("mysql", "information_schema",
                                                                   "performance_schema", "sys"):
            raise ConfigError(f"{name}: base de datos inválida o del sistema: {database!r}")
        user = _str(c, "user", "")
        if not SQL_USER_RE.match(user) or user.lower() == "root":
            raise ConfigError(f"{name}: usuario SQL inválido (nunca root): {user!r}")
        pw = _str(c, "password_file", "")
        if not os.path.isabs(pw):
            raise ConfigError(f"{name}: password_file debe ser una ruta absoluta")
        host = c.get("host", "127.0.0.1")
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ConfigError(f"{name}: solo se admiten servidores MariaDB locales (127.0.0.1 o socket)")
        sock = c.get("socket", "")
        if sock and (not isinstance(sock, str) or not os.path.isabs(sock)):
            raise ConfigError(f"{name}: socket debe ser una ruta absoluta")
        cfg.explorer_connections.append(ExplorerConnection(
            name=name, database=database, user=user, password_file=pw, host=host,
            port=_int(c, "port", 3306, 1, 65535), socket=sock or ""))
