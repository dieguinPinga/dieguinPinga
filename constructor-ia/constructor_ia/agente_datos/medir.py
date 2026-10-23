"""Contrato MEDIR del agente de datos: el modelo dice QUÉ medir; el coordinador escribe el SQL.

Extiende la idea del Investigador 0.5 («qué medir sobre qué objetos») a agregados agrupados con filtros sobre
varias tablas. El modelo nunca escribe JOIN: el compilador une las tablas siguiendo SOLO relaciones comprobadas por
el cartógrafo (contención de valores medida) y prefiere el sentido muchos→uno, que no multiplica filas. Si una tabla
solo se alcanza en el sentido uno→muchos, el plan queda marcado («puede multiplicar filas») y el verificador lo mide.

Formato (líneas, una sola propuesta; desde la fase 2 la línea «MEDIR: agregado» ya no se pide):
    MEDIDA: suma lineas.cantidad            (conteo | conteo t.c | distintos t.c | suma | promedio | maximo | minimo;
                                             la medida puede ser el producto de dos columnas: suma t.a * t.b)
    AGRUPAR: t.c[, t.d]                     (opcional, hasta 2)
    FILTRO: t.c desde 2026-03-01 hasta 2026-04-01   (intervalo semiabierto [desde, hasta))
    FILTRO: t.c entre A y B                 (ambos extremos incluidos)
    FILTRO: t.c = valor
    ORDEN: mayor | menor                    (opcional)
    LIMITE: N                               (opcional, 1 a 50)

Los nombres se resuelven solo por coincidencia EXACTA (sin distinguir mayúsculas). Nada se elige por parecido: un
nombre inexistente produce una ACLARACIÓN con las opciones reales, sin ejecutar nada.

Normalizaciones de FORMA (nunca de contenido), cada una registrada en plan["normalizaciones"]:
- «MEDIR: suma t.c» sin una línea MEDIDA se lee como «MEDIDA: suma t.c» (la primera corrida real con Qwen escribió
  así la medida). El contenido pasa por las mismas validaciones; «MEDIR: suma» sin columna sigue siendo inválido, y
  una MEDIR con medida distinta de la MEDIDA es una ambigüedad;
- una columna sin tabla («producto_id») se completa SOLO si está en exactamente una de las tablas elegidas (el
  coordinador da la función que lo resuelve); si no, es una aclaración.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import deque

from ..explorer.mapstore import is_numeric, is_temporal
from ..investigador.engine import sql_literal

FUNCIONES = {"conteo": "COUNT", "distintos": "COUNT", "suma": "SUM", "promedio": "AVG", "maximo": "MAX",
             "minimo": "MIN"}
CAMPOS = ("MEDIR", "MEDIDA", "AGRUPAR", "FILTRO", "ORDEN", "LIMITE")
FECHA_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$")


class Aclaracion(ValueError):
    """El plan no se puede compilar tal como está; el mensaje dice por qué y qué opciones reales hay."""


def _q(ident: str) -> str:
    return "`" + str(ident).replace("`", "``") + "`"


def parsear(texto: str, resolver=None) -> dict:
    """Lee las líneas del contrato. Los campos repetidos (salvo FILTRO) con valores distintos son una ambigüedad.
    resolver(nombre) -> «tabla.columna» completa una columna sin tabla, o lanza Aclaracion."""
    campos: dict = {}
    filtros = []
    for linea in texto.splitlines():
        m = re.match(r"^\s*\**\s*(MEDIR|MEDIDA|AGRUPAR|FILTRO|ORDEN|LIMITE)\s*\**\s*:\s*(.*?)\s*$", linea, re.I)
        if not m:
            continue
        k, v = m.group(1).upper(), m.group(2).strip().strip("`")
        if k == "FILTRO":
            if v:
                filtros.append(v)
            continue
        if k in campos and campos[k] != v:
            raise Aclaracion(f"campo {k} con valores distintos («{campos[k]}» y «{v}»): una sola propuesta")
        campos[k] = v
    normalizaciones = []
    medir = (campos.get("MEDIR") or "").strip()
    if medir and medir.lower() != "agregado":
        if "MEDIDA" in campos and campos["MEDIDA"] != medir:
            raise Aclaracion(f"MEDIR «{medir}» y MEDIDA «{campos['MEDIDA']}» dicen cosas distintas (valores distintos): "
                             "una sola propuesta")
        if "MEDIDA" not in campos:
            _medida(medir)                      # el contenido se valida igual que en MEDIDA
            campos["MEDIDA"] = medir
            normalizaciones.append(f"«MEDIR: {medir}» se leyó como «MEDIDA: {medir}»")
    if "MEDIDA" not in campos:
        raise Aclaracion("falta MEDIDA")

    def obj(x):
        x = x.strip().strip("`")
        if "." not in x and resolver is not None and x:
            completo = resolver(x)
            normalizaciones.append(f"«{x}» se completó como «{completo}» (única columna con ese nombre en las tablas "
                                   "elegidas)")
            x = completo
        return _objeto(x)
    med = _medida(campos["MEDIDA"], obj)
    plan = {"medida": med, "agrupar": [], "filtros": [_filtro(f, obj) for f in filtros], "orden": None,
            "limite": None, "normalizaciones": normalizaciones}
    if campos.get("AGRUPAR"):
        plan["agrupar"] = [obj(x) for x in campos["AGRUPAR"].split(",") if x.strip()]
        if len(plan["agrupar"]) > 2:
            raise Aclaracion("AGRUPAR admite hasta 2 columnas")
    orden = (campos.get("ORDEN") or "").lower()
    if orden:
        if orden not in ("mayor", "menor"):
            raise Aclaracion(f"ORDEN debe ser «mayor» o «menor», no «{orden}»")
        plan["orden"] = orden
    if campos.get("LIMITE"):
        if not campos["LIMITE"].isdigit() or not 1 <= int(campos["LIMITE"]) <= 50:
            raise Aclaracion("LIMITE debe ser un número entre 1 y 50")
        plan["limite"] = int(campos["LIMITE"])
    return plan


def _objeto(x: str) -> tuple[str, str]:
    x = x.strip().strip("`")
    if x.count(".") != 1:
        raise Aclaracion(f"«{x}» debe ser tabla.columna")
    t, c = x.split(".")
    return t.strip("` "), c.strip("` ")


def _medida(v: str, obj=None) -> dict:
    obj = obj or _objeto
    m = re.match(r"^(\w+)\s*(.*)$", v.strip())
    func = (m.group(1) if m else "").lower()
    if func not in FUNCIONES:
        raise Aclaracion(f"función de MEDIDA desconocida «{func}»; opciones: {', '.join(FUNCIONES)}")
    resto = m.group(2).strip()
    cols = [obj(x) for x in resto.split("*")] if resto else []
    if len(cols) > 2:
        raise Aclaracion("la medida admite a lo sumo el producto de dos columnas")
    if func != "conteo" and not cols:
        raise Aclaracion(f"«{func}» necesita una columna")
    if func in ("conteo", "distintos") and len(cols) > 1:
        raise Aclaracion(f"«{func}» admite una sola columna")
    return {"funcion": func, "columnas": cols}


def _filtro(v: str, obj=None) -> dict:
    obj = obj or _objeto
    m = re.match(r"^(\S+)\s+desde\s+(.+?)\s+hasta\s+(.+)$", v, re.I)
    if m:
        return {"objeto": obj(m.group(1)), "tipo": "desde_hasta", "a": m.group(2).strip("'\" "),
                "b": m.group(3).strip("'\" ")}
    m = re.match(r"^(\S+)\s+entre\s+(.+?)\s+y\s+(.+)$", v, re.I)
    if m:
        return {"objeto": obj(m.group(1)), "tipo": "entre", "a": m.group(2).strip("'\" "),
                "b": m.group(3).strip("'\" ")}
    m = re.match(r"^(\S+)\s*=\s*(.+)$", v)
    if m:
        return {"objeto": obj(m.group(1)), "tipo": "igual", "a": m.group(2).strip("'\" ")}
    raise Aclaracion(f"FILTRO no reconocido «{v}»: usá «t.c desde A hasta B», «t.c entre A y B» o «t.c = valor»")


def texto_canonico(plan: dict) -> str:
    """El plan en su forma canónica (lo que se guarda como consulta aprendida, ya normalizado)."""
    med = plan["medida"]
    lineas = [f"MEDIDA: {med['funcion']}" + ("" if not med["columnas"] else " " + " * ".join(
        f"{t}.{c}" for t, c in med["columnas"]))]
    if plan["agrupar"]:
        lineas.append("AGRUPAR: " + ", ".join(f"{t}.{c}" for t, c in plan["agrupar"]))
    for f in plan["filtros"]:
        o = f"{f['objeto'][0]}.{f['objeto'][1]}"
        lineas.append(f"FILTRO: {o} desde {f['a']} hasta {f['b']}" if f["tipo"] == "desde_hasta" else
                      f"FILTRO: {o} entre {f['a']} y {f['b']}" if f["tipo"] == "entre" else f"FILTRO: {o} = {f['a']}")
    if plan["orden"]:
        lineas.append(f"ORDEN: {plan['orden']}")
    if plan["limite"]:
        lineas.append(f"LIMITE: {plan['limite']}")
    return "\n".join(lineas)


# Compilación ------------------------------------------------------------------------------------------------------

def _resolver(schema: dict, t: str, c: str) -> tuple[str, dict]:
    tablas = schema["tablas"]
    tt = next((x for x in tablas if x.lower() == t.lower()), None)
    if tt is None:
        raise Aclaracion(f"la tabla «{t}» no existe; tablas: {', '.join(sorted(tablas))}")
    col = next((x for x in tablas[tt]["columnas"] if x["nombre"].lower() == c.lower()), None)
    if col is None:
        raise Aclaracion(f"la columna «{t}.{c}» no existe; columnas de {tt}: "
                         + ", ".join(x["nombre"] for x in tablas[tt]["columnas"]))
    return tt, col


def _aristas(relaciones: list[dict]) -> list[dict]:
    """Solo relaciones COMPROBADAS (contención medida); muchos→uno si el destino es único."""
    return [r for r in relaciones if r.get("contencion") is True]


def _camino(raiz: str, destino: str, rels: list[dict], dirigido: bool):
    """Camino más corto de raiz a destino. Dirigido: solo hijo→padre (destino único). Devuelve lista de pasos."""
    cola, visto = deque([(raiz, [])]), {raiz}
    while cola:
        t, pasos = cola.popleft()
        if t == destino:
            return pasos
        for r in rels:
            ida = r["tabla"] == t and (r.get("unico") or not dirigido)
            vuelta = (not dirigido) and r["tabla_ref"] == t
            if ida and r["tabla_ref"] not in visto:
                visto.add(r["tabla_ref"])
                cola.append((r["tabla_ref"], pasos + [(r, "muchos_a_uno" if r.get("unico") else "sin_unicidad")]))
            if vuelta and r["tabla"] not in visto:
                visto.add(r["tabla"])
                cola.append((r["tabla"], pasos + [(r, "uno_a_muchos")]))
    return None


def _fecha(v: str) -> dt.datetime:
    v = v.strip()
    if not FECHA_RE.match(v):
        raise Aclaracion(f"«{v}» no es una fecha AAAA-MM-DD[ HH:MM[:SS]]")
    fmt = "%Y-%m-%d %H:%M:%S" if v.count(":") == 2 else ("%Y-%m-%d %H:%M" if ":" in v else "%Y-%m-%d")
    return dt.datetime.strptime(v.replace("T", " "), fmt)


def compilar(plan: dict, schema: dict, relaciones: list[dict]) -> dict:
    """Plan → SQL determinista. Devuelve el plan compilado (con su SQL y lo necesario para verificarlo)."""
    med = plan["medida"]
    cols_med = [_resolver(schema, t, c) + (c,) for t, c in med["columnas"]]
    grupos = [_resolver(schema, t, c) for t, c in plan["agrupar"]]
    etiquetas = [None if e is None else _resolver(schema, *e) for e in (plan.get("etiquetas") or [])]
    filtros = [(_resolver(schema, *f["objeto"]), f) for f in plan["filtros"]]
    if cols_med:
        raiz = cols_med[0][0]
    elif grupos:
        raiz = grupos[0][0]
    elif filtros:
        raiz = filtros[0][0][0]
    else:
        raise Aclaracion("«conteo» sin columna necesita AGRUPAR o FILTRO para saber qué tabla contar")
    for (t, col, _) in cols_med:
        if med["funcion"] in ("suma", "promedio") and not is_numeric(col["tipo"]):
            raise Aclaracion(f"«{med['funcion']}» necesita una columna numérica; {t}.{col['nombre']} es {col['tipo']}")
    tablas = ([raiz] + [t for t, *_ in cols_med[1:]] + [t for t, _ in grupos] + [t for (t, _), _ in filtros]
              + [e[0] for e in etiquetas if e])
    rels = _aristas(relaciones)
    alias = {raiz: "t0"}
    joins, multiplica = [], False
    for t in tablas:
        if t in alias:
            continue
        pasos = _camino(raiz, t, rels, dirigido=True)
        if pasos is None:
            pasos = _camino(raiz, t, rels, dirigido=False)
        if pasos is None:
            raise Aclaracion(f"no hay una relación comprobada que una {raiz} con {t}; relaciones comprobadas: "
                             + (", ".join(f"{r['tabla']}.{r['columna']}→{r['tabla_ref']}.{r['columna_ref']}"
                                          for r in rels) or "ninguna"))
        actual = raiz
        for r, sentido in pasos:
            if sentido == "uno_a_muchos":
                origen, destino, c_or, c_de = r["tabla_ref"], r["tabla"], r["columna_ref"], r["columna"]
                multiplica = True
            else:
                origen, destino, c_or, c_de = r["tabla"], r["tabla_ref"], r["columna"], r["columna_ref"]
                multiplica = multiplica or sentido == "sin_unicidad"
            assert origen == actual
            if destino not in alias:
                alias[destino] = f"t{len(alias)}"
                joins.append({"tabla": destino, "alias": alias[destino], "on": (alias[origen], c_or, alias[destino],
                                                                                 c_de), "sentido": sentido})
            actual = destino

    def ref(t, c):
        return f"{alias[t]}.{_q(c)}"

    if med["funcion"] == "conteo":
        expr = None
        agg = f"COUNT({ref(cols_med[0][0], cols_med[0][1]['nombre'])})" if cols_med else "COUNT(*)"
    elif med["funcion"] == "distintos":
        expr = None
        agg = f"COUNT(DISTINCT {ref(cols_med[0][0], cols_med[0][1]['nombre'])})"
    else:
        expr = " * ".join(ref(t, col["nombre"]) for t, col, _ in cols_med)
        agg = f"{FUNCIONES[med['funcion']]}({expr})"
    where, intervalos = [], []
    for (t, col), f in filtros:
        r_ = ref(t, col["nombre"])
        temporal = is_temporal(col["tipo"])
        if f["tipo"] in ("desde_hasta", "entre") and temporal:
            a, b = _fecha(f["a"]), _fecha(f["b"])
            sa, sb = f"'{a:%Y-%m-%d %H:%M:%S}'", f"'{b:%Y-%m-%d %H:%M:%S}'"
            where.append(f"{r_} >= {sa} AND {r_} {'<' if f['tipo'] == 'desde_hasta' else '<='} {sb}")
            intervalos.append(_intervalo(t, col, f["tipo"], a, b))
        elif f["tipo"] in ("desde_hasta", "entre"):
            where.append(f"{r_} >= {sql_literal(f['a'])} AND {r_} {'<' if f['tipo'] == 'desde_hasta' else '<='} "
                         f"{sql_literal(f['b'])}")
        elif temporal:
            a = _fecha(f["a"])
            where.append(f"{r_} = '{a:%Y-%m-%d %H:%M:%S}'" if col["tipo"].lower().startswith("datetime")
                         else f"{r_} = '{a:%Y-%m-%d}'")
            intervalos.append(_intervalo(t, col, "igual", a, a))
        else:
            where.append(f"{r_} = {sql_literal(f['a'])}")
    desde = f"FROM {_q(raiz)} t0" + "".join(
        f" JOIN {_q(j['tabla'])} {j['alias']} ON {j['on'][0]}.{_q(j['on'][1])} = {j['on'][2]}.{_q(j['on'][3])}"
        for j in joins)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    gby = [ref(t, col["nombre"]) for t, col in grupos]
    eby = [ref(t, col["nombre"]) for t, col in (e for e in etiquetas if e)]
    pk = [c["nombre"] for c in schema["tablas"][raiz]["columnas"] if c["clave"] == "PRI"]
    c = {"plan": plan, "raiz": raiz, "alias": alias, "joins": joins, "desde": desde, "where": cond,
         "agregado": agg, "expresion": expr, "grupos": [(t, col["nombre"]) for t, col in grupos], "gby": gby,
         "etiquetas": [None if e is None else (e[0], e[1]["nombre"]) for e in etiquetas], "eby": eby,
         "tablas": sorted(alias), "intervalos": intervalos, "puede_multiplicar": multiplica,
         "pk_raiz": pk[0] if len(pk) == 1 else None, "medida_tablas": [t for t, *_ in cols_med]}
    c["sql"] = sql_con_agregado(c, agg, plan["limite"])
    c["nombres"] = nombres(c)
    return c


def sql_con_agregado(c: dict, agg: str, limite: int | None) -> str:
    """El SELECT del plan con otro agregado: mismas filas (FROM, JOIN, WHERE), mismos grupos, mismo orden.
    Columnas del resultado: claves g1.. (alias internos), valor, etiquetas e1.. (alias internos). Los alias nunca
    llegan al usuario: el coordinador los traduce con nombres()."""
    gsel = [f"{g} AS g{i + 1}" for i, g in enumerate(c["gby"])]
    esel = [f"{e} AS e{i + 1}" for i, e in enumerate(c["eby"])]
    sql = f"SELECT {', '.join(gsel + [agg + ' AS valor'] + esel)} {c['desde']}{c['where']}"
    if c["gby"]:
        sql += " GROUP BY " + ", ".join(c["gby"] + c["eby"])
    if c["plan"]["orden"]:
        sql += f" ORDER BY valor {'DESC' if c['plan']['orden'] == 'mayor' else 'ASC'}" + "".join(
            f", g{i + 1}" for i in range(len(c["gby"])))
    if limite:
        sql += f" LIMIT {limite}"
    return sql


def describir_medida(func: str, columnas) -> str:
    """«suma(lineas_pedido.cantidad × lineas_pedido.precio_unitario)»: el significado de la columna «valor»."""
    return f"{func}(" + (" × ".join(f"{t}.{c}" for t, c in columnas) or "filas") + ")"


def nombres(c: dict) -> dict:
    """Alias interno → nombre que se muestra (para M4, la respuesta y el informe)."""
    out = {f"g{i + 1}": f"{t}.{col}" + (" (clave)" if c["eby"] else "") for i, (t, col) in enumerate(c["grupos"])}
    out.update({f"e{i + 1}": f"{t}.{col}" for i, (t, col) in enumerate(e for e in c["etiquetas"] if e)})
    out["valor"] = describir_medida(c["plan"]["medida"]["funcion"], c["plan"]["medida"]["columnas"])
    return out


def _intervalo(t: str, col: dict, tipo: str, a: dt.datetime, b: dt.datetime) -> dict:
    """Intervalo EFECTIVO de un filtro temporal, normalizado a semiabierto [inicio, fin) cuando es posible.
    En una columna DATE, «entre A y B» abarca los días completos A..B: [A, B+1 día). En DATETIME, «entre A y B» con
    B sin hora termina en B 00:00:00 (deja afuera casi todo el día B): [A, B] cerrado, que NO es [A, B+1)."""
    solo_fecha = col["tipo"].lower().startswith("date") and not col["tipo"].lower().startswith("datetime")
    if tipo == "desde_hasta":
        return {"objeto": f"{t}.{col['nombre']}", "inicio": a, "fin": b, "fin_incluido": False}
    if tipo == "entre" and solo_fecha:
        return {"objeto": f"{t}.{col['nombre']}", "inicio": a, "fin": b + dt.timedelta(days=1), "fin_incluido": False}
    if tipo == "igual" and solo_fecha:
        return {"objeto": f"{t}.{col['nombre']}", "inicio": a, "fin": a + dt.timedelta(days=1), "fin_incluido": False}
    return {"objeto": f"{t}.{col['nombre']}", "inicio": a, "fin": b, "fin_incluido": True}


# Consultas de verificación derivadas del plan (las usa el verificador) ---------------------------------------------

def sql_grupos_completo(c: dict) -> str:
    """Todos los grupos, con su conteo de filas: para sumas por grupo, cotas y ranking."""
    if not c["gby"]:
        return ""
    gsel = [f"{g} AS g{i + 1}" for i, g in enumerate(c["gby"])]
    return (f"SELECT {', '.join(gsel)}, {c['agregado']} AS valor, COUNT(*) AS n {c['desde']}{c['where']}"
            f" GROUP BY {', '.join(c['gby'])}")


def sql_total(c: dict) -> str:
    """El mismo agregado sin agrupar, más el conteo y los extremos de la expresión medida."""
    extra = f", MIN({c['expresion']}) AS minimo, MAX({c['expresion']}) AS maximo" if c["expresion"] else ""
    return f"SELECT {c['agregado']} AS valor, COUNT(*) AS n{extra} {c['desde']}{c['where']}"


def sql_filas(c: dict) -> str | None:
    """Filas unidas contra filas distintas de la tabla raíz: si difieren, los JOIN multiplicaron filas."""
    if not c["pk_raiz"]:
        return None
    return f"SELECT COUNT(*) AS filas, COUNT(DISTINCT t0.{_q(c['pk_raiz'])}) AS distintas {c['desde']}{c['where']}"


def sql_ganador(c: dict, valores: list) -> str:
    """Contraprueba: el agregado SOLO para el grupo ganador, sin GROUP BY, ORDER ni LIMIT."""
    cond = c["where"] + (" AND " if c["where"] else " WHERE ") + " AND ".join(
        (f"{g} IS NULL" if v is None else f"{g} = {sql_literal(v)}") for g, v in zip(c["gby"], valores))
    return f"SELECT {c['agregado']} AS valor {c['desde']}{cond}"
