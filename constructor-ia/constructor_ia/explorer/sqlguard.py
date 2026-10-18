"""Filtro previo de sentencias del agente (capa SECUNDARIA).

Los controles efectivos son: cuenta SQL con solo SELECT/SHOW VIEW en una base,
sesión `tx_read_only=1`, `max_statement_time`, `max_join_size`, `sql_select_limit`,
sin multi-sentencias ni LOCAL INFILE en el protocolo, y topes del intermediario.
Este filtro existe para rechazar temprano, con un mensaje claro, lo que el
servidor rechazaría igual, y para bloquear construcciones que consumen recursos
o bloquean (SLEEP, BENCHMARK, FOR UPDATE…). Trabaja sobre tokens, no sobre
búsquedas de palabras en el texto: comentarios y literales se eliminan antes.
"""
from __future__ import annotations

import re

MAX_SQL = 4000
ALLOWED_FIRST = {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "VALUES", "TABLE"}
FORBIDDEN_WORDS = {
    "INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE", "UPSERT", "CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME",
    "GRANT", "REVOKE", "SET", "CALL", "DO", "HANDLER", "LOAD", "LOCK", "UNLOCK", "INTO", "OUTFILE", "DUMPFILE",
    "KILL", "SHUTDOWN", "FLUSH", "RESET", "PURGE", "INSTALL", "UNINSTALL", "ANALYZE", "OPTIMIZE", "REPAIR",
    "CHECK", "CHECKSUM", "PREPARE", "EXECUTE", "DEALLOCATE", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT",
    "START", "XA", "USE", "HELP", "BINLOG", "CHANGE", "SIGNAL", "RESIGNAL", "GET", "SHARE",
}
FORBIDDEN_FUNCS = {
    "SLEEP", "BENCHMARK", "GET_LOCK", "RELEASE_LOCK", "RELEASE_ALL_LOCKS", "IS_FREE_LOCK", "IS_USED_LOCK",
    "LOAD_FILE", "MASTER_POS_WAIT", "MASTER_GTID_WAIT", "SYS_EXEC", "SYS_EVAL", "NEXTVAL", "SETVAL", "LASTVAL",
    "LAST_INSERT_ID", "UUID_SHORT",
}
SHOW_ALLOWED = {"TABLES", "FULL", "COLUMNS", "FIELDS", "INDEX", "INDEXES", "KEYS", "CREATE", "TABLE", "DATABASES",
                "SCHEMAS", "VARIABLES", "WARNINGS", "ERRORS", "COLLATION", "CHARACTER", "STATUS"}
TOKEN_RE = re.compile(r"\s+|[A-Za-z_][A-Za-z0-9_$]*|`(?:``|[^`])*`|@@?[A-Za-z0-9_.$`]*|\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
                      r"|<=>|[<>!=]=|<>|\|\||&&|::|.", re.S)


class SqlRejected(ValueError):
    pass


def _strip(sql: str) -> str:
    """Quita comentarios y literales de cadena; rechaza comentarios ejecutables de MariaDB."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("/*", i):
            if sql.startswith(("/*!", "/*M!", "/*m!"), i):
                raise SqlRejected("Los comentarios ejecutables /*! … */ no están permitidos")
            j = sql.find("*/", i + 2)
            if j < 0:
                raise SqlRejected("Comentario sin cerrar")
            out.append(" ")
            i = j + 2
        elif sql.startswith("--", i) and (i + 2 >= n or sql[i + 2] in " \t\r\n"):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif c == "#":
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif c in "'\"":
            j = i + 1
            while j < n:
                if sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise SqlRejected("Cadena sin cerrar")
            out.append(" '' ")
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def tokens(sql: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(_strip(sql)) if not t.isspace()]


def check(sql: str) -> str:
    """Devuelve la sentencia normalizada (sin ';' final) o lanza SqlRejected con el motivo."""
    if not isinstance(sql, str) or not sql.strip():
        raise SqlRejected("Consulta vacía")
    if len(sql) > MAX_SQL:
        raise SqlRejected(f"Consulta demasiado larga (máximo {MAX_SQL} caracteres)")
    if "\x00" in sql:
        raise SqlRejected("Carácter nulo en la consulta")
    sql = sql.strip().rstrip(";").strip()
    toks = tokens(sql)
    if not toks:
        raise SqlRejected("Consulta vacía")
    upper = [t.upper() for t in toks]
    if ";" in toks:
        raise SqlRejected("Solo se permite una sentencia por consulta")
    first = upper[0]
    if first not in ALLOWED_FIRST:
        raise SqlRejected(f"Solo se permiten consultas de lectura (SELECT, WITH, SHOW, DESCRIBE, EXPLAIN); se recibió {toks[0]}")
    if first == "EXPLAIN" and (len(upper) < 2 or upper[1] not in ("SELECT", "WITH", "FORMAT", "EXTENDED", "PARTITIONS")):
        raise SqlRejected("EXPLAIN solo admite SELECT")
    if first == "SHOW" and (len(upper) < 2 or upper[1] not in SHOW_ALLOWED):
        raise SqlRejected(f"SHOW {toks[1] if len(toks) > 1 else ''} no está permitido")
    for i, t in enumerate(upper):
        if t in FORBIDDEN_WORDS and not (first == "SHOW" and t in ("CREATE", "TABLE", "STATUS", "INDEX")):
            raise SqlRejected(f"Palabra no permitida en una consulta de lectura: {toks[i]}")
        if t in FORBIDDEN_FUNCS:
            raise SqlRejected(f"Función no permitida: {toks[i]}")
        if t.startswith("@") and not t.startswith("@@"):
            raise SqlRejected("Las variables de usuario (@var) no están permitidas")
    if "FOR" in upper:
        j = upper.index("FOR")
        if j + 1 < len(upper) and upper[j + 1] in ("UPDATE", "SHARE"):
            raise SqlRejected("FOR UPDATE / FOR SHARE (bloqueos) no están permitidos")
    return sql


def quote_ident(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 64 or "\x00" in name:
        raise SqlRejected("Identificador inválido")
    return "`" + name.replace("`", "``") + "`"
