"""Evidencia de ejecución por candidata (Etapa 1 del experimento de elección; PILOTO.md, «Experimento de elección»).

GENÉRICA: sirve para cualquier instrumento Python. El anfitrión deriva del árbol sintáctico qué nombres define y usa
cada candidata; el sandbox ejecuta el instrumento una vez con los mismos datos y observa esas líneas. De ahí salen
hechos neutrales, con la misma plantilla para todas las candidatas:

* cuántas veces se ejecutó (o que nunca se ejecutó);
* valores antes y después de su última ejecución, y muestras de lo que usó y lo que dejó;
* si, al terminar medir, cada nombre que define conserva el valor que dejó esa línea (llega a la salida) o si otra
  instrucción lo cambió después (se pierde);
* si asigna una constante y reemplazó un valor distinto;
* en una compuesta (if / while): en cuántas evaluaciones su condición fue verdadera.

Nunca dice cuál es la causa, y no usa el juez ni la referencia.
"""
from __future__ import annotations

import ast

from . import localizador as L
from . import parches as P


def _constante(nodo) -> bool:
    if not isinstance(nodo, ast.Assign):
        return False
    try:
        ast.literal_eval(nodo.value)
        return True
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False


def nombres_locales(codigo: str) -> set[str]:
    """Nombres que el programa asigna o recibe como parámetro (no funciones ni módulos globales)."""
    out = set()
    for n in ast.walk(ast.parse(codigo)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):   # los parámetros de una lambda no son locales
            out |= {a.arg for a in n.args.args + n.args.kwonlyargs}
        elif isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.For)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                out |= {x.id for x in ast.walk(t) if isinstance(x, ast.Name)}
    return out


def pedido(unidades: list[P.Unidad], codigo: str | None = None) -> dict:
    """Lo que el sandbox debe observar, por línea de inicio de cada candidata. Con el código, «usa» se limita a
    nombres del propio programa (sin funciones ni módulos)."""
    locales = nombres_locales(codigo) if codigo else None
    out = {}
    for u in unidades:
        defs, uses = L._defs_uses(u)
        if locales is not None:
            uses &= locales
        cuerpo = None
        if isinstance(u.nodo, (ast.If, ast.While)) and u.nodo.body:
            cuerpo = [u.nodo.body[0].lineno, u.nodo.body[-1].end_lineno]
        out[str(u.lineno)] = {"define": sorted(defs), "usa": sorted(uses - defs)[:6] + sorted(uses & defs)[:2],
                              "cuerpo": cuerpo}
    return out


def rasgos(traza: dict | None, u: P.Unidad) -> dict:
    """Hechos derivados de la traza para una candidata (los mismos campos para todas)."""
    d = ((traza or {}).get("lineas") or {}).get(str(u.lineno)) or {}
    final = (traza or {}).get("final") or {}
    ultima = d.get("ultima") or {}
    despues, antes = ultima.get("despues") or {}, ultima.get("antes") or {}
    nombres = sorted(despues)
    llega = {n: final.get(n) == despues.get(n) for n in nombres if n in final}
    return {"veces": d.get("veces", 0), "verdadera": d.get("verdadera", 0),
            "compuesta": isinstance(u.nodo, (ast.If, ast.While)),
            "llega": llega, "pierde": sorted(n for n, ok in llega.items() if not ok),
            "constante_reemplaza": _constante(u.nodo) and any(antes.get(n) != despues.get(n) and
                                                              antes.get(n) != "(sin definir)" for n in nombres),
            "muestras": d.get("muestras") or [], "ultima": ultima}


def _muestra(m: dict) -> str:
    usa = ", ".join(f"{k}={v}" for k, v in (m.get("usa") or {}).items())
    deja = ", ".join(f"{k}={v}" for k, v in (m.get("despues") or {}).items())
    return (f"usa {usa}" if usa else "") + (" → " if usa and deja else "") + (f"deja {deja}" if deja else "")


def texto(traza: dict | None, cands: list[dict], letras: str) -> str:
    """Evidencia en texto, con la MISMA plantilla para cada candidata, en el orden en que se muestran."""
    lineas = ["Evidencia de ejecución (el instrumento actual, ejecutado una vez con los mismos datos; son hechos "
              "observados, no indican cuál es la causa):"]
    for letra, c in zip(letras, cands):
        u = c["unidad"]
        r = rasgos(traza, u)
        lineas.append(f"{letra}) línea {u.lineno}: se ejecutó {r['veces']} "
                      f"{'vez' if r['veces'] == 1 else 'veces'}.")
        if r["veces"] == 0:
            continue
        if r["compuesta"]:
            lineas.append(f"   Su condición fue verdadera en {r['verdadera']} de {r['veces']} evaluaciones.")
        ultima = r["ultima"]
        if ultima.get("despues"):
            lineas.append("   Última ejecución: antes " + ", ".join(f"{k}={v}" for k, v in ultima["antes"].items())
                          + "; después " + ", ".join(f"{k}={v}" for k, v in ultima["despues"].items()) + ".")
        for n, ok in r["llega"].items():
            lineas.append(f"   Al terminar medir, {n} conserva el valor que dejó esta línea." if ok else
                          f"   Al terminar medir, {n} ya no tiene el valor que dejó esta línea (cambió después).")
        if r["constante_reemplaza"]:
            lineas.append("   Asigna un valor constante y reemplazó un valor distinto.")
        muestras = [m for m in r["muestras"] if m] + ([ultima] if ultima and r["veces"] > 2 else [])
        if muestras:
            lineas.append("   Muestras: " + " | ".join(_muestra(m) for m in muestras if _muestra(m)))
    return "\n".join(lineas) + "\n"
