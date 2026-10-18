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
NESTED_KEYS = ("argumentos", "args", "parametros", "parámetros", "arguments", "params", "input")
ALIASES = {"table": "tabla", "column": "columna", "target_table": "tabla_destino", "target_column": "columna_destino",
           "tabla_origen": "tabla", "columna_origen": "columna", "query": "sql", "consulta": "sql"}
METADATA_RE = re.compile(r"\binformation_schema\b|^\s*(show|describe|desc|explain)\b", re.I)
NUM_RE = re.compile(r"(?<![\w.])-?\d[\d.,]*\d|(?<![\w.])\d{2,}")


def _clean_ident(v) -> str:
    s = str(v or "").strip().strip("`'\"[]").strip()
    return s


class StepProblem(ValueError):
    """Problema reparable de una respuesta del modelo (no consume un paso)."""


def resolve_table(name: str, tables: dict, database: str) -> tuple[str | None, str]:
    """Devuelve (tabla_real, nota). nota no vacía = corrección aplicada o motivo del fallo."""
    raw = _clean_ident(name)
    if not raw:
        return None, "falta el nombre de la tabla"
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
    """Devuelve (paso_normalizado, correcciones). Lanza StepProblem con un mensaje reparable."""
    if not isinstance(d, dict):
        raise StepProblem("la respuesta no fue un objeto JSON válido")
    tool = str(d.get("herramienta", "")).strip().lower()
    if tool not in TOOLS:
        raise StepProblem(f"«herramienta» debe ser una de: {', '.join(TOOLS)} (se recibió «{tool or 'nada'}»)")
    if not str(d.get("pregunta", "")).strip():
        raise StepProblem("falta «pregunta»: escribí qué querés averiguar con este paso")
    args = {}
    for src in [d] + [d[k] for k in NESTED_KEYS if isinstance(d.get(k), dict)]:
        for k, v in src.items():
            key = ALIASES.get(k, k)
            if key in ARG_KEYS and v not in (None, "") and key not in args:
                args[key] = str(v)[:4000]
    if "tabla" in args and "columna" not in args and "." in _clean_ident(args["tabla"]):
        t, _, c = _clean_ident(args["tabla"]).rpartition(".")
        if t in tables or t.lower() in {x.lower() for x in tables}:
            args["tabla"], args["columna"] = t, c
    notes = []
    missing = [k for k in REQUIRED[tool] if not args.get(k)]
    if tool == "perfil" and args.get("columna") == "":
        args.pop("columna")
    if missing:
        raise StepProblem(f"la herramienta «{tool}» necesita: {', '.join(REQUIRED[tool])}; faltó: {', '.join(missing)}. "
                          "Pasá los argumentos como campos del JSON (p. ej. \"tabla\": \"nombre\")")
    for tk, ck in (("tabla", "columna"), ("tabla_destino", "columna_destino")):
        if tk in args and tool != "sql":
            real, note = resolve_table(args[tk], tables, database)
            if real is None:
                raise StepProblem(note)
            if note:
                notes.append(note)
            args[tk] = real
            if args.get(ck):
                col, note = resolve_column(real, args[ck], tables)
                if col is None:
                    raise StepProblem(note)
                if note:
                    notes.append(note)
                args[ck] = col
    out = dict(d)
    out["herramienta"] = tool
    for k in ARG_KEYS:
        out.pop(k, None)
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


def literal_values(text: str) -> list[str]:
    """Valores que la afirmación presenta como datos: códigos EN MAYÚSCULAS y textos entre comillas."""
    vals = [m.group(1) for m in QUOTED_RE.finditer(text or "")]
    vals += [c for c in CODE_RE.findall(text or "") if c not in GENERIC_CODES and not re.fullmatch(r"[CF]\d+", c)]
    return list(dict.fromkeys(v.strip() for v in vals if v.strip()))


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


def verify_claim(text: str, evidence: list[dict], now: float | None = None) -> tuple[str, str, str]:
    """Contrasta una afirmación con su evidencia.

    evidence: [{"id": "C7", "tipo": "consulta", "status": ..., "row_count": n, "utilidad": ..., "texto": resultado}]
              o [{"id": "F3", "tipo": "hecho", "certainty": ..., "texto": ...}]
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
        haystack = " ".join(e.get("texto", "") for e in usable)
        tokens = set()
        for tok in re.findall(r"-?\d[\d.,]*", haystack):
            tokens |= _norm_num(tok.rstrip(".,"))
        missing = [n for n in numbers if not (_norm_num(n) & tokens)]
        if missing:
            return ("sin_verificar", "cifras_no_encontradas",
                    f"cifras que no aparecen en la evidencia citada: {', '.join(missing[:5])}")
    haystack_all = " ".join(e.get("texto", "") for e in usable).lower()
    absent = [v for v in literal_values(text) if v.lower() not in haystack_all]
    if absent:
        return ("sin_verificar", "valores_no_encontrados",
                f"valores que no aparecen en la evidencia citada: {', '.join(absent[:5])}")
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


def evidence_text(result_json: str | None) -> str:
    try:
        r = json.loads(result_json or "null") or {}
    except ValueError:
        return ""
    rows = r.get("rows") or []
    return " ".join(str(v) for row in rows for v in row) + " " + " ".join(r.get("columns") or [])
