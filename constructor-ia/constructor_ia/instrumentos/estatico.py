"""Revisión estática de un instrumento antes de ejecutarlo (defensa ADICIONAL: la barrera real es el sandbox).

Rechaza lo que un instrumento de medición no necesita y que solo amplía la superficie de ataque: importar módulos
fuera de la lista permitida, evaluar código dinámico, abrir archivos, acceder a atributos internos («__x__»).
La lista de módulos es conservadora y EXTENSIBLE: una capacidad nueva, una vez verificada, se agrega en
[instrumentos] modulos_extra sin tocar la arquitectura. Los instrumentos anteriores reutilizados como bibliotecas se
permiten por su nombre de módulo.
"""
from __future__ import annotations

import ast
import re

# Biblioteca estándar suficiente para medir y sintetizar (sin red, archivos, procesos ni introspección).
BASE_MODULES = frozenset({"math", "statistics", "collections", "datetime", "json", "itertools", "bisect", "decimal",
                          "re", "functools", "heapq", "fractions", "random", "operator", "string", "investiga"})
BANNED_CALLS = frozenset({"eval", "exec", "compile", "open", "__import__", "input", "breakpoint", "globals", "locals",
                          "vars", "getattr", "setattr", "delattr", "memoryview", "exit", "quit", "help"})
# Nunca se habilitan, ni siquiera por configuración: procesos, archivos, red, introspección, carga de código.
NEVER_MODULES = frozenset({"os", "sys", "subprocess", "socket", "ctypes", "importlib", "builtins", "pickle", "marshal",
                           "shutil", "pathlib", "io", "threading", "multiprocessing", "signal", "inspect", "gc", "code",
                           "codeop", "runpy", "pty", "fcntl", "resource", "mmap", "select", "selectors", "asyncio",
                           "urllib", "http", "ftplib", "smtplib", "ssl", "tempfile", "glob", "posix", "_thread", "types",
                           "sqlite3", "zipimport", "webbrowser"})
LIB_RE = re.compile(r"^[a-z][a-z0-9_]{0,60}$")


def allowed_modules(extra=()) -> frozenset:
    return (BASE_MODULES | frozenset(extra or ())) - NEVER_MODULES


def check(code: str, *, extra_modules=(), libraries=(), max_bytes: int = 24 * 1024) -> list[str]:
    """Problemas encontrados (lista vacía = aceptable para ejecutar en el sandbox)."""
    problems: list[str] = []
    if not isinstance(code, str) or not code.strip():
        return ["el instrumento está vacío"]
    if len(code.encode()) > max_bytes:
        return [f"el instrumento supera {max_bytes // 1024} KB"]
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"error de sintaxis en la línea {e.lineno}: {e.msg}"]
    allowed = allowed_modules(extra_modules) | {m for m in libraries if LIB_RE.match(m) and m not in NEVER_MODULES}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in allowed:
                    problems.append(f"línea {node.lineno}: import {a.name} no está permitido")
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in allowed:
                problems.append(f"línea {node.lineno}: from {node.module} import … no está permitido")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            problems.append(f"línea {node.lineno}: acceso a atributo interno «{node.attr}»")
        elif isinstance(node, ast.Name) and node.id.startswith("__") and node.id != "__name__":
            problems.append(f"línea {node.lineno}: nombre interno «{node.id}»")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
            problems.append(f"línea {node.lineno}: {node.func.id}() no está permitido en un instrumento")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append(f"línea {node.lineno}: global/nonlocal no está permitido")
    medir = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "medir"]
    if not medir:
        problems.append("falta la función medir(db, p) a nivel de módulo")
    elif len(medir[0].args.args) < 2:
        problems.append("medir debe recibir dos argumentos: medir(db, p)")
    return list(dict.fromkeys(problems))[:20]
