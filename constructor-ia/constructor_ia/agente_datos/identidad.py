"""Identidad del grupo: CLAVE de agrupación, ETIQUETA visible y VALOR calculado, siempre separados (fase 2c).

Un plan agrupa por lo que el modelo escribió en AGRUPAR. El coordinador decide POR REGLA, sin interpretar el
dominio, qué identifica a cada grupo:
1. la columna es la clave primaria (de una columna) de su tabla → es la clave; la etiqueta se elige aparte;
2. la columna es una referencia COMPROBADA (contención medida, destino único) → es la clave; la etiqueta sale de la
   tabla referida si está entre las elegidas;
3. la entidad preguntada nombra la COLUMNA («qué ciudad» ↔ clientes.ciudad) → el valor es la entidad: se agrupa
   por el valor y no hay etiqueta aparte;
4. la entidad preguntada nombra la TABLA de esa columna, la columna no es su clave y la tabla tiene una clave
   primaria de una columna → se agrupa por la clave y la columna queda como etiqueta (agrupar por un nombre
   juntaría entidades distintas con el mismo nombre). Se registra como normalización;
5. la columna tiene un valor distinto por fila en los datos ACTUALES → sirve como clave, pero solo por los datos
   (S7 lo informa);
6. si nada de lo anterior se cumple, la identidad queda sin verificar (S7 no verificable).

La etiqueta automática es la columna de texto o categoría (según la ficha; sin ficha, por tipo) con un valor
distinto por fila o, si ninguna lo tiene, la de más valores distintos (empate: orden alfabético). Es solo para
mostrar: la clave se conserva siempre en el resultado y en la respuesta estructurada, para distinguir nombres
repetidos.
"""
from __future__ import annotations

from ..explorer.mapstore import is_numeric, is_temporal
from . import texto as TX


def _real(schema: dict, t: str, c: str):
    tt = next((x for x in schema["tablas"] if x.lower() == t.lower()), None)
    if tt is None:
        return None
    col = next((x["nombre"] for x in schema["tablas"][tt]["columnas"] if x["nombre"].lower() == c.lower()), None)
    return (tt, col) if col else None


def clave_primaria(schema: dict, t: str) -> str | None:
    pk = [c["nombre"] for c in schema["tablas"][t]["columnas"] if c["clave"] == "PRI"]
    return pk[0] if len(pk) == 1 else None


def unica_en_datos(perfiles: dict, t: str, c: str) -> bool:
    p = perfiles.get(t) or {}
    pc = (p.get("columnas") or {}).get(c) or {}
    return bool(pc) and pc.get("distintos") == pc.get("no_nulos", p.get("filas")) == p.get("filas")


def referencia(rels: list[dict], t: str, c: str) -> dict | None:
    return next((r for r in rels if r["tabla"] == t and r["columna"] == c and r.get("contencion") is True
                 and r.get("unico")), None)


def etiqueta_auto(t: str, schema: dict, perfiles: dict, fichas: dict, excluir: str | None = None) -> str | None:
    roles = {c: r["rol"] for c, r in ((fichas.get(t) or {}).get("roles") or {}).items()}
    pk = clave_primaria(schema, t)
    cands = []
    for col in schema["tablas"][t]["columnas"]:
        n = col["nombre"]
        if n in (pk, excluir):
            continue
        if roles:
            if roles.get(n) not in ("texto", "categoria"):
                continue
        elif is_numeric(col["tipo"]) or is_temporal(col["tipo"]):
            continue
        pc = ((perfiles.get(t) or {}).get("columnas") or {}).get(n)
        if pc is None:
            continue
        cands.append((not unica_en_datos(perfiles, t, n), -int(pc.get("distintos") or 0), n))
    return sorted(cands)[0][2] if cands else None


def resolver(plan: dict, schema: dict, perfiles: dict, fichas: dict, rels: list[dict], entidad: str | None,
             tablas: list[str]) -> list[str]:
    """Completa plan["agrupar"] (claves), plan["etiquetas"] y plan["identidad"]. Devuelve las normalizaciones."""
    e = TX.raiz(entidad) if entidad else None
    claves, etiquetas, identidad, notas = [], [], [], []
    for t0, c0 in plan["agrupar"]:
        real = _real(schema, t0, c0)
        if real is None:                    # el compilador dará la aclaración con las opciones reales
            claves.append((t0, c0))
            etiquetas.append(None)
            identidad.append({"tipo": "sin_verificar", "detalle": f"{t0}.{c0} no existe"})
            continue
        t, c = real
        pk = clave_primaria(schema, t)
        ref = referencia(rels, t, c)
        if pk == c:
            claves.append((t, c))
            etiquetas.append(_etiqueta(t, schema, perfiles, fichas, c))
            identidad.append({"tipo": "clave", "detalle": f"{t}.{c} es la clave primaria de {t}"})
        elif ref is not None:
            claves.append((t, c))
            destino = ref["tabla_ref"]
            etiquetas.append(_etiqueta(destino, schema, perfiles, fichas) if destino in tablas else None)
            identidad.append({"tipo": "referencia", "detalle": f"{t}.{c} → {destino}.{ref['columna_ref']} "
                                                                "(relación comprobada, destino único)"})
        elif e and e in TX.raices(c):
            claves.append((t, c))
            etiquetas.append(None)
            identidad.append({"tipo": "atributo", "detalle": f"la entidad preguntada («{entidad}») es el valor de "
                                                              f"{t}.{c}: cada valor distinto es un grupo"})
        elif e and e in TX.raices(t) and pk:
            claves.append((t, pk))
            etiquetas.append((t, c))
            identidad.append({"tipo": "reagrupada", "detalle": f"se agrupa por la clave {t}.{pk}; {t}.{c} queda "
                                                                "como etiqueta"})
            notas.append(f"AGRUPAR {t}.{c} se reemplazó por la clave {t}.{pk} con {t}.{c} como etiqueta (la entidad "
                         f"«{entidad}» es una fila de {t}; agrupar por {c} juntaría filas distintas con el mismo valor)")
        elif unica_en_datos(perfiles, t, c):
            claves.append((t, c))
            etiquetas.append(None)
            identidad.append({"tipo": "unica_en_datos", "detalle": f"{t}.{c} tiene un valor distinto por fila en "
                                                                    "los datos actuales (no es una clave declarada)"})
        else:
            claves.append((t, c))
            etiquetas.append(None)
            identidad.append({"tipo": "sin_verificar", "detalle": f"{t}.{c} no es clave ni referencia comprobada y "
                                                                   "repite valores: puede juntar filas distintas"})
    plan["agrupar"], plan["etiquetas"], plan["identidad"] = claves, etiquetas, identidad
    return notas


def _etiqueta(t, schema, perfiles, fichas, excluir=None):
    n = etiqueta_auto(t, schema, perfiles, fichas, excluir)
    return (t, n) if n else None
