"""A2 — sensibilidad de la respuesta a la MEDIDA elegida (fase 2c).

Un plan puede ser matemáticamente correcto y medir otra cosa. El coordinador no puede saber qué significa una
palabra del dominio, pero sí puede medir si la respuesta DEPENDE de esa interpretación: recalcula el mismo ranking
con otras medidas plausibles PARA LA MISMA PREGUNTA y compara.

Alternativa plausible ≠ operación posible. Sumar unidades y sumar dinero son operaciones posibles sobre los mismos
datos, pero responden preguntas distintas («más unidades» frente a «más facturación»): que cambien el ganador no
demuestra ambigüedad, cambia la magnitud. Por eso solo se comparan medidas de la MISMA MAGNITUD que la del plan,
según una regla genérica y documentada (no propia de ninguna base): la firma de la medida es la lista de las clases
de tipo SQL de sus factores (entera: INT y similares; continua: DECIMAL, FLOAT, DOUBLE; indicador: una columna
entera cuyos valores medidos están todos entre 0 y 1, cuya suma cuenta filas en lugar de sumar cantidades). Una suma de una columna
entera solo se compara con otras columnas enteras; un producto entera × continua, con otros productos entera ×
continua. Las demás operaciones se listan como «otras magnitudes» (sin ejecutarlas) y nunca provocan una abstención.
La firma de tipos es un indicio, no una prueba de significado: dos columnas enteras pueden ser magnitudes distintas
(unidades y días), y dinero guardado en centavos enteros quedaría como entero. Lo que la firma no separa, lo informa
el diagnóstico; el significado de la medida sigue sin confirmar (S4) y la respuesta sigue «incierta».

Alternativas ACOTADAS y justificadas por tipos, roles y relaciones (nunca todas las columnas numéricas):
- columnas con rol «medida» en la ficha (o, sin ficha, numéricas que no son clave ni referencia) de la tabla raíz
  del plan y de las tablas que el plan ya une en sentido muchos→uno (un valor por fila de la raíz). Ninguna
  alternativa agrega un JOIN ni cambia las filas: mismo FROM, mismo WHERE, mismos grupos, mismo orden;
- la misma función del plan (suma, promedio, máximo o mínimo); la intención (S2) ya controla la función;
- cada columna sola y cada producto de dos columnas con al menos una de la raíz, con la misma firma de tipos que
  la medida del plan;
- orden determinista: primero la medida del plan, después las columnas solas y los productos, alfabéticamente;
- a lo sumo MAX_ALTERNATIVAS consultas además de la del plan. Si hay más candidatas o alguna consulta falla, la
  exploración queda INCOMPLETA y se registra: nunca se concluye que el ganador es estable.

Clases: ganador_dependiente (alguna alternativa cambia el ganador), valor_dependiente (mismo ganador, otro valor),
estable (mismo ganador y valor), sin_alternativas, incompleta, no_aplica (sin ranking agrupado o función que no
depende de una columna medida). La política (qué nivel le corresponde a cada clase) la aplica el verificador.
"""
from __future__ import annotations

from itertools import combinations

from ..explorer.mapstore import is_numeric
from . import medir as MD
from . import verificador as VE

MAX_ALTERNATIVAS = 6
FUNCIONES = ("suma", "promedio", "maximo", "minimo")


def _medidas_de(t: str, schema: dict, fichas: dict, rels: list[dict]) -> list[str]:
    roles = {c: r["rol"] for c, r in ((fichas.get(t) or {}).get("roles") or {}).items()}
    refs = {r["columna"] for r in rels if r["tabla"] == t}
    out = []
    for col in schema["tablas"][t]["columnas"]:
        if not is_numeric(col["tipo"]):
            continue
        if roles:
            if roles.get(col["nombre"]) == "medida":
                out.append(col["nombre"])
        elif col["clave"] != "PRI" and col["nombre"] not in refs:
            out.append(col["nombre"])
    return out


def clase(tipo: str, perfil: dict | None = None) -> str:
    t = tipo.lower()
    if not t.startswith(("tinyint", "smallint", "mediumint", "int", "bigint", "integer")):
        return "continua"
    p = perfil or {}
    try:
        if p.get("min") is not None and float(p["min"]) >= 0 and float(p["max"]) <= 1:
            return "indicador"
    except (TypeError, ValueError):
        pass
    return "entera"


def firma(cols, schema: dict, perfiles: dict | None = None) -> tuple:
    """La magnitud de una medida, por regla de tipos (y del perfil, para los indicadores): las clases de sus
    factores, ordenadas."""
    out = []
    for t, x in cols:
        col = next(cc for cc in schema["tablas"][t]["columnas"] if cc["nombre"].lower() == x.lower())
        out.append(clase(col["tipo"], (((perfiles or {}).get(t) or {}).get("columnas") or {}).get(col["nombre"])))
    return tuple(sorted(out))


def candidatas(c: dict, schema: dict, fichas: dict, rels: list[dict],
               perfiles: dict | None = None) -> tuple[list[tuple], str, list[tuple]]:
    """Medidas alternativas de la MISMA magnitud (tuplas ordenadas de (tabla, columna)), sin la del plan, su
    justificación y las operaciones de OTRA magnitud (que responden otra pregunta y no se comparan)."""
    raiz = c["raiz"]
    unidas = [j["tabla"] for j in c["joins"] if j["sentido"] == "muchos_a_uno"]
    de_raiz = [(raiz, x) for x in _medidas_de(raiz, schema, fichas, rels)]
    otras = [(t, x) for t in unidas for x in _medidas_de(t, schema, fichas, rels)]
    elegida = _clave(c)
    solas = [(x,) for x in sorted(de_raiz + otras)]
    pares = [tuple(sorted(p)) for p in combinations(sorted(de_raiz + otras), 2) if raiz in (p[0][0], p[1][0])]
    todas = [m for m in solas + pares if _min(m) != _min(elegida)]
    f = firma(elegida, schema, perfiles)
    justif = (f"columnas de medida de {raiz} (raíz del plan)" + (f" y de {', '.join(unidas)} (unidas muchos→uno)"
                                                                  if unidas else "")
              + f": {', '.join(f'{t}.{x}' for t, x in sorted(de_raiz + otras)) or 'ninguna'}; solas y productos de dos "
                f"con al menos una de la raíz, con la misma magnitud que la del plan ({' × '.join(f)})")
    return ([m for m in todas if firma(m, schema, perfiles) == f], justif,
            [m for m in todas if firma(m, schema, perfiles) != f])


def _clave(c: dict) -> tuple:
    cols = [(t, x) for t, x in c["plan"]["medida"]["columnas"]]
    return tuple(sorted((next(tt for tt in c["alias"] if tt.lower() == t.lower()), x) for t, x in cols))


def _min(cols) -> tuple:
    return tuple(sorted((t.lower(), x.lower()) for t, x in cols))


def _ganadores(filas: list, ng: int, k: int) -> list:
    return [tuple(str(v) for v in r[:ng]) for r in filas[:k]]


def explorar(c: dict, schema: dict, fichas: dict, rels: list[dict], run, o, perfiles: dict | None = None) -> dict:
    """run(sql, herramienta) -> resultado del intermediario. o: el resultado del plan (ya verificado técnicamente)."""
    func = c["plan"]["medida"]["funcion"]
    ng, k = len(c["gby"]), c["plan"]["limite"] or 1
    base = {"clase": "no_aplica", "medida": MD.describir_medida(func, c["plan"]["medida"]["columnas"]),
            "alternativas": [], "candidatas": 0, "exploradas": 0, "consultas": 0, "completa": True, "motivo": "",
            "otras_magnitudes": []}
    if func not in FUNCIONES or not c["plan"]["medida"]["columnas"] or not ng or not c["plan"]["orden"]:
        base["motivo"] = "sin ranking agrupado sobre una columna medida"
        return base
    if not o.rows:
        base["motivo"] = "el plan no devolvió filas"
        return base
    cands, justif, otras = candidatas(c, schema, fichas, rels, perfiles)
    base.update(candidatas=len(cands), justificacion=justif,
                otras_magnitudes=[MD.describir_medida(func, m) for m in otras])
    elegido = _ganadores(o.rows, ng, k)
    valor = o.rows[0][ng]
    base["elegida"] = {"medida": base["medida"], "ganador": [list(g) for g in elegido], "valor": str(valor),
                       "etiqueta": [str(v) for v in o.rows[0][ng + 1:]]}
    if not cands:
        base["clase"] = "sin_alternativas"
        return base
    explorar_ = cands[:MAX_ALTERNATIVAS]
    if len(cands) > MAX_ALTERNATIVAS:
        base["completa"] = False
        base["motivo"] = (f"{len(cands)} alternativas posibles y un tope de {MAX_ALTERNATIVAS} consultas: se exploraron "
                          f"las primeras {MAX_ALTERNATIVAS} (orden determinista)")
    cambia_ganador = cambia_valor = False
    for cols in explorar_:
        expr = " * ".join(f"{c['alias'][t]}.{MD._q(x)}" for t, x in cols)
        agg = f"{MD.FUNCIONES[func]}({expr})"
        r = run(MD.sql_con_agregado(c, agg, k), "sensibilidad")
        base["consultas"] += 1
        desc = MD.describir_medida(func, cols)
        if r.status != "ok" or r.truncated:
            base["completa"] = False
            base["motivo"] = (base["motivo"] + "; " if base["motivo"] else "") + f"{desc}: la consulta falló"
            base["alternativas"].append({"medida": desc, "estado": "error"})
            continue
        if not r.rows:
            base["alternativas"].append({"medida": desc, "estado": "ok", "ganador": [], "valor": None,
                                         "etiqueta": []})
            cambia_ganador = True
            continue
        g = _ganadores(r.rows, ng, k)
        otro_ganador = g != elegido
        otro_valor = not VE._igual(r.rows[0][ng], valor)
        cambia_ganador |= otro_ganador
        cambia_valor |= otro_valor
        base["alternativas"].append({"medida": desc, "estado": "ok", "ganador": [list(x) for x in g],
                                     "valor": str(r.rows[0][ng]), "etiqueta": [str(v) for v in r.rows[0][ng + 1:]],
                                     "cambia_ganador": otro_ganador, "cambia_valor": otro_valor})
    base["exploradas"] = len(explorar_)
    if cambia_ganador:
        base["clase"] = "ganador_dependiente"
    elif not base["completa"]:
        base["clase"] = "incompleta"
    elif cambia_valor:
        base["clase"] = "valor_dependiente"
    else:
        base["clase"] = "estable"
    return base


def resumen(s: dict) -> str:
    """Una línea por interpretación (para el informe y la respuesta): ninguna se presenta como confirmada."""
    def fila(a):
        if a.get("estado") == "error":
            return f"{a['medida']}: no se pudo calcular"
        quien = " / ".join(a.get("etiqueta") or []) or " / ".join(" ".join(g) for g in a.get("ganador") or [])
        clave = ", ".join(" ".join(g) for g in a.get("ganador") or [])
        return f"{a['medida']}: {quien or 'sin filas'}" + (f" (clave {clave})" if a.get("etiqueta") and clave else "") \
            + f" = {a.get('valor')}"
    out = [fila(s["elegida"]) + " [la del plan]"] if s.get("elegida") else []
    return "; ".join(out + [fila(a) for a in s["alternativas"]])
