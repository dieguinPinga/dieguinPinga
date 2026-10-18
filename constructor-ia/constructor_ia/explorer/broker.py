"""Intermediario entre el agente y MariaDB («la linterna»).

El agente nunca ve credenciales ni abre conexiones: propone una consulta y el
intermediario decide si se ejecuta y con qué límites. Controles, en orden:

1. Verificación de la cuenta (fallo cerrado): `SHOW GRANTS` solo puede contener
   USAGE global y SELECT/SHOW VIEW sobre la base configurada (nombre exacto,
   comodines escapados), sin GRANT OPTION, roles ni proxy; usuario distinto de
   root; sonda inocua (CREATE TEMPORARY TABLE) que debe ser rechazada; y lectura
   de las variables de sesión aplicadas.
2. Por consulta: conexión nueva con `SET SESSION tx_read_only=1,
   max_statement_time, sql_select_limit, max_join_size`; protocolo sin
   multi-sentencias ni LOCAL INFILE; filtro de sentencias (sqlguard).
3. Topes del cliente: tiempo (con KILL QUERY en el servidor), filas, bytes y
   una sola consulta a la vez en todo el proceso.
4. Registro de cada consulta (texto, estado, filas, bytes, tiempo y resumen
   acotado del resultado) en la base de Constructor IA, nunca con secretos.
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, field

from ..config import Config, ExplorerConnection
from ..db import Database
from . import sqlguard
from .mysqlproto import Connection, LimitExceeded, MySQLError

log = logging.getLogger(__name__)
ALLOWED_DB_PRIVS = {"SELECT", "SHOW VIEW"}
_GLOBAL_LOCK = threading.Lock()
SYSTEM_MAX_ROWS = 5000


class BrokerError(Exception):
    pass


@dataclass
class QueryOutcome:
    status: str  # ok | error | rechazada | tiempo | cancelada | bloqueada
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    truncated: bool = False
    truncated_reason: str = ""
    ms: int = 0
    bytes: int = 0
    error: str = ""
    query_id: int | None = None

    def summary(self, max_rows: int = 20, max_cell: int = 80) -> dict:
        def cell(v):
            if isinstance(v, str) and len(v) > max_cell:
                return v[:max_cell] + "…"
            return v
        return {"status": self.status, "columns": self.columns, "row_count": len(self.rows),
                "rows": [[cell(v) for v in r] for r in self.rows[:max_rows]], "truncated": self.truncated,
                "truncated_reason": self.truncated_reason, "error": self.error, "ms": self.ms}


def read_password(path: str) -> str:
    """Lee el archivo de contraseña exigiendo: archivo regular, sin enlaces, propio y con permisos 0600."""
    try:
        st = os.lstat(path)
    except OSError:
        raise BrokerError("No se encontró el archivo de contraseña de la conexión") from None
    if not stat.S_ISREG(st.st_mode):
        raise BrokerError("El archivo de contraseña debe ser un archivo regular (no un enlace)")
    if st.st_uid != os.getuid():
        raise BrokerError("El archivo de contraseña debe pertenecer al usuario del servicio")
    if st.st_mode & 0o077:
        raise BrokerError("El archivo de contraseña debe tener permisos 0600")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as fh:
        pw = fh.read(512).strip("\r\n")
    if not pw:
        raise BrokerError("El archivo de contraseña está vacío")
    return pw


def _redact_grant(line: str) -> str:
    line = re.sub(r"IDENTIFIED BY PASSWORD '[^']*'", "IDENTIFIED BY PASSWORD '[oculto]'", line)
    return re.sub(r"(IDENTIFIED VIA \S+ USING )(?:'[^']*'|\S+)", r"\1'[oculto]'", line)


def _unquote_db(target: str) -> tuple[str, bool]:
    """`db`.* → (nombre sin escapes, tiene_comodines_sin_escapar)."""
    m = re.match(r"^`((?:``|[^`])*)`\.", target)
    if not m:
        return "", True
    raw = m.group(1).replace("``", "`")
    wildcard = bool(re.search(r"(?<!\\)[_%]", raw))
    return raw.replace("\\_", "_").replace("\\%", "%"), wildcard


def check_grants(lines: list[str], database: str) -> list[str]:
    """Devuelve problemas (vacío = privilegios aceptables)."""
    problems = []
    for raw in lines:
        line = _redact_grant(raw)
        m = re.match(r"^GRANT (.+?) ON (\S+) TO ", line)
        if not m:
            problems.append(f"concesión no reconocida (rol o proxy): {line[:120]}")
            continue
        privs_txt, target = m.group(1), m.group(2)
        if "WITH GRANT OPTION" in line.upper():
            problems.append("la cuenta tiene GRANT OPTION")
        if privs_txt.upper().startswith("PROXY"):
            problems.append("la cuenta tiene privilegio PROXY")
            continue
        # Los privilegios por columna «SELECT (a, b)» llevan comas internas: se quitan antes de separar.
        privs = {p.strip().upper() for p in re.sub(r"\([^)]*\)", "", privs_txt).split(",")}
        if target == "*.*":
            if privs != {"USAGE"}:
                problems.append(f"privilegios globales no permitidos: {', '.join(sorted(privs - {'USAGE'}))}")
            continue
        db, wildcard = _unquote_db(target)
        if wildcard:
            problems.append(f"el nombre de base en la concesión tiene comodines sin escapar ({target}); "
                            r"concédalo como `nombre\_con\_escape`")
        if db != database:
            problems.append(f"acceso a otra base además de la configurada: {db or target}")
        extra = privs - ALLOWED_DB_PRIVS
        if extra:
            problems.append(f"privilegios no permitidos sobre {target}: {', '.join(sorted(extra))}")
    return problems


class Broker:
    def __init__(self, cfg: Config, conn: ExplorerConnection, db: Database):
        self.cfg = cfg
        self.conn = conn
        self.db = db
        self.status: dict = {"verified": False, "reason": "verificación pendiente", "checked_at": None}

    @property
    def name(self) -> str:
        return self.conn.name

    def _connect(self, timeout: float = 5.0) -> Connection:
        return Connection(user=self.conn.user, password=read_password(self.conn.password_file),
                          database=self.conn.database, host=self.conn.host, port=self.conn.port,
                          unix_socket=self.conn.socket or None, connect_timeout=timeout)

    def _session_sql(self, max_rows: int | None = None) -> str:
        cfg = self.cfg
        rows = int(max_rows or cfg.explorer_max_rows)
        return (f"SET SESSION tx_read_only=1, max_statement_time={int(cfg.explorer_query_timeout_s)}, "
                f"sql_select_limit={rows + 1}, max_join_size={int(cfg.explorer_max_join_size)}")

    # Verificación (fallo cerrado) -------------------------------------
    def verify(self) -> dict:
        info: dict = {"verified": False, "checked_at": time.time(), "database": self.conn.database,
                      "user": self.conn.user, "endpoint": self.conn.socket or f"{self.conn.host}:{self.conn.port}"}
        try:
            c = self._connect()
        except (BrokerError, MySQLError, OSError, ConnectionError) as e:
            info["reason"] = f"no se pudo conectar: {e}"
            self.status = info
            return info
        try:
            info["server_version"] = c.server_version.replace("5.5.5-", "")
            current = c.query("SELECT CURRENT_USER()").rows[0][0]
            info["current_user"] = current
            if str(current).split("@")[0].lower() == "root":
                raise BrokerError("la conexión usa root: prohibido")
            grants = [r[0] for r in c.query("SHOW GRANTS FOR CURRENT_USER()").rows]
            info["grants"] = [_redact_grant(g) for g in grants]
            problems = check_grants(grants, self.conn.database)
            if problems:
                raise BrokerError("; ".join(problems))
            # Sonda de privilegios ANTES de la sesión de solo lectura: debe rechazarla el sistema de
            # privilegios (acceso denegado), no el modo de solo lectura. Es inocua: una tabla temporal
            # vive solo en esta sesión y no toca datos.
            try:
                c.query("CREATE TEMPORARY TABLE cia_sonda_escritura (x INT)")
                raise BrokerError("la sonda de escritura (tabla temporal) fue aceptada: la cuenta tiene privilegios de más")
            except MySQLError as e:
                if e.code not in (1044, 1142, 1227):
                    raise BrokerError(f"sonda de escritura no concluyente (error {e.code})") from None
                info["write_probe"] = f"rechazada por privilegios ({e.code})"
            c.query(self._session_sql())
            row = c.query("SELECT @@tx_read_only, @@max_statement_time, @@sql_select_limit, @@max_join_size,"
                          " DATABASE()").rows[0]
            info["session"] = {"tx_read_only": row[0], "max_statement_time": row[1], "sql_select_limit": row[2],
                               "max_join_size": row[3], "database": row[4]}
            want = (1, float(self.cfg.explorer_query_timeout_s), self.cfg.explorer_max_rows + 1,
                    self.cfg.explorer_max_join_size)
            got = (int(row[0]), float(row[1]), int(row[2]), int(row[3]))
            if got != want or row[4] != self.conn.database:
                raise BrokerError(f"límites de sesión no aplicados: esperado {want}, obtenido {got}")
            info["verified"] = True
            info["reason"] = ""
        except (BrokerError, MySQLError, LimitExceeded, ConnectionError, OSError) as e:
            info["reason"] = str(e)
        finally:
            c.close()
        self.status = info
        if info["verified"]:
            log.info("Conexión %s verificada (solo lectura)", self.name)
        else:
            log.error("Conexión %s BLOQUEADA: %s", self.name, info["reason"])
        return info

    # Ejecución --------------------------------------------------------
    def run(self, sql: str, *, mission_id: str | None = None, step: int | None = None, origin: str = "agente",
            tool: str = "sql", question: str = "", rationale: str = "", cancel: threading.Event | None = None,
            max_rows: int | None = None) -> QueryOutcome:
        cfg = self.cfg
        # Las consultas de esquema del propio sistema (information_schema) pueden traer más filas.
        ceiling = SYSTEM_MAX_ROWS if origin == "sistema" else cfg.explorer_max_rows
        max_rows = min(max_rows or cfg.explorer_max_rows, ceiling)
        out = QueryOutcome("error", sql=sql)
        try:
            out.sql = sqlguard.check(sql)
        except sqlguard.SqlRejected as e:
            out.status, out.error = "rechazada", str(e)
            return self._record(out, mission_id, step, origin, tool, question, rationale)
        if not self.status.get("verified"):
            out.status, out.error = "bloqueada", "conexión no verificada: " + str(self.status.get("reason"))
            return self._record(out, mission_id, step, origin, tool, question, rationale)
        t0 = time.monotonic()
        with _GLOBAL_LOCK:
            if cancel is not None and cancel.is_set():
                out.status, out.error = "cancelada", "exploración detenida"
                return self._record(out, mission_id, step, origin, tool, question, rationale)
            conn = None
            done = threading.Event()
            try:
                conn = self._connect()
                conn.query(self._session_sql(max_rows), timeout=5)
                tid = conn.thread_id
                if cancel is not None:
                    threading.Thread(target=self._watch_cancel, args=(cancel, done, conn, tid), daemon=True).start()
                byte_cap = cfg.explorer_max_result_kb * 1024 * (4 if origin == "sistema" else 1)
                res = conn.query(out.sql, max_rows=max_rows, max_bytes=byte_cap,
                                 timeout=cfg.explorer_query_timeout_s + 2)
                out.columns = [col.name for col in res.columns]
                out.rows = res.rows
                out.truncated, out.truncated_reason, out.bytes = res.truncated, res.truncated_reason, res.bytes_read
                out.status = "ok"
            except LimitExceeded:
                if cancel is not None and cancel.is_set():
                    out.status, out.error = "cancelada", "exploración detenida"
                else:
                    out.status, out.error = "tiempo", f"la consulta superó {cfg.explorer_query_timeout_s} s"
                self._kill(conn)
            except MySQLError as e:
                if cancel is not None and cancel.is_set():
                    out.status, out.error = "cancelada", "exploración detenida"
                elif e.code in (1969, 3024):  # max_statement_time
                    out.status, out.error = "tiempo", f"el servidor interrumpió la consulta por tiempo ({e.message[:120]})"
                elif e.code == 1104:  # MAX_JOIN_SIZE
                    out.status, out.error = "error", ("consulta demasiado costosa (MAX_JOIN_SIZE): agregá filtros, "
                                                      "LIMIT, usá una muestra o consultá menos tablas a la vez")
                else:
                    out.status, out.error = "error", f"MariaDB {e.code}: {e.message[:300]}"
            except (BrokerError, OSError, ConnectionError) as e:
                if cancel is not None and cancel.is_set():
                    out.status, out.error = "cancelada", "exploración detenida"
                else:
                    out.status, out.error = "error", f"conexión: {e}"
            finally:
                done.set()
                if conn is not None:
                    conn.close()
        out.ms = int((time.monotonic() - t0) * 1000)
        return self._record(out, mission_id, step, origin, tool, question, rationale)

    def _watch_cancel(self, cancel, done, conn, tid):
        while not done.wait(0.2):
            if cancel.is_set():
                conn.abort()  # el cliente se libera ya, aunque el KILL QUERY no llegue
                for _ in range(3):  # limpieza en el servidor (max_statement_time cubre el resto)
                    if self._kill_tid(tid):
                        break
                return

    def _kill(self, conn: Connection | None) -> None:
        if conn is not None:
            self._kill_tid(conn.thread_id)

    def _kill_tid(self, tid: int) -> bool:
        """KILL QUERY sobre el propio hilo (no requiere privilegios adicionales). Devuelve True si se envió."""
        try:
            k = self._connect(timeout=3)
            try:
                k.query(f"KILL QUERY {int(tid)}", timeout=3)
                return True
            finally:
                k.close()
        except MySQLError as e:
            return e.code == 1094  # el hilo ya no existe: nada que interrumpir
        except Exception:  # noqa: BLE001 - mejor esfuerzo; max_statement_time cubre el resto
            return False

    def _record(self, out: QueryOutcome, mission_id, step, origin, tool, question, rationale) -> QueryOutcome:
        summary = out.summary(max_rows=50, max_cell=200)
        cur = self.db.execute(
            "INSERT INTO explore_queries(conn, mission_id, step, origin, tool, question, rationale, sql_text, status,"
            " row_count, bytes, ms, truncated, result_json, error, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.name, mission_id, step, origin, tool, (question or "")[:500], (rationale or "")[:1000],
             out.sql[:4000], out.status, len(out.rows), out.bytes, out.ms, int(out.truncated),
             json.dumps(summary, ensure_ascii=False, default=str)[:60_000], out.error[:1000], time.time()))
        out.query_id = cur.lastrowid
        return out


class BrokerRegistry:
    def __init__(self, cfg: Config, db: Database):
        self.brokers = {c.name: Broker(cfg, c, db) for c in cfg.explorer_connections}

    def get(self, name: str) -> Broker:
        b = self.brokers.get(name)
        if b is None:
            raise BrokerError("Conexión inexistente")
        return b

    def verify_all(self) -> None:
        for b in self.brokers.values():
            b.verify()
