"""Validación, reparación de argumentos y contraste de afirmaciones del Explorador.

Funciones puras (sin E/S) que rodean al modelo con infraestructura, sin decidir por él:
* normalize_step: acepta argumentos planos o anidados («argumentos», «args», «parametros»),
  resuelve nombres de tablas y columnas contra la estructura observada (mayúsculas, comillas,
  prefijo de base, parecido) y explica con precisión lo que falta.
* precheck_sql: problemas detectables ANTES de ejecutar (base equivocada en information_schema,
  tablas inexistentes, otras bases). Se devuelven al modelo para que reformule sin gastar un paso.
* usefulness: distingue ejecución exitosa de resultado útil.
* verify_claim: contrasta una afirmación con su evidencia real (estado, filas, cifras y, si afirma algo
  ACTUAL o vigente, la antigüedad de los registros).
* temporal_note: distingue registros históricos de actuales en un resultado.
"""
from __future__ import annotations

import difflib
import json
import re
import time

TOOLS = ("muestra", "perfil", "frecuentes", "relacion", "sql", "terminar")
REQUIRED = {
    "muestra": ("tabla",),
    "perfil": ("tabla",),
    "frecuentes": ("tabla", "columna"),
    "relacion": ("tabla", "columna", "tabla_destino", "columna_destino"),
    "sql": ("sql",),
    "terminar": (),
}
ARG_KEYS = ("tabla", "columna", "tabla_destino", "columna_destino", "sql")
# Argumentos que cada herramienta usa (los demás se ignoran: no pueden invalidar el paso)
TOOL_ARGS = {
    "muestra": ("tabla",),
    "perfil": ("tabla", "columna"),
    "frecuentes": ("tabla", "columna"),
    "relacion": ("tabla", "columna", "tabla_destino", "columna_destino"),
    "sql": ("sql",),
    "terminar": (),
}
# Textos de relleno que el modelo escribe cuando un campo «no aplica»: la salida estructurada declara los
# argumentos como texto y no puede emitir un JSON null, así que aparece la palabra "null".
PLACEHOLDERS = {"null", "none", "nil", "undefined", "nan", "n/a", "na", "-", "--", "—", "?", "ninguna", "ninguno",
                "nada", "vacio", "vacío", "no aplica", "sin tabla", "sin columna", "<tabla>", "<columna>", "tabla",
                "columna", "nombre", "string", "...", "…"}
NESTED_KEYS = ("argumentos", "args", "parametros", "parámetros", "arguments", "params", "input")
ALIASES = {"table": "tabla", "column": "columna", "target_table": "tabla_destino", "target_column": "columna_destino",
           "tabla_origen": "tabla", "columna_origen": "columna", "query": "sql", "consulta": "sql"}
METADATA_RE = re.compile(r"\binformation_schema\b|^\s*(show|describe|desc|explain)\b", re.I)
NUM_RE = re.compile(r"(?<![\w.])-?\d[\d.,]*\d|(?<![\w.])\d{2,}")


def _clean_ident(v) -> str:
    s = str(v or "").strip().strip("`'\"[]").strip()
    return s


def is_placeholder(v, real_names: set | None = None) -> bool:
    """True si el valor equivale a «sin valor» (None, vacío o un texto de relleno como "null").

    real_names: nombres existentes (en minúsculas); un nombre real nunca se considera relleno.
    """
    if v is None:
        return True
    if isinstance(v, (dict, list)):
        return not v
    s = _clean_ident(v).lower()
    if s and real_names and s in real_names:
        return False
    return not s or s in PLACEHOLDERS


class StepProblem(ValueError):
    """Problema reparable de una respuesta del modelo (no consume un paso).

    field: argumento afectado; received: lo que llegó (para que la reparación lo cite con precisión);
    kind: falta_argumento | tabla_inexistente | columna_inexistente | otra_base | herramienta | pregunta | formato.
    """

    def __init__(self, msg: str, kind: str = "formato", field: str | None = None, received=None):
        super().__init__(msg)
        self.kind, self.field, self.received = kind, field, received

    @property
    def signature(self) -> str:
        """Identidad del problema para detectar repeticiones (mismo error, mismo campo, mismo valor)."""
        return f"{self.kind}|{self.field}|{str(self.received)[:80].lower()}"


def resolve_table(name: str, tables: dict, database: str) -> tuple[str | None, str]:
    """Devuelve (tabla_real, nota). nota no vacía = corrección aplicada o motivo del fallo."""
    raw = _clean_ident(name)
    if not raw:
        return None, "falta el nombre de la tabla"
    if raw.lower() in PLACEHOLDERS and raw.lower() not in {t.lower() for t in tables}:
        return None, f"falta el nombre de la tabla (llegó el texto «{raw}», que no es un nombre)"
    if "." in raw:
        db, _, rest = raw.partition(".")
        if db.strip("`").lower() != database.lower():
            return None, f"«{raw}» apunta a otra base; solo existe la base conectada «{database}»"
        raw = _clean_ident(rest)
    if raw in tables:
        return raw, ""
    lower = {t.lower(): t for t in tables}
    if raw.lower() in lower:
        return lower[raw.lower()], f"se corrigió «{raw}» → «{lower[raw.lower()]}» (mayúsculas)"
    close = difflib.get_close_matches(raw.lower(), list(lower), n=2, cutoff=0.85)
    if len(close) == 1:
        return lower[close[0]], f"se corrigió «{raw}» → «{lower[close[0]]}» (nombre parecido)"
    return None, f"la tabla «{raw}» no existe"


def resolve_column(table: str, name: str, tables: dict) -> tuple[str | None, str]:
    raw = _clean_ident(name)
    if not raw:
        return None, "falta el nombre de la columna"
    if "." in raw:
        raw = raw.rsplit(".", 1)[1]
    cols = [c["nombre"] for c in tables[table]["columnas"]]
    if raw in cols:
        return raw, ""
    lower = {c.lower(): c for c in cols}
    if raw.lower() in lower:
        return lower[raw.lower()], f"se corrigió «{raw}» → «{lower[raw.lower()]}» (mayúsculas)"
    close = difflib.get_close_matches(raw.lower(), list(lower), n=2, cutoff=0.85)
    if len(close) == 1:
        return lower[close[0]], f"se corrigió «{raw}» → «{lower[close[0]]}» (nombre parecido)"
    return None, f"la columna «{raw}» no existe en {table}"


def normalize_step(d, tables: dict, database: str) -> tuple[dict, list[str]]:
    """Devuelve (paso_normalizado, correcciones). Lanza StepProblem con un mensaje reparable.

    Solo se toman y validan los argumentos que usa la herramienta elegida; los textos de relleno («null»,
    «None», «n/a»…) cuentan como ausentes. Nunca se elige una tabla en nombre del modelo.
    """
    if not isinstance(d, dict):
        raise StepProblem("la respuesta no fue un objeto JSON válido", "formato")
    tool = str(d.get("herramienta", "")).strip().lower()
    if tool not in TOOLS:
        raise StepProblem(f"«herramienta» debe ser una de: {', '.join(TOOLS)} (se recibió «{tool or 'nada'}»)",
                          "herramienta", "herramienta", tool)
    if is_placeholder(d.get("pregunta")):
        raise StepProblem("falta «pregunta»: escribí qué querés averiguar con este paso", "pregunta", "pregunta",
                          d.get("pregunta"))
    real = {t.lower() for t in tables} | {c["nombre"].lower() for t in tables.values() for c in t["columnas"]}
    raw_args, placeholders = {}, {}
    for src in [d] + [d[k] for k in NESTED_KEYS if isinstance(d.get(k), dict)]:
        for k, v in src.items():
            key = ALIASES.get(k, k)
            if key not in ARG_KEYS or key in raw_args or isinstance(v, (dict, list)):
                continue
            if is_placeholder(v, real):
                if v is not None and str(v).strip():
                    placeholders.setdefault(key, str(v).strip()[:40])
                continue
            raw_args[key] = str(v)[:4000]
    if "tabla" in raw_args and "columna" not in raw_args and "." in _clean_ident(raw_args["tabla"]):
        t, _, c = _clean_ident(raw_args["tabla"]).rpartition(".")
        if t in tables or t.lower() in {x.lower() for x in tables}:
            raw_args["tabla"], raw_args["columna"] = t, c
    used = TOOL_ARGS[tool]
    args = {k: v for k, v in raw_args.items() if k in used}
    notes = []
    ignored = sorted(k for k in raw_args if k not in used)
    if ignored:
        notes.append(f"se ignoraron argumentos que «{tool}» no usa: {', '.join(ignored)}")
    missing = [k for k in REQUIRED[tool] if not args.get(k)]
    if missing:
        field = missing[0]
        got = placeholders.get(field)
        detail = (f"«{field}» llegó como el texto «{got}», que no es un valor" if got
                  else f"faltó «{field}»")
        raise StepProblem(f"la herramienta «{tool}» necesita: {', '.join(REQUIRED[tool])}; {detail}",
                          "falta_argumento", field, got)
    for tk, ck in (("tabla", "columna"), ("tabla_destino", "columna_destino")):
        if tk in args:
            real, note = resolve_table(args[tk], tables, database)
            if real is None:
                raise StepProblem(note, "otra_base" if "otra base" in note else "tabla_inexistente", tk, args[tk])
            if note:
                notes.append(note)
            args[tk] = real
            if args.get(ck):
                col, note = resolve_column(real, args[ck], tables)
                if col is None:
                    raise StepProblem(note, "columna_inexistente", ck, args[ck])
                if note:
                    notes.append(note)
                args[ck] = col
    out = {k: v for k, v in d.items() if k not in ARG_KEYS and k not in NESTED_KEYS}
    out["herramienta"] = tool
    out.update(args)
    return out, notes


def _string_literals(sql: str) -> list[str]:
    return [m.group(2) for m in re.finditer(r"(['\"])((?:\\.|(?!\1).)*)\1", sql)]


def precheck_sql(sql: str, tables: dict, database: str) -> str:
    """Problemas evidentes antes de ejecutar (texto vacío = sin problemas detectados)."""
    low = sql.lower()
    if "information_schema" in low:
        for m in re.finditer(r"(table_schema|schema_name)\s*(=|like|in\s*\()\s*(['\"])([^'\"]*)\3", sql, re.I):
            if m.group(4) != database:
                return (f"la consulta filtra {m.group(1)} = '{m.group(4)}', pero la base conectada es «{database}». "
                        f"Usá {m.group(1)} = DATABASE(). Además, para contar filas reales usá perfil(tabla) o "
                        "SELECT COUNT(*) FROM tabla: TABLE_ROWS de information_schema es solo una estimación")
    ctes = {m.group(1).lower() for m in re.finditer(r"(?i)(?:\bwith\b|,)\s*`?(\w+)`?\s+as\s*\(", sql)}
    lower_tables = {t.lower() for t in tables}
    unknown, other_db = [], []
    for m in re.finditer(r"(?i)\b(?:from|join)\s+((?:`[^`]+`|\w+)(?:\s*\.\s*(?:`[^`]+`|\w+))?)", sql):
        ref = m.group(1).replace("`", "").replace(" ", "")
        if "." in ref:
            db, _, t = ref.partition(".")
            if db.lower() == "information_schema":
                continue
            if db.lower() != database.lower():
                other_db.append(ref)
                continue
            ref = t
        if ref.lower() in ("dual", "seq") or ref.lower().startswith("seq_"):
            continue
        if ref.lower() not in lower_tables and ref.lower() not in ctes:
            unknown.append(ref)
    if other_db:
        return f"solo podés consultar la base conectada «{database}»; se referenció: {', '.join(sorted(set(other_db)))}"
    if unknown:
        return (f"tabla(s) inexistente(s): {', '.join(sorted(set(unknown)))}. "
                f"Tablas disponibles: {', '.join(sorted(tables))}")
    return ""


def is_metadata(sql: str) -> bool:
    return bool(METADATA_RE.search(sql or ""))


def usefulness(status: str, row_count: int, rows: list, sql: str) -> str:
    """ejecutada ≠ útil: error | rechazada | vacia | solo_nulos | metadatos | datos."""
    if status != "ok":
        return "rechazada" if status in ("rechazada", "bloqueada") else "error"
    if not row_count:
        return "vacia"
    if all(all(v is None for v in r) for r in rows or []):
        return "solo_nulos"
    if is_metadata(sql):
        return "metadatos"
    return "datos"


def _norm_num(s: str) -> set[str]:
    """Variantes comparables de una cifra (1.234,5 / 1234.5 / 1234,50…), con y sin signo."""
    s = s.strip().lstrip("+")
    out = {s}
    if s.startswith("-"):
        out |= _norm_num(s[1:])  # «-05» de «2024-05-01» también es «05»
    digits = s.replace(",", "").replace(".", "")
    out.add(digits)
    for cand in (s.replace(",", ""), s.replace(".", "").replace(",", "."), s.replace(",", ".")):
        try:
            f = float(cand)
            out.add(repr(f).rstrip("0").rstrip("."))
            out.add(str(int(f)) if f == int(f) else repr(f))
        except ValueError:
            pass
    return {x for x in out if x}


CODE_RE = re.compile(r"(?<![\w.])([A-Z][A-Z0-9_]{1,15})(?![\w])")
QUOTED_RE = re.compile(r"[«\"'`“‘]([^«»\"'`“”‘’]{1,40})[»\"'`”’]")
GENERIC_CODES = {"SQL", "NULL", "ID", "PK", "FK", "JSON", "CSV", "API", "UTC", "OK", "SELECT", "FROM", "WHERE",
                 "COUNT", "AVG", "MIN", "MAX", "SUM", "LIMIT", "AND", "OR", "NOT", "DISTINCT", "GROUP", "ORDER",
                 "BY", "JOIN", "ON", "AS", "IS", "IN", "DESC", "ASC", "TRUE", "FALSE", "HTML", "URL", "UUID"}


def _literals_at(text: str) -> list[tuple[str, int]]:
    """(literal, posición): textos entre comillas y códigos EN MAYÚSCULAS, sin repetir."""
    found = [(m.group(1).strip(), m.start()) for m in QUOTED_RE.finditer(text or "")]
    found += [(m.group(1), m.start()) for m in CODE_RE.finditer(text or "")
              if m.group(1) not in GENERIC_CODES and not re.fullmatch(r"[CF]\d+", m.group(1))]
    out, seen = [], set()
    for v, pos in sorted(found, key=lambda x: x[1]):
        if v and v not in seen:
            seen.add(v)
            out.append((v, pos))
    return out


def literal_values(text: str) -> list[str]:
    """Literales de la afirmación: códigos EN MAYÚSCULAS y textos entre comillas (valores o nombres)."""
    return [v for v, _ in _literals_at(text)]


def _fold(s: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFKD", str(s).lower()) if not unicodedata.combining(c))


IDENT_RE = re.compile(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?")
VALUE_CTX_RE = re.compile(r"\b(?:el|un|los|unos|del|al)\s+valor(?:es)?\s*$|\bvalor(?:es)?\s*$")
TERMINAL_RE = re.compile(r"[.!?…»\"'”’)\]]\s*$")


def is_truncated(text: str, limit: int | None) -> bool:
    """¿El texto llegó cortado por un límite de longitud? (ocupa el límite y no termina una frase)."""
    t = " ".join(str(text or "").split())
    return bool(limit) and len(t) >= limit - 3 and not TERMINAL_RE.search(t)


def classify_literals(text: str, evidence: list[dict], names: set | None) -> dict:
    """Separa los literales de una afirmación en VALORES de datos, IDENTIFICADORES SQL y ALIAS (0.4.7).

    * valor: aparece en las CELDAS del resultado citado (o en el texto de un hecho citado), nunca en los encabezados.
    * alias: nombre de una columna del resultado o un «AS x» del SQL citado: es un nombre, no un dato.
    * identificador: nombre de tabla o columna del esquema que figura en el SQL citado (o en un hecho citado).
    Un literal presentado como valor («el valor «x»») solo puede ser un valor. Devuelve listas y los problemas:
    «ausentes» (ni valor ni nombre) y «ajenos» (nombres del esquema que la evidencia citada no usa).
    """
    cells = " ".join(e.get("celdas", e.get("texto", "")) for e in evidence).lower()
    cells_f = _fold(cells)
    alias, sql_words, fact_words = set(), set(), set()
    for e in evidence:
        if e.get("tipo") == "consulta":
            alias |= {_fold(c) for c in e.get("columnas") or []}
            sql = _fold(e.get("sql") or "").replace("`", "")
            alias |= set(re.findall(r"\bas\s+([a-z_][a-z0-9_]*)", sql))
            sql_words |= set(re.findall(r"[a-z_][a-z0-9_]*", sql))
        else:
            fact_words |= set(re.findall(r"[a-z_][a-z0-9_]*", _fold(e.get("texto", ""))))
    out = {"valores": [], "identificadores": [], "alias": [], "ausentes": [], "ajenos": []}
    for v, pos in _literals_at(text):
        fv = _fold(v).strip("` ").strip()
        as_value = bool(VALUE_CTX_RE.search(_fold((text or "")[:pos]).rstrip()))
        if v.lower() in cells or fv in cells_f:
            out["valores"].append(v)
        elif not as_value and fv in alias:
            out["alias"].append(v)
        elif not as_value and names and IDENT_RE.fullmatch(fv) and all(p in names for p in fv.split(".")):
            parts = fv.split(".")
            if all(p in sql_words or p in alias or p in fact_words for p in parts):
                out["identificadores"].append(v)
            else:
                out["ajenos"].append(v)
        else:
            out["ausentes"].append(v)
    return out


TS_RE = re.compile(r"(?<!\d)((?:19|20)\d\d)-([01]\d)-([0-3]\d)(?:[ T]([0-2]\d):([0-5]\d)(?::([0-5]\d))?)?(?!\d)")
CURRENT_RE = re.compile(r"\b(actual(?:es|mente)?|vigentes?|ahora|hoy|en este momento|en tiempo real|hoy en d[ií]a|"
                        r"current(?:ly)?|now)\b", re.I)
FRESH_S = 3600.0  # un registro más viejo que esto no demuestra el estado actual


def timestamps(text: str) -> list[float]:
    """Fechas y horas (ISO, sin zona: se toma la hora local del servidor) presentes en un texto."""
    out = []
    for m in TS_RE.finditer(text or ""):
        y, mo, d, h, mi, se = (int(x) if x else 0 for x in m.groups())
        try:
            out.append(time.mktime((y, mo, d, h, mi, se, 0, 0, -1)))
        except (OverflowError, ValueError):
            continue
    return out


def temporal_note(rows: list, now: float | None = None) -> str:
    """Rango de fechas de un resultado y advertencia histórico/actual (genérica, sin conocer la base)."""
    now = time.time() if now is None else now
    ts = timestamps(" ".join(str(v) for r in rows or [] for v in r if v is not None))
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    if not ts:
        return (f"FECHAS: el resultado no contiene fechas. Fecha y hora actual: {stamp}. Sin fechas no se puede afirmar "
                "que un valor sea el ACTUAL o vigente.")
    lo, hi = min(ts), max(ts)
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))  # noqa: E731
    age = now - hi
    rel = ("de la última hora" if age <= FRESH_S else
           f"de hace {age / 86400:.0f} días" if age >= 86400 else f"de hace {age / 3600:.0f} horas")
    return (f"FECHAS: los valores de fecha del resultado van de {fmt(lo)} a {fmt(hi)} (el más reciente es {rel}). "
            f"Fecha y hora actual: {stamp}. Son registros HISTÓRICOS: describen lo que se registró en esas fechas; "
            "no demuestran el estado ACTUAL salvo que sean de la última hora.")


def verify_claim(text: str, evidence: list[dict], now: float | None = None, names: set | None = None,
                 out: dict | None = None) -> tuple[str, str, str]:
    """Contrasta una afirmación con su evidencia.

    evidence: [{"id": "C7", "tipo": "consulta", "status": ..., "row_count": n, "utilidad": ..., "texto": resultado,
                "celdas": solo valores, "columnas": encabezados, "sql": consulta}]
              o [{"id": "F3", "tipo": "hecho", "certainty": ..., "texto": ...}]
    names: nombres del esquema (tablas y columnas, en minúsculas sin acentos) para distinguir identificadores SQL de
    valores. out: si se pasa, recibe la clasificación de los literales.
    Devuelve (certeza, veredicto, detalle): certeza «inferencia» si está respaldada, si no «sin_verificar».
    """
    if not evidence:
        return "sin_verificar", "sin_evidencia", "no cita ninguna consulta ni hecho existente"
    usable = [e for e in evidence
              if (e["tipo"] == "consulta" and e.get("utilidad") in ("datos", "metadatos"))
              or (e["tipo"] == "hecho" and e.get("certainty") in ("comprobado", "inferencia"))]
    if not usable:
        states = ", ".join(f"{e['id']}: {e.get('utilidad') or e.get('certainty')}" for e in evidence)
        return "sin_verificar", "evidencia_vacia", f"la evidencia citada no aporta datos ({states})"
    numbers = [n for n in NUM_RE.findall(text or "") if len(re.sub(r"\D", "", n)) >= 2]
    if numbers:
        # Las cifras se buscan en los DATOS (celdas), no en los encabezados ni alias del resultado.
        haystack = " ".join(e.get("celdas", e.get("texto", "")) for e in usable)
        tokens, params = set(), set()
        for tok in re.findall(r"-?\d[\d.,]*", haystack):
            tokens |= _norm_num(tok.rstrip(".,"))
        # Una cifra del SQL citado (un LIMIT, un alias como p95) es un PARÁMETRO de la consulta, no un dato.
        for tok in re.findall(r"-?\d[\d.,]*", " ".join(e.get("sql") or "" for e in usable if e["tipo"] == "consulta")):
            params |= _norm_num(tok.rstrip(".,"))
        missing = [n for n in numbers if not (_norm_num(n) & tokens)]
        if out is not None and missing:
            out["parametros"] = [n for n in missing if _norm_num(n) & params]
        missing = [n for n in missing if not (_norm_num(n) & params)]
        if missing:
            return ("sin_verificar", "cifras_no_encontradas",
                    f"cifras que no aparecen en la evidencia citada: {', '.join(missing[:5])}")
    lits = classify_literals(text, usable, names)
    if out is not None:
        out.update({k: v for k, v in lits.items() if v})
    if lits["ajenos"]:
        return ("sin_verificar", "identificadores_ajenos",
                f"nombres del esquema que la evidencia citada no usa: {', '.join(lits['ajenos'][:5])}")
    if lits["ausentes"]:
        return ("sin_verificar", "valores_no_encontrados",
                f"valores que no aparecen en los datos de la evidencia citada: {', '.join(lits['ausentes'][:5])}")
    if CURRENT_RE.search(text or ""):
        ts = timestamps(" ".join(e.get("texto", "") for e in usable if e["tipo"] == "consulta"))
        now = time.time() if now is None else now
        if not ts:
            return ("sin_verificar", "vigencia_no_demostrada",
                    "afirma algo ACTUAL o vigente, pero la evidencia no tiene fechas que lo demuestren")
        if now - max(ts) > FRESH_S:
            return ("sin_verificar", "vigencia_no_demostrada",
                    "afirma algo ACTUAL o vigente, pero el registro más reciente de la evidencia es del "
                    + time.strftime("%Y-%m-%d %H:%M", time.localtime(max(ts))) + " (histórico)")
    return "inferencia", "respaldada", "contrastada con " + ", ".join(e["id"] for e in usable[:5])


def evidence_cells(result_json: str | None) -> tuple[str, list[str]]:
    """(valores de las celdas, encabezados) de un resultado registrado."""
    try:
        r = json.loads(result_json or "null") or {}
    except ValueError:
        return "", []
    return " ".join(str(v) for row in r.get("rows") or [] for v in row), [str(c) for c in r.get("columns") or []]


def evidence_text(result_json: str | None) -> str:
    try:
        r = json.loads(result_json or "null") or {}
    except ValueError:
        return ""
    rows = r.get("rows") or []
    return " ".join(str(v) for row in rows for v in row) + " " + " ".join(r.get("columns") or [])


def salvage_json(text: str, max_tries: int = 400) -> dict | None:
    """Recupera el objeto JSON más largo posible de una respuesta truncada por el límite de generación.

    Recorre el texto llevando la pila de llaves y corchetes; prueba cortes justo después de un valor completo
    (cierre de cadena, objeto o arreglo), desde el final hacia atrás, cerrando lo que quedó abierto. Nunca se
    completa un texto a medio escribir ni se inventa contenido: lo que no llegó a generarse queda ausente (p. ej. un
    hallazgo sin «evidencia», que luego se contrasta contra la consulta interpretada).
    """
    start = text.find("{")
    if start < 0:
        return None
    s = text[start:]
    stack, cuts = [], []
    in_str = esc = False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
                cuts.append((i + 1, "".join(reversed(stack))))
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack:
                break
            stack.pop()
            cuts.append((i + 1, "".join(reversed(stack))))
            if not stack:
                break
    for pos, closers in reversed(cuts[-max_tries:]):
        try:
            obj = json.loads(s[:pos] + closers)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def columns_named(text: str, columns: list[str]) -> list[str]:
    """Columnas (de la lista dada) que el texto nombra literalmente, en orden de aparición.

    Coincidencia exacta de palabra, sin distinguir mayúsculas ni acentos («símbolo» no es «symbol»: no se adivina
    por parecido). Sirve para reconocer una elección que el propio modelo ya escribió.
    """
    import unicodedata

    def fold(s: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFKD", str(s).lower()) if not unicodedata.combining(c))

    words = re.findall(r"[a-z_][a-z0-9_]*", fold(text or "").replace("`", " "))
    by = {fold(c): c for c in columns}
    out = []
    for w in words:
        if w in by and by[w] not in out:
            out.append(by[w])
    return out
