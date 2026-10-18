"""Extracción y validación estática del código devuelto por el modelo.

La validación es solo sintáctica/estructural (ast.parse, sin ejecutar nada):
nunca se aprueba código por su apariencia; la aprobación depende de las
pruebas ejecutadas en el sandbox.
"""
from __future__ import annotations

import ast
import re
import sys

FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[^\n]*\n(.*?)(?:```|\Z)", re.S)
MAX_CODE = 100_000
STDLIB = set(sys.stdlib_module_names)


def extract_code(text: str) -> tuple[str | None, str]:
    """Devuelve (código, nota). Prefiere bloques ```python; si no hay, intenta el texto completo."""
    blocks = []
    for m in FENCE_RE.finditer(text or ""):
        lang = m.group(1).lower()
        if lang in ("", "python", "py", "python3"):
            closed = text[m.end() - 3:m.end()] == "```"
            blocks.append((m.group(2), closed, lang))
    if blocks:
        python_blocks = [b for b in blocks if b[2] in ("python", "py", "python3")] or blocks
        code, closed, _ = max(python_blocks, key=lambda b: len(b[0]))
        note = "" if closed else "bloque de código sin cerrar (respuesta posiblemente cortada)"
        return code.strip("\n") + "\n", note
    stripped = (text or "").strip()
    if stripped:
        try:
            ast.parse(stripped)
            return stripped + "\n", "respuesta sin bloque de código; se usó el texto completo"
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            pass
    return None, "no se encontró un bloque de código Python en la respuesta"


def validate_code(code: str, spec: dict) -> list[str]:
    """Lista de problemas (vacía si la validación estática pasa)."""
    if len(code) > MAX_CODE:
        return [f"El código supera {MAX_CODE} caracteres"]
    try:
        tree = ast.parse(code, filename="solucion.py")
    except SyntaxError as e:
        return [f"Error de sintaxis en la línea {e.lineno}: {e.msg}"]
    except (ValueError, RecursionError, MemoryError) as e:
        return [f"El código no se pudo analizar: {type(e).__name__}"]
    problems = []
    if spec.get("mode", "funcion") == "funcion":
        fn = spec.get("function")
        defined = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
        if fn not in defined:
            problems.append(f"No se define la función '{fn}' en el nivel superior del módulo")
    bad = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            bad.add(node.module.split(".")[0])
    bad = sorted(m for m in bad if m not in STDLIB)
    if bad:
        problems.append("Usa módulos que no son de la biblioteca estándar (no disponibles): " + ", ".join(bad))
    return problems
