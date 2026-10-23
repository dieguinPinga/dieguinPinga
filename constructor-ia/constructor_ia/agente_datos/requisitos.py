"""Requisitos de una pregunta, deducidos POR REGLA, y su cobertura en cada paso (AGENTE_DATOS.md, §13).

El coordinador detecta lo que la pregunta exige ANTES de avanzar a la microtarea siguiente:
- período: un intervalo exacto [desde, hasta) («marzo de 2026», «en 2026»);
- orden y límite: «más/mayor» → mayor, «menos/menor» → menor; una entidad en singular → 1 resultado; un número
  explícito («los 3 productos») → ese número;
- entidad: el sustantivo preguntado («qué producto»);
- medida: un ranking o un promedio necesitan algo que medir (una columna de rol «medida» o filas que contar);
- conceptos de la medida (fase 2c, A1): las palabras de contenido que quedan después de quitar el período, el
  orden, el límite y la entidad («gastó», «unidades»). Determinan QUÉ se mide; el modelo tiene que declarar cómo
  las interpreta (INTERPRETA) y esa declaración tiene que coincidir con la MEDIDA del plan. Es una HIPÓTESIS del
  modelo, nunca una verificación: solo sirve para que la interpretación quede escrita y sea comparable.

Nunca inventa: si la regla no puede determinar algo sin ambigüedad (varios períodos, un mes sin año, «la semana
pasada», mayor y menor a la vez, una comparación «más de …», varios resultados sin decir cuántos), devuelve una
ACLARACIÓN y la misión se abstiene pidiendo esa aclaración, sin llamar al modelo para elegir tablas.
"""
from __future__ import annotations

import re
from collections import deque

from ..explorer.mapstore import is_temporal
from . import texto as TX

NUMEROS = {"dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10}
INDICIOS_TEMPORALES = set(TX.MESES) | {"trimestre", "trimestres", "semestre", "semestres", "semana", "semanas", "ayer",
                                      "hoy", "anteayer", "pasado", "pasada", "ultimo", "ultima", "ultimos", "ultimas",
                                      "reciente", "recientes", "dias", "meses", "anos"}
COMPARACION = re.compile(r"\b(mas|menos)\s+(de|que)\b")


def deducir(pregunta: str) -> dict:
    ws = TX.palabras(pregunta)
    s = set(ws)
    out = {"periodo": None, "orden": None, "limite": None, "entidad": TX.entidad(pregunta),
           "medida_requerida": False, "aclaraciones": [], "deducido": [], "conceptos": conceptos(pregunta)}
    per = TX.periodo(pregunta)
    if per and per.get("varios"):
        out["aclaraciones"].append("la pregunta menciona más de un período: ¿cuál hay que usar?")
    elif per:
        usados = {w for w in TX.MESES if w in s and per["texto"].startswith(f"{TX.MESES[w]:02d}/")}
        extra = sorted((s & INDICIOS_TEMPORALES) - usados)
        if extra:
            out["aclaraciones"].append(f"además de {per['texto']}, la pregunta menciona «{', '.join(extra)}»: ¿qué "
                                       "período exacto hay que usar?")
        else:
            out["periodo"] = per
            out["deducido"].append(f"período {per['texto']} = [{per['desde']:%Y-%m-%d}, {per['hasta']:%Y-%m-%d})")
    elif s & INDICIOS_TEMPORALES:
        out["aclaraciones"].append(f"la pregunta habla de un tiempo («{', '.join(sorted(s & INDICIOS_TEMPORALES))}») que "
                                   "la regla no puede convertir en un intervalo exacto (por ejemplo, falta el año): "
                                   "¿qué período?")
    it = TX.intencion(pregunta)
    if s & TX.MAYOR and s & TX.MENOR:
        out["aclaraciones"].append("la pregunta pide a la vez el mayor y el menor: ¿cuál?")
    elif COMPARACION.search(" ".join(ws)):
        out["aclaraciones"].append("la pregunta compara con un valor («más de…», «menos que…»): el coordinador todavía "
                                   "no interpreta comparaciones")
    elif it["orden"]:
        out["orden"] = it["orden"]
        n = [int(x) for x in re.findall(r"\b(\d{1,2})\b", " ".join(ws))] + [NUMEROS[w] for w in ws if w in NUMEROS]
        ent = out["entidad"] or ""
        plural = ent.endswith("s") or bool(s & {"cuales", "quienes"})
        if len(set(n)) > 1:
            out["aclaraciones"].append(f"la pregunta menciona varias cantidades ({sorted(set(n))}): ¿cuántos resultados?")
        elif n:
            out["limite"] = n[0]
        elif plural:
            out["aclaraciones"].append(f"la pregunta pide varios («{ent or 'cuáles'}») sin decir cuántos")
        elif ent:
            out["limite"] = 1
        out["deducido"].append(f"orden {out['orden']}" + (f", {out['limite']} resultado(s)" if out["limite"] else ""))
    out["medida_requerida"] = bool(it["orden"] or it["agregado"] == "promedio")
    if out["entidad"]:
        out["deducido"].append(f"entidad «{out['entidad']}»")
    if out["conceptos"]:
        out["deducido"].append("conceptos de la medida: " + ", ".join(f"«{w}»" for w in out["conceptos"]))
    return out


def conceptos(pregunta: str) -> list[str]:
    """La primera palabra de la pregunta por cada raíz de TX.terminos (los conceptos que determinan la medida)."""
    ws = TX.palabras(pregunta)
    return [next(w for w in ws if TX.raiz(w) == r) for r in TX.terminos(pregunta)]


# Cobertura de las tablas elegidas (después de M2) ------------------------------------------------------------------

def _roles(fichas: dict, t: str) -> dict:
    return {c: r["rol"] for c, r in ((fichas.get(t) or {}).get("roles") or {}).items()}


def _camino(desde: set, destino: str, rels: list[dict]) -> list[dict] | None:
    ok = [r for r in rels if r.get("contencion")]
    cola, visto = deque((t, []) for t in sorted(desde)), set(desde)
    while cola:
        t, pasos = cola.popleft()
        if t == destino:
            return pasos
        for r in ok:
            for a, b in ((r["tabla"], r["tabla_ref"]), (r["tabla_ref"], r["tabla"])):
                if a == t and b not in visto:
                    visto.add(b)
                    cola.append((b, pasos + [r]))
    return None


def _via(pasos: list[dict]) -> str:
    return ", ".join(f"{r['tabla']}.{r['columna']} → {r['tabla_ref']}.{r['columna_ref']}" for r in pasos)


def alternativas(tablas: list[str], rol: str, fichas: dict, rels: list[dict]) -> list[str]:
    """Columnas con ese rol en tablas NO elegidas pero conectadas, en orden alfabético (sin preferencia)."""
    out = []
    for t in sorted(fichas):
        if t in tablas:
            continue
        cols = sorted(c for c, r in _roles(fichas, t).items() if r == rol)
        pasos = _camino(set(tablas), t, rels) if cols else None
        if cols and pasos is not None:
            out += [f"{t}.{c} (unida por {_via(pasos)})" for c in cols]
    return out


def tablas_de_entidad(entidad: str, tablas, fichas: dict) -> list[str]:
    r = TX.raiz(entidad)
    return [t for t in tablas if r in TX.raices(t) or r in TX.raices((fichas.get(t) or {}).get("fila", ""))]


def cobertura(req: dict, tablas: list[str], fichas: dict, rels: list[dict]) -> list[str]:
    """Lo que la pregunta exige y las tablas elegidas no cubren. Vacío = cubierto."""
    problemas = []
    cierre = " (orden alfabético; no indica preferencia)"
    if req.get("periodo"):
        if not any(r == "tiempo" for t in tablas for r in _roles(fichas, t).values()):
            alts = alternativas(tablas, "tiempo", fichas, rels)
            problemas.append(f"la pregunta pide un período ({req['periodo']['texto']}), pero ninguna tabla elegida tiene "
                             "una columna de rol «tiempo». " + (
                                 f"Columnas de tiempo en tablas conectadas{cierre}: {'; '.join(alts)}" if alts else
                                 "Ninguna tabla conectada tiene una columna de rol «tiempo»"))
    if req.get("entidad") and not tablas_de_entidad(req["entidad"], tablas, fichas):
        alts = [f"{t} (unida por {_via(p)})" for t in tablas_de_entidad(req["entidad"], sorted(fichas), fichas)
                if t not in tablas and (p := _camino(set(tablas), t, rels)) is not None]
        problemas.append(f"la pregunta pregunta por «{req['entidad']}», pero ninguna tabla elegida corresponde a esa "
                         "entidad. " + (f"Tablas conectadas que la nombran{cierre}: {'; '.join(alts)}" if alts else
                                        "Ninguna tabla conectada la nombra"))
    if req.get("medida_requerida"):
        hay_medida = any(r == "medida" for t in tablas for r in _roles(fichas, t).values())
        de_entidad = set(tablas_de_entidad(req["entidad"], tablas, fichas)) if req.get("entidad") else set()
        if not hay_medida and set(tablas) <= de_entidad:
            alts = alternativas(tablas, "medida", fichas, rels)
            problemas.append("la pregunta pide un ranking o un promedio y las tablas elegidas no tienen nada que medir "
                             "(ni una columna de rol «medida» ni otras filas que contar). "
                             + (f"Columnas de medida en tablas conectadas{cierre}: {'; '.join(alts)}" if alts else ""))
    return problemas


# Requisitos de un plan MEDIR (después de M3, ANTES de ejecutar) ----------------------------------------------------

def columnas_por_rol(tablas: list[str], fichas: dict, rol: str) -> list[str]:
    return sorted(f"{t}.{c}" for t in tablas for c, r in _roles(fichas, t).items() if r == rol)


def faltantes(req: dict, plan: dict, schema: dict, tablas: list[str], fichas: dict) -> list[tuple[str, str]]:
    """(motivo, corrección) por cada requisito que el plan no cumple. No ejecuta nada."""
    out = []
    if req.get("periodo"):
        temporales = [f for f in plan["filtros"] if f["tipo"] in ("desde_hasta", "entre", "igual") and any(
            t.lower() == f["objeto"][0].lower() and any(c["nombre"].lower() == f["objeto"][1].lower() and
                                                        is_temporal(c["tipo"]) for c in td["columnas"])
            for t, td in schema["tablas"].items())]
        if not temporales:
            cols = columnas_por_rol(tablas, fichas, "tiempo")
            p = req["periodo"]
            out.append((f"falta el FILTRO del período {p['texto']} sobre una columna de tiempo",
                        f"agregá «FILTRO: <tabla.columna de tiempo> desde {p['desde']:%Y-%m-%d} hasta "
                        f"{p['hasta']:%Y-%m-%d}»" + (f"; columnas de tiempo de las tablas elegidas: {', '.join(cols)}"
                                                      if cols else "")))
    if req.get("entidad") and not plan["agrupar"]:
        cols = [c for c in columnas_por_rol(tablas, fichas, "texto") + columnas_por_rol(tablas, fichas, "categoria")
                + columnas_por_rol(tablas, fichas, "identificador")
                if c.split(".")[0] in tablas_de_entidad(req["entidad"], tablas, fichas)]
        out.append((f"la pregunta pregunta por «{req['entidad']}» y falta AGRUPAR",
                    f"agregá «AGRUPAR: <tabla.columna>»" + (f" con una de: {', '.join(sorted(cols))}" if cols else "")))
    return out


# A1 — interpretación declarada de los conceptos de la medida (después de M3, ANTES de ejecutar) --------------------

FUNCIONES_TEXTO = {"suma": "suma", "sumar": "suma", "conteo": "conteo", "contar": "conteo", "promedio": "promedio",
                   "maximo": "maximo", "minimo": "minimo", "distintos": "distintos"}


def interpretacion(req: dict, prop: dict) -> list[tuple[str, str]]:
    """(motivo, corrección) si falta la INTERPRETA de un concepto de la medida o si contradice la MEDIDA del plan.
    En un plan MEDIR, las columnas citadas por esas INTERPRETA tienen que estar en la MEDIDA y entre todas cubrirla;
    si nombran una función, tiene que ser la del plan. En SQL libre solo se exige que estén declaradas.
    Quedan fuera las palabras que el plan usa como VALOR de un filtro por igualdad («Rosario» en «FILTRO:
    clientes.ciudad = Rosario»): no dicen qué se mide."""
    palabras_ = req.get("conceptos") or []
    if not palabras_:
        return []
    plan = prop.get("plan") if prop.get("tipo") == "medir" else None
    valores = {w for f in (plan or {}).get("filtros", []) if f["tipo"] == "igual" for w in TX.palabras(f["a"])}
    interp = prop.get("interpreta") or {}
    declaradas, faltan = {}, []
    for w in palabras_:
        if w in valores:
            continue
        r = TX.raiz(w)
        vals = [v for k, v in interp.items() if r in TX.raices(k) or TX.raiz(k) == r]
        if vals:
            declaradas[w] = vals
        else:
            faltan.append(w)
    forma = (f"<la misma MEDIDA: {_medida_texto(plan)}>" if plan else "<qué columna o cálculo representa>")
    out = []
    if faltan:
        out.append((f"falta INTERPRETA para {', '.join(f'«{w}»' for w in faltan)} (dice qué se mide)",
                    "agregá " + "; ".join(f"«INTERPRETA: {w} = {forma}»" for w in faltan)
                    + " (es tu hipótesis: el coordinador la registra como declarada, no como verificada)"))
    if plan is None:
        return out
    medida = {f"{t}.{c}".lower() for t, c in plan["medida"]["columnas"]}
    cubiertas = set()
    for w, vals in declaradas.items():
        for v in vals:
            citadas = {m.lower() for m in re.findall(r"[A-Za-z_][\w$]*\.[A-Za-z_][\w$]*", v)}
            ajenas = sorted(citadas - medida)
            cubiertas |= citadas & medida
            funcs = {FUNCIONES_TEXTO[x] for x in TX.palabras(v) if x in FUNCIONES_TEXTO}
            if ajenas:
                out.append((f"INTERPRETA «{w} = {v}» cita {', '.join(ajenas)}, que no está en la MEDIDA del plan",
                            f"la interpretación y la MEDIDA tienen que decir lo mismo: o cambiás la MEDIDA o cambiás "
                            f"la interpretación de «{w}»"))
            if funcs and funcs != {plan["medida"]["funcion"]}:
                out.append((f"INTERPRETA «{w} = {v}» dice {', '.join(sorted(funcs))} y la MEDIDA es "
                            f"«{plan['medida']['funcion']}»", f"usá la misma función en la MEDIDA y en la "
                                                               f"interpretación de «{w}»"))
    sin = sorted(medida - cubiertas)
    if declaradas and sin:
        out.append((f"la MEDIDA usa {', '.join(sin)}, que ninguna INTERPRETA de "
                    f"{', '.join(f'«{w}»' for w in declaradas)} explica",
                    "cada columna de la MEDIDA tiene que aparecer en la interpretación de alguna palabra de la pregunta "
                    "(si ninguna palabra la pide, sacala de la MEDIDA)"))
    return out


def _medida_texto(plan: dict) -> str:
    med = plan["medida"]
    return med["funcion"] + ("" if not med["columnas"] else " " + " * ".join(f"{t}.{c}" for t, c in med["columnas"]))
