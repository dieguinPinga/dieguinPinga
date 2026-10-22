"""Defectos EXTERNOS para evaluar E0 a ciegas (PILOTO.md, «Evaluación a ciegas de E0»).

Los escribe otra persona, sin conocer las reglas, en una carpeta con un `manifiesto.json`:

    [{"id": "X01", "archivo": "x01.py", "lineas": [desde, hasta], "clase": "cálculo",
      "descripcion": "una frase"}, ...]

* cada archivo es el instrumento COMPLETO con el defecto (una variante de la referencia del banco: misma pregunta y
  mismo contrato, medir(db, p));
* `lineas` son las líneas del defecto EN ESE ARCHIVO (solo se usan para puntuar);
* `clase` la declara quien lo escribe (cálculo, sobrescritura, condición, omisión u otra) y solo se usa para informar.

La carga valida forma y sintaxis; no mira el contenido para decidir nada.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

CLASES = ("cálculo", "sobrescritura", "condición", "omisión", "otra")


def cargar(carpeta) -> tuple[list[dict], list[str]]:
    """Devuelve (defectos válidos, problemas). Un defecto con problemas se informa y no se evalúa."""
    carpeta = Path(carpeta)
    problemas, out, vistos = [], [], set()
    try:
        items = json.loads((carpeta / "manifiesto.json").read_text())
    except (OSError, ValueError) as e:
        return [], [f"manifiesto.json ilegible: {e}"]
    if not isinstance(items, list):
        return [], ["manifiesto.json debe ser una lista"]
    for i, d in enumerate(items):
        if not isinstance(d, dict):
            problemas.append(f"entrada {i}: no es un objeto")
            continue
        did = str(d.get("id") or "")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,19}", did) or did in vistos or re.fullmatch(r"(D\d+|M\d+)", did):
            problemas.append(f"entrada {i}: id «{did}» inválido, repetido o del banco (D…/M…)")
            continue
        vistos.add(did)
        archivo = carpeta / str(d.get("archivo") or "")
        try:
            if archivo.resolve().parent != carpeta.resolve():
                raise OSError("fuera de la carpeta")
            codigo = archivo.read_text()
            arbol = ast.parse(codigo)
        except (OSError, SyntaxError, ValueError) as e:
            problemas.append(f"{did}: archivo ilegible o con errores de sintaxis ({e})")
            continue
        if not any(isinstance(n, ast.FunctionDef) and n.name == "medir" for n in arbol.body):
            problemas.append(f"{did}: no define medir(db, p)")
            continue
        lineas = d.get("lineas")
        n = len(codigo.splitlines())
        if not (isinstance(lineas, list) and len(lineas) == 2 and all(isinstance(x, int) for x in lineas)
                and 1 <= lineas[0] <= lineas[1] <= n):
            problemas.append(f"{did}: «lineas» debe ser [desde, hasta] dentro del archivo (1..{n})")
            continue
        clase = d.get("clase") if d.get("clase") in CLASES else "otra"
        out.append({"id": did, "descripcion": str(d.get("descripcion") or "")[:200], "clase": clase,
                    "codigo": codigo if codigo.endswith("\n") else codigo + "\n", "lineas": lineas})
    return out, problemas
