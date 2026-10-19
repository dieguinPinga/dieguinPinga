"""Conocimiento acumulativo del Explorador (v0.4.5): funciones puras, sin E/S.

El modelo elige libremente tablas, preguntas y herramientas. Estas funciones lo ayudan a reconocer:
* si una consulta puede responder lo que su pregunta busca (capabilities, coherence, answer_status);
* si está repitiendo una consulta ya hecha (firma, firma_forma);
* qué alcance tiene la evidencia (scope) y si una afirmación lo excede (claim_scope_check);
* si una afirmación aporta conocimiento nuevo frente al ya registrado (claim_key, claim_values, classify).

Principios: el análisis de SQL es heurístico y ante la duda devuelve «desconocido» (nunca bloquea SQL válido);
la clasificación de aporte es conservadora (si no se puede determinar, «indeterminado», no «nuevo»); dos
observaciones distintas de momentos o alcances distintos no son una contradicción.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from . import validation as V

BUSCA = ("filas", "ejemplos", "valores_distintos", "frecuencias", "rango_nulos", "relacion", "agregado_por_grupo",
         "evolucion_temporal", "otro")
BUSCA_LABEL = {"filas": "cuántas filas hay", "ejemplos": "ejemplos de registros", "valores_distintos": "valores distintos",
               "frecuencias": "frecuencias de valores", "rango_nulos": "rango, promedio o nulos",
               "relacion": "una relación entre tablas", "agregado_por_grupo": "agregados por grupo",
               "evolucion_temporal": "la evolución en el tiempo", "otro": "otra cosa"}
NEEDS_COLUMN = {"valores_distintos", "frecuencias", "rango_nulos"}
HOW = {  # qué herramientas pueden responder cada cosa (información para el modelo, no una elección)
    "filas": "perfil(tabla) o sql con COUNT(*)",
    "ejemplos": "muestra(tabla) o sql con SELECT … LIMIT",
    "valores_distintos": "perfil(tabla, columna), frecuentes(tabla, columna) o sql con COUNT(DISTINCT …)",
    "frecuencias": "frecuentes(tabla, columna) o sql con GROUP BY y COUNT(*)",
    "rango_nulos": "perfil(tabla, columna) o sql con MIN/MAX/AVG",
    "relacion": "relacion(tabla, columna, tabla_destino, columna_destino) o sql con JOIN",
    "agregado_por_grupo": "sql con GROUP BY",
    "evolucion_temporal": "sql con GROUP BY u ORDER BY sobre una columna de fecha",
}
# Aportes que cuentan como conocimiento nuevo (el resto no completa un ciclo con conocimiento nuevo).
KNOWLEDGE = ("nuevo", "amplia", "difiere_alcance", "difiere_tiempo")
SAME_MOMENT_S = 3600.0

_STR_RE = re.compile(r"'(?:''|\\.|[^'])*'|\"(?:\"\"|\\.|[^\"])*\"")
_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/|#[^\n]*", re.S)
QUANT_RE = re.compile(r"\b(todas?|todos|siempre|nunca|ningun[oa]?|ningún|cada|únicamente|unicamente|solamente|solo|sólo|"
                      r"exclusivamente|sin excepci[oó]n|en general|la mayor[ií]a|toda la tabla|la tabla (?:entera|completa)|"
                      r"en la tabla|la tabla (?:tiene|contiene|registra|guarda|almacena))\b|100 ?%", re.I)
# Frases que hablan EXPLÍCITAMENTE de la tabla entera: mandan sobre el alcance que declare el modelo.
TABLE_WIDE_RE = re.compile(r"\b(toda la tabla|la tabla (?:entera|completa)|la totalidad|en su totalidad|"
                           r"(?:el|del) total de (?:los )?(?:registros|filas)|todas las filas|todos los registros|"
                           r"cobertura completa|sin muestreo|analiz[oó] toda|recorri[oó] toda)\b", re.I)
# Aportes que no son conocimiento nuevo (además de confirma, indeterminado, trivial…), 0.4.7.
NOT_NEW = ("reformula", "redondeo_compatible")


def fold(s: str) -> str:
    """Minúsculas sin acentos (para reconocer «duración» como la columna «duracion»)."""
    return "".join(c for c in unicodedata.normalize("NFKD", str(s).lower()) if not unicodedata.combining(c))


# Firma de consultas ----------------------------------------------------------------------------------
def normalize_sql(sql: str) -> str:
    s = _COMMENT_RE.sub(" ", sql or "")
    parts, last = [], 0
    for m in _STR_RE.finditer(s):  # minúsculas fuera de las cadenas; las cadenas se conservan tal cual
        parts.append(s[last:m.start()].lower())
        parts.append(m.group(0))
        last = m.end()
    parts.append(s[last:].lower())
    s = "".join(parts).replace("`", "")
    s = re.sub(r"\s+", " ", s).strip().rstrip(";").strip()
    return re.sub(r"\s*([(),=<>])\s*", r"\1", s)


def firma(sql: str) -> str:
    return hashlib.sha1(normalize_sql(sql).encode()).hexdigest()[:20]


def firma_forma(shape: dict) -> str:
    """Consultas «casi iguales»: misma operación sobre las mismas tablas y columnas."""
    key = json.dumps([shape.get("operacion"), sorted(shape.get("tablas", [])), sorted(shape.get("columnas", [])),
                      bool(shape.get("where"))])
    return hashlib.sha1(key.encode()).hexdigest()[:20]


def result_hash(columns, rows) -> str:
    return hashlib.sha1(json.dumps([list(columns or []), [list(r) for r in rows or []]], default=str).encode()).hexdigest()[:20]


# Límites del SQL ejecutado ---------------------------------------------------------------------------
_LIMIT_RE = re.compile(r"\blimit\s+(\d+)(?:\s*,\s*(\d+))?")


def sql_limits(sql: str) -> dict:
    """LIMIT exterior (recorta el resultado) e interiores (recortan las filas que se analizan: subconsultas, CTE).

    Un LIMIT dentro de paréntesis limita una fuente de filas: si existe, la cobertura completa de la tabla no está
    demostrada por la consulta misma. «LIMIT a, b» cuenta b filas.
    """
    s = _STR_RE.sub("?", normalize_sql(sql or ""))
    depth, depths = 0, []
    for ch in s:
        depth += ch == "("
        depth -= ch == ")"
        depths.append(depth)
    outer, inner = None, []
    for m in _LIMIT_RE.finditer(s):
        n = int(m.group(2) or m.group(1))
        if depths[m.start()] > 0:
            inner.append(n)
        else:
            outer = n
    return {"exterior": outer, "interiores": inner}


# Forma de una consulta -------------------------------------------------------------------------------
def tool_shape(tool: str, args: dict) -> dict:
    """Forma EXACTA de las consultas que genera el sistema para cada herramienta."""
    t, c = args.get("tabla"), args.get("columna")
    base = {"tablas": [t] if t else [], "columnas": [c] if c else [], "where": False, "order": False, "limit": None,
            "group": False, "join": False, "herramienta": tool}
    if tool == "muestra":
        return {**base, "operacion": "muestra", "limit": 10}
    if tool == "perfil":
        return {**base, "operacion": "perfil" if c else "conteo"}
    if tool == "frecuentes":
        return {**base, "operacion": "frecuencias", "group": True}
    if tool == "relacion":
        return {**base, "operacion": "relacion", "tablas": [x for x in (t, args.get("tabla_destino")) if x],
                "columnas": [x for x in (c, args.get("columna_destino")) if x], "join": True}
    return {**base, "operacion": "desconocido"}


def sql_shape(sql: str, tables: dict) -> dict:
    """Forma aproximada de un SQL propio del modelo. Ante la duda, operacion = «desconocido»."""
    s = _STR_RE.sub("?", normalize_sql(sql))
    lower = {fold(t): t for t in tables}
    refs = []
    for m in re.finditer(r"\b(?:from|join)\s+([\w.]+)", s):
        name = m.group(1).split(".")[-1]
        if fold(name) in lower and lower[fold(name)] not in refs:
            refs.append(lower[fold(name)])
    words = set(re.findall(r"[a-z_][a-z0-9_]*", s))
    cols = sorted({c["nombre"] for t in refs for c in tables[t]["columnas"] if fold(c["nombre"]) in words})
    aggs = sorted({a for a in ("count", "sum", "avg", "min", "max") if re.search(rf"\b{a}\(", s)})
    shape = {"tablas": refs, "columnas": cols, "agregados": aggs, "where": " where " in f" {s} ",
             "order": "order by" in s, "group": "group by" in s, "join": " join " in f" {s} ",
             "distinct": "distinct" in s, "limit": None, "herramienta": "sql",
             "subconsulta": s.count("select") > 1}
    lims = sql_limits(sql)
    shape["limit"], shape["limites_interiores"] = lims["exterior"], lims["interiores"]
    if V.is_metadata(sql):
        op = "metadatos"
    elif not refs:
        op = "desconocido"
    elif shape["join"] or re.search(r"\b(in|exists)\s*\(\s*select", s):
        op = "relacion"
    elif shape["group"] and "count" in aggs:
        op = "frecuencias"
    elif shape["group"]:
        op = "agrupado"
    elif "count(distinct" in s or (shape["distinct"] and not aggs):
        op = "distintos"
    elif aggs == ["count"] and re.search(r"count\((\*|1)\)", s):
        op = "conteo"
    elif aggs:
        op = "agregado"
    else:
        op = "muestra" if shape["limit"] is not None else "seleccion"
    shape["operacion"] = op
    return shape


def capabilities(shape: dict, temporal_cols: set | None = None) -> set | None:
    """Qué puede responder una consulta con esa forma. None = no se sabe (no se usa para bloquear)."""
    op = shape.get("operacion")
    caps = {"muestra": {"ejemplos"}, "seleccion": {"ejemplos"}, "conteo": {"filas"},
            "perfil": {"valores_distintos", "rango_nulos", "filas"},
            "frecuencias": {"frecuencias", "valores_distintos", "agregado_por_grupo"},
            "agrupado": {"agregado_por_grupo"}, "distintos": {"valores_distintos"}, "relacion": {"relacion"},
            "metadatos": set()}.get(op)
    if op == "agregado":
        aggs = set(shape.get("agregados", []))
        caps = set()
        if aggs & {"min", "max", "avg"}:
            caps.add("rango_nulos")
        if "count" in aggs:
            caps.add("filas")
    if caps is None:
        return None
    caps = set(caps)
    if temporal_cols and set(shape.get("columnas", [])) & temporal_cols and (shape.get("group") or shape.get("order")):
        caps.add("evolucion_temporal")
    return caps


def coherence(busca: str | None, columna_objetivo: str | None, tool: str, args: dict, shape: dict,
              temporal_cols: set | None = None) -> str:
    """Problema reparable si una herramienta DEL SISTEMA no puede responder lo que la pregunta busca.

    Para sql nunca hay problema: el analizador es heurístico y no se bloquea SQL válido por no entenderlo.
    """
    if not busca or busca == "otro" or busca not in BUSCA or tool in ("sql", "terminar"):
        return ""
    caps = capabilities(shape, temporal_cols) or set()
    desc = {"muestra": "muestra(tabla) solo trae 10 filas de ejemplo", "conteo": "perfil(tabla) sin columna solo cuenta filas",
            "perfil": f"perfil(tabla, {args.get('columna')}) da nulos, distintos, mínimo, máximo y promedio",
            "frecuencias": f"frecuentes(tabla, {args.get('columna')}) da los valores más frecuentes",
            "relacion": "relacion(...) comprueba si los valores existen en la tabla destino"}.get(shape.get("operacion"), tool)
    if busca not in caps:
        return (f"tu pregunta busca {BUSCA_LABEL[busca]}, pero {desc}. Pueden responderlo: {HOW.get(busca, 'sql')}. "
                "Elegí la herramienta que prefieras, o cambiá «busca» si tu pregunta era otra")
    target = V._clean_ident(columna_objetivo) if columna_objetivo and not V.is_placeholder(columna_objetivo) else ""
    if busca in NEEDS_COLUMN and target and args.get("columna") and fold(target) != fold(args["columna"]):
        return (f"tu pregunta es sobre la columna «{target}», pero la herramienta se aplicó a «{args['columna']}». "
                "Corregí la columna o «columna_objetivo»")
    return ""


def answer_status(busca: str | None, shape: dict, utility: str, temporal_cols: set | None = None) -> str:
    """respondida | parcial | no_respondida | indeterminada (si no se sabe, no se presume ni lo uno ni lo otro)."""
    if utility not in ("datos", "metadatos"):
        return "no_respondida"
    if not busca or busca == "otro":
        return "indeterminada"
    caps = capabilities(shape, temporal_cols)
    if caps is None:
        return "indeterminada"
    return "respondida" if busca in caps else "parcial"


# Alcance de la evidencia -----------------------------------------------------------------------------
def scope(shape: dict, row_count: int, truncated: bool, table_rows: dict, analyzed: int | None = None,
          sample_cap: int | None = None, sql: str | None = None) -> dict:
    """Qué parte de la tabla respalda un resultado (0.4.7: según el SQL REALMENTE ejecutado).

    table_rows: {tabla: (n, exacto)}; analyzed: filas que entraron al cálculo, si se conocen; sql: el SQL ejecutado,
    cuyos LIMIT interiores (subconsultas, CTE) recortan las filas analizadas. La cobertura completa se certifica solo
    si está DEMOSTRADA: sin LIMIT interior, o con menos filas analizadas (o un conteo exacto de la tabla menor) que
    ese límite. Si no se puede demostrar, el alcance es una muestra.
    """
    op = shape.get("operacion")
    tabs = shape.get("tablas") or []
    total, exact = table_rows.get(tabs[0], (None, False)) if len(tabs) == 1 else (None, False)
    if sql:
        lims = sql_limits(sql)
        outer, inner = lims["exterior"], lims["interiores"]
    else:  # sin el SQL: la forma, y para las herramientas del sistema su tope de muestra conocido
        outer, inner = shape.get("limit"), list(shape.get("limites_interiores") or [])
        if not inner and sample_cap and shape.get("herramienta") in ("perfil", "frecuentes", "relacion"):
            inner = [sample_cap]
    cap = min(inner) if inner else None
    out = {"operacion": op, "tablas": tabs, "columnas": shape.get("columnas", []), "filas_observadas": row_count,
           "filas_tabla": total, "filas_tabla_exactas": exact, "completo": False, "limite_exterior": outer,
           "limite_interior": cap}
    if analyzed is not None:
        out["filas_analizadas"] = analyzed

    def covered(seen) -> bool:
        """¿Está demostrado que el LIMIT interior no recortó filas?"""
        return cap is None or (seen is not None and seen < cap) or (total is not None and exact and total < cap)

    if op == "metadatos":
        out.update(tipo="metadatos", descripcion="metadatos de la base, no registros")
    elif op in ("muestra", "seleccion"):
        if shape.get("where"):
            out["tipo"] = "subconjunto"
        elif shape.get("order"):
            out["tipo"] = "extremos"
        else:
            out["tipo"] = "muestra_arbitraria"
        unfiltered = not shape.get("where") and not truncated
        if unfiltered and covered(row_count if outer is None else analyzed) and (
                outer is None                                        # sin LIMIT exterior: todo lo que llegó
                or row_count < outer                                 # devolvió menos que el LIMIT: no había más filas
                or (total is not None and exact and row_count >= total)):
            out.update(tipo="tabla_completa", completo=True)
    elif op in ("conteo", "perfil", "frecuencias", "relacion", "agregado", "agrupado", "distintos"):
        if shape.get("where") and shape.get("herramienta") == "sql":
            out["tipo"] = "subconjunto"
        elif op == "relacion" and shape.get("herramienta") != "sql":
            out["tipo"] = "agregado_muestra"  # valores distintos muestreados
        elif covered(analyzed):
            out.update(tipo="agregado_total", completo=not truncated)
        else:
            out["tipo"] = "agregado_muestra"
        if out["tipo"] != "subconjunto" and outer is not None and row_count >= outer and (shape.get("group") or op == "frecuencias"):
            out["grupos_recortados"] = True  # el resultado muestra solo los primeros grupos
    else:
        out.update(tipo="desconocido")
    out["cobertura"] = "demostrada" if out["completo"] else ("no_demostrada" if cap is not None else "parcial")
    out["descripcion"] = describe_scope(out)
    return out


def analyzed_rows(tool: str, has_column: bool, columns, rows, sql: str) -> int | None:
    """Filas que entraron al cálculo de una herramienta del sistema, si el resultado lo demuestra.

    perfil las informa; frecuentes, solo si el resultado muestra TODOS los grupos (menos filas que su LIMIT
    exterior): entonces la suma de «veces» es el total analizado. Si no se puede saber, None (no se presume).
    """
    if not rows:
        return None
    columns = list(columns or [])
    row = dict(zip(columns, rows[0]))
    if tool == "perfil":
        n = row.get("filas") if has_column else row.get("filas_contadas")
        return n if isinstance(n, int) else None
    if tool == "frecuentes" and "veces" in columns:
        outer = sql_limits(sql)["exterior"]
        if outer is not None and len(rows) >= outer:
            return None
        i = columns.index("veces")
        try:
            return sum(int(r[i]) for r in rows)
        except (TypeError, ValueError):
            return None
    return None


def describe_scope(sc: dict) -> str:
    n, total = sc.get("filas_observadas"), sc.get("filas_tabla")
    of = (f" de {'' if sc.get('filas_tabla_exactas') else '~'}{total}" + (f" ({100 * n / total:.2g} %)" if total else "")
          if total else "")
    return {
        "muestra_arbitraria": f"{n} filas{of}: las primeras en el orden físico de la tabla; NO es una muestra aleatoria",
        "extremos": f"{n} filas{of}, ordenadas: son los extremos, no un caso típico",
        "subconjunto": f"{n} filas de un subconjunto filtrado (WHERE): no representa al resto de la tabla",
        "tabla_completa": f"todas las filas de la tabla ({n})",
        "agregado_total": "cálculo sobre la tabla completa" + (f" ({sc['filas_analizadas']} filas)" if sc.get("filas_analizadas") else ""),
        "agregado_muestra": "cálculo sobre una muestra limitada de la tabla, no sobre toda"
                            + (f" (hasta {sc['limite_interior']} filas por un LIMIT interior"
                               + (f", de {'' if sc.get('filas_tabla_exactas') else '~'}{total} filas" if total else "")
                               + ")" if sc.get("limite_interior") else ""),
        "metadatos": "metadatos de la base, no registros",
    }.get(sc.get("tipo"), "alcance no determinado")


def generalizes(text: str) -> bool:
    return bool(QUANT_RE.search(text or ""))


def claim_scope_check(text: str, declared: str | None, sc: dict | None) -> tuple[bool, str]:
    """(dentro_del_alcance, nota). Una afirmación sobre toda la tabla respaldada por una muestra parcial excede la evidencia.

    0.4.7: lo que el TEXTO dice explícitamente («toda la tabla», «la totalidad»…) manda sobre el alcance declarado
    (lo que se guarda es el texto); y si el alcance de la evidencia no se puede determinar, la cobertura completa no
    está demostrada: no se certifica.
    """
    explicit = bool(TABLE_WIDE_RE.search(text or ""))
    wide = explicit or declared == "tabla" or (declared not in ("muestra", "subconjunto") and generalizes(text))
    if not wide:
        return True, ""
    if sc and sc.get("tipo") == "metadatos":
        return True, "alcance: metadatos de la base"
    if not sc or sc.get("tipo") in (None, "desconocido"):
        return False, "habla de la tabla completa, pero el alcance de la evidencia no está determinado"
    if sc.get("completo") and sc.get("tipo") in ("agregado_total", "tabla_completa"):
        return True, ""
    return False, f"habla de la tabla completa, pero la evidencia es: {sc.get('descripcion')}"


def how_to_verify(sc: dict | None) -> str:
    cols = ", ".join((sc or {}).get("columnas") or []) or "las columnas involucradas"
    return f"medirlo sobre la tabla completa (p. ej. perfil o frecuentes de {cols}, o un SQL agregado sin LIMIT)"


# Aporte de una afirmación ----------------------------------------------------------------------------
def claim_values(text: str) -> set[str]:
    out = set()
    for n in V.NUM_RE.findall(text or ""):
        if len(re.sub(r"\D", "", n)) < 1:
            continue
        clean = n.replace(",", "") if n.count(",") and n.count(".") else n.replace(",", ".")
        try:
            f = float(clean)
            out.add(repr(f)[:-2] if repr(f).endswith(".0") else repr(f))
        except ValueError:
            out.add(n)
    out |= {fold(v) for v in V.literal_values(text or "")}
    return out


def _cell_matches(value: str, cell) -> bool:
    if cell is None:
        return False
    c = fold(cell)
    if value == c:
        return True
    try:
        return float(value) == float(str(cell))
    except ValueError:
        return False


def claim_key(text: str, shape: dict | None, tables: dict, columns=None, rows=None) -> str | None:
    """Clave «tablas|columnas» de lo que afirma; None si no se puede determinar (conservador).

    Las columnas salen de los nombres que la afirmación menciona y de las columnas del resultado donde aparecen sus
    valores (si un valor aparece en más de dos columnas, es ambiguo y no se usa).
    """
    shape = shape or {}
    tabs = list(shape.get("tablas") or [])
    words = set(re.findall(r"[a-z_][a-z0-9_]*", fold(text)))
    for t in tables:
        if fold(t) in words and t not in tabs:
            tabs.append(t)
    if not tabs:
        return None
    cols = {c["nombre"] for t in tabs if t in tables for c in tables[t]["columnas"] if fold(c["nombre"]) in words}
    if columns and rows:
        real = {fold(c["nombre"]): c["nombre"] for t in tabs if t in tables for c in tables[t]["columnas"]}
        for v in claim_values(text):
            hits = {columns[i] for r in rows for i, cell in enumerate(r) if i < len(columns) and _cell_matches(v, cell)}
            hits = {real[fold(h)] for h in hits if fold(h) in real}  # alias del resultado (minimo, veces…) no cuentan
            if 0 < len(hits) <= 2:
                cols |= hits
    if not cols and shape.get("operacion") == "conteo":
        return f"{','.join(sorted(tabs))}|"
    if not cols and len(shape.get("columnas") or []) == 1:
        cols = set(shape["columnas"])
    if not cols:
        return None
    return f"{','.join(sorted(tabs))}|{','.join(sorted(cols))}"


def is_trivial(text: str, row_count: int | None, limit: int | None) -> bool:
    """Repite el tamaño del resultado («devolvió 10 filas») sin interpretar nada."""
    vals = claim_values(text)
    if not vals or V.literal_values(text):
        return False
    meta = {str(x) for x in (row_count, limit) if x is not None}
    return vals <= meta and bool(re.search(r"\b(filas|registros|resultados?)\b", fold(text)))


def _numeric(vals) -> set:
    out = set()
    for v in vals:
        try:
            float(v)
            out.add(v)
        except ValueError:
            pass
    return out


def _decimals(v: str) -> int:
    return len(v.split(".")[1]) if "." in v else 0


def rounding_of(v: str, known) -> str | None:
    """Valor conocido del que «v» es un redondeo COMPATIBLE (3.2 de 3.21), o None.

    Compatible no es igual: no demuestra igualdad ni diferencia. Solo se considera si el conocido tiene más decimales.
    """
    try:
        x = float(v)
    except ValueError:
        return None
    d = _decimals(v)
    for k in known:
        try:
            y = float(k)
        except ValueError:
            continue
        if _decimals(k) > d and abs(round(y, d) - x) < 10 ** -(d + 3):
            return k
    return None


def _distinctive(vals: set) -> bool:
    """¿Los valores identifican algo? (un texto, o alguna cifra de más de un dígito)."""
    return any(len(re.sub(r"[^0-9a-z]", "", v)) >= 2 for v in vals)


def classify(new: dict, existing: list[dict], related: list[dict] | None = None) -> tuple[str, int | None, str]:
    """Aporte de una afirmación respaldada frente a hechos con la misma clave.

    new / existing: {"id", "values", "consulta", "tipo", "observadas", "mision", "ts"}. Conservador: sin clave o sin
    valores comparables → «indeterminado». Valores distintos con alcance o momento distintos → «difiere_*», nunca
    «contradice»; solo hay contradicción con el mismo alcance, el mismo momento y evidencia distinta.

    0.4.7: decir lo mismo que un hecho nacido de la MISMA consulta es «reformula» (no es conocimiento nuevo); un valor
    que es el redondeo de uno conocido es «redondeo_compatible» (ni igualdad ni diferencia demostradas). related:
    hechos de las mismas tablas con otra clave; compartir columnas NO basta: solo cuentan si nacieron de la misma
    consulta y la afirmación no trae ningún valor que ellos no tengan.
    """
    if new.get("key") is None:
        return "indeterminado", None, "no se pudo determinar de qué tabla y columna habla"
    vals = set(new.get("values") or ())
    q = new.get("consulta")
    if not existing:
        for r in related or ():
            if q is not None and r.get("consulta") == q and vals and _distinctive(vals) and vals <= set(r.get("values") or ()):
                return "reformula", r["id"], f"dice lo mismo que F{r['id']}, sacado de la misma consulta C{q}"
        return "nuevo", None, "no había conocimiento registrado sobre esto"
    if not vals:
        return "indeterminado", existing[0]["id"], "ya hay conocimiento sobre esto y la afirmación no trae cifras ni valores para comparar"
    known = set().union(*(set(e.get("values") or ()) for e in existing))
    fresh = vals - known
    if not fresh:
        best = max(existing, key=lambda e: len(vals & set(e.get("values") or ())))
        if q is not None and best.get("consulta") == q:
            return "reformula", best["id"], f"dice lo mismo que F{best['id']}, sacado de la misma consulta C{q}"
        return "confirma", best["id"], f"coincide con F{best['id']}"
    rounded = {v: rounding_of(v, known) for v in fresh}
    if all(rounded.values()):
        e = next(x for x in existing if set(x.get("values") or ()) & set(rounded.values()))
        pairs = ", ".join(f"{v} ≈ {k}" for v, k in sorted(rounded.items()))
        return ("redondeo_compatible", e["id"], f"{pairs} (F{e['id']}): compatible por redondeo; no demuestra "
                "igualdad ni diferencia")
    fresh = {v for v, k in rounded.items() if not k}
    for e in sorted(existing, key=lambda e: -(e.get("ts") or 0)):
        if not _numeric(e.get("values") or ()) or not _numeric(fresh):
            continue
        if e.get("consulta") and e.get("consulta") == new.get("consulta"):
            continue  # misma evidencia: es otra faceta del mismo resultado, no una contradicción
        if (e.get("tipo"), e.get("observadas")) != (new.get("tipo"), new.get("observadas")):
            return ("difiere_alcance", e["id"], f"difiere de F{e['id']}, que se observó con otro alcance "
                    f"({e.get('tipo') or 'desconocido'}): no es una contradicción")
        # Mismo momento = misma expedición y poco tiempo entre ambas observaciones. Entre expediciones los datos
        # pueden haber cambiado: nunca se presume contradicción.
        same_moment = bool(e.get("mision") and e.get("mision") == new.get("mision")) and (
            not (e.get("ts") and new.get("ts")) or abs(new["ts"] - e["ts"]) < SAME_MOMENT_S)
        if not same_moment:
            return ("difiere_tiempo", e["id"], f"difiere de F{e['id']}, observado en otro momento: los datos pueden "
                    "haber cambiado; no es una contradicción")
        return "contradice", e["id"], f"mismo alcance y mismo momento que F{e['id']}, con cifras distintas"
    return "amplia", None, "agrega valores nuevos sobre algo ya estudiado"
