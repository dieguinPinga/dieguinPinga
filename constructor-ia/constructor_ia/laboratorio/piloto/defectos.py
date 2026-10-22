"""Defectos EXTERNOS para evaluar E0 a ciegas (PILOTO.md, «Evaluación a ciegas de E0»).

Los escribe otra persona, sin conocer las reglas, en una carpeta con un `manifiesto.json`:

    [{"id": "X01", "archivo": "x01.py", "lineas": [desde, hasta], "clase": "cálculo",
      "descripcion": "una frase"}, ...]

* cada archivo es el instrumento COMPLETO con el defecto (una variante de la referencia del banco: misma pregunta y
  mismo contrato, medir(db, p));
* `lineas` son las líneas del defecto EN ESE ARCHIVO (solo se usan para puntuar);
* `clase` la declara quien lo escribe (cálculo, sobrescritura, condición, omisión u otra) y solo se usa para informar;
* OMISIONES: no hay una línea existente que sea «la causa», sino un PUNTO DE INSERCIÓN. Se declara con
  `"insercion": {"despues_de": N, "sangria": K}`: la instrucción que falta iría después de la línea N, con K espacios
  de sangría. `objetivo()` deriva de ahí las instrucciones aceptables (convención en PILOTO.md, «Ciclo v1»).

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
        ins = d.get("insercion")
        if ins is not None and not (isinstance(ins, dict) and isinstance(ins.get("despues_de"), int)
                                    and isinstance(ins.get("sangria"), int) and 0 <= ins["despues_de"] <= n
                                    and 0 <= ins["sangria"] <= 80):
            problemas.append(f"{did}: «insercion» debe ser {{\"despues_de\": 0..{n}, \"sangria\": columnas}}")
            continue
        out.append({"id": did, "descripcion": str(d.get("descripcion") or "")[:200], "clase": clase,
                    "codigo": codigo if codigo.endswith("\n") else codigo + "\n", "lineas": lineas,
                    "insercion": ins})
    return out, problemas


def _bloques(arbol):
    """(dueño, lista de instrucciones) de cada bloque del programa."""
    for nodo in ast.walk(arbol):
        for campo in ("body", "orelse", "finalbody"):
            lista = getattr(nodo, campo, None)
            if isinstance(lista, list) and lista and all(isinstance(x, ast.stmt) for x in lista):
                yield nodo, lista


def objetivo(defecto: dict) -> set[int]:
    """Líneas de inicio de las instrucciones que cuentan como «la causa» al puntuar una localización.

    * Defecto con líneas: las instrucciones cuya región editable (el encabezado, si es compuesta) toca esas líneas.
    * Omisión con punto de inserción (convención fijada ANTES de evaluar): la instrucción inmediatamente anterior y
      la inmediatamente posterior al hueco, dentro del mismo bloque; si falta una de las dos (el hueco está al
      principio o al final del bloque), también el encabezado del dueño del bloque (un if, un for…).
    """
    from . import parches as P
    codigo = defecto["codigo"]
    ins = defecto.get("insercion")
    if not ins:
        a, b = defecto["lineas"]
        return {u.lineno for u in P.instrucciones(codigo) if u.lineno <= b and a <= u.edit_end}
    n, k = ins["despues_de"], ins["sangria"]
    for dueno, lista in _bloques(ast.parse(codigo)):
        if lista[0].col_offset != k:
            continue
        inicio = lista[0].lineno
        fin = lista[-1].end_lineno
        if not (inicio - 1 <= n <= fin):
            continue
        antes = [s for s in lista if s.end_lineno <= n]
        despues = [s for s in lista if s.lineno > n]
        out = ({antes[-1].lineno} if antes else set()) | ({despues[0].lineno} if despues else set())
        if (not antes or not despues) and not isinstance(dueno, (ast.FunctionDef, ast.Module)):
            out.add(dueno.lineno)
        return out
    return set()
