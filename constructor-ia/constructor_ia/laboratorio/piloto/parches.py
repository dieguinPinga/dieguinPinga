"""Parches por instrucción: unidades editables, ensamblado determinista y verificación propia del coordinador.

Una microtarea pide reemplazar o eliminar UNA instrucción (o el encabezado de una instrucción compuesta). El
coordinador ensambla la respuesta sin inferencia y se verifica a sí mismo antes de culpar al modelo:

* el fragmento debe compilar SOLO (si no, la falla es del modelo);
* el programa ensamblado debe compilar y las líneas fuera de la región deben quedar idénticas (si el fragmento
  compilaba solo y esto falla, la falla es del COORDINADOR y no se le cobra al modelo).
"""
from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass

FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\s*$")
COMPUESTAS = (ast.If, ast.For, ast.While, ast.With, ast.Try)


@dataclass
class Unidad:
    lineno: int
    end_lineno: int            # fin de la instrucción completa
    edit_end: int              # fin de la región editable (el encabezado, si es compuesta)
    col: int
    compuesta: bool
    unica_en_bloque: bool
    nodo: ast.stmt

    @property
    def id(self) -> str:
        return f"L{self.lineno}"


def instrucciones(codigo: str) -> list[Unidad]:
    tree = ast.parse(codigo)
    out = []

    def bloque(stmts):
        for s in stmts:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bloque(s.body)
                continue
            compuesta = isinstance(s, COMPUESTAS)
            edit_end = (s.body[0].lineno - 1) if compuesta else s.end_lineno
            out.append(Unidad(s.lineno, s.end_lineno, max(s.lineno, edit_end), s.col_offset, compuesta,
                              len(stmts) == 1, s))
            for campo in ("body", "orelse", "finalbody"):
                hijos = getattr(s, campo, None)
                if hijos:
                    bloque(hijos)
            for h in getattr(s, "handlers", []) or []:
                bloque(h.body)

    bloque(tree.body)
    return out


def unidad_en(codigo: str, linea: int) -> Unidad | None:
    """La instrucción más chica que contiene la línea."""
    cands = [u for u in instrucciones(codigo) if u.lineno <= linea <= u.end_lineno]
    return min(cands, key=lambda u: u.end_lineno - u.lineno) if cands else None


def mostrar(codigo: str, u: Unidad, contexto: int = 2) -> str:
    lines = codigo.splitlines()
    a, b = max(1, u.lineno - contexto), min(len(lines), u.edit_end + contexto)
    return "\n".join(f"{'>>' if u.lineno <= i <= u.edit_end else '  '} {i:3d} | {lines[i - 1]}" for i in range(a, b + 1))


def limpiar_fragmento(texto: str) -> str:
    lines = [ln for ln in (texto or "").splitlines() if not FENCE_RE.match(ln.strip())]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return textwrap.dedent("\n".join(lines))


@dataclass
class Ensamblado:
    codigo: str | None
    atribucion: str | None     # None (ok) | "modelo" | "coordinador"
    detalle: str


def aplicar(codigo: str, u: Unidad, accion: str, fragmento: str = "") -> Ensamblado:
    lines = codigo.splitlines()
    pre, post = lines[:u.lineno - 1], lines[(u.end_lineno if accion == "eliminar" else u.edit_end):]
    sangria = " " * u.col
    if accion == "eliminar":
        nuevo = [sangria + "pass"] if u.unica_en_bloque else []
    elif accion == "reemplazar":
        frag = limpiar_fragmento(fragmento)
        if not frag.strip():
            return Ensamblado(None, "modelo", "el fragmento está vacío")
        if u.compuesta and not frag.rstrip().endswith(":"):
            return Ensamblado(None, "modelo", "el encabezado de una instrucción compuesta debe terminar en «:» "
                                              "(para quitar todo el bloque, la acción es «eliminar»)")
        prueba = frag + ("\n    pass" if u.compuesta else "")
        try:
            ast.parse(prueba)
        except SyntaxError as e:
            return Ensamblado(None, "modelo", f"el fragmento no compila solo: {e.msg} (línea {e.lineno})")
        nuevo = [(sangria + ln) if ln.strip() else "" for ln in frag.splitlines()]
    else:
        return Ensamblado(None, "modelo", f"acción desconocida: {accion}")
    resultado = pre + nuevo + post
    texto = "\n".join(resultado) + "\n"
    if resultado[:len(pre)] != pre or (post and resultado[-len(post):] != post):
        return Ensamblado(None, "coordinador", "el ensamblado alteró líneas fuera de la región")
    try:
        ast.parse(texto)
    except SyntaxError as e:
        return Ensamblado(None, "coordinador", f"el fragmento compilaba solo pero el ensamblado no: {e.msg} "
                                               f"(línea {e.lineno})")
    return Ensamblado(texto, None, "ok")
