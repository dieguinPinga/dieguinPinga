"""Localizador automático (sin modelo y sin saber nada del dominio).

Entrada: el código y las verificaciones que fallan. Cada falla puede traer el LUGAR de una excepción (línea) y/o los
CAMPOS de la salida que están mal (nombres de claves). Salida: hasta k instrucciones candidatas, ordenadas.

1. Lugar de una excepción → esa instrucción primero.
2. Campos → «rebanada hacia atrás» estática: desde las expresiones que producen esos campos en la salida, se
   siguen las instrucciones que definen cada nombre usado, salto por salto.
3. Orden: menos saltos primero; a igual distancia, la definición más tardía primero (la última escritura es la que
   llega a la salida). Las instrucciones que solo arman la salida van al final.

Es heurístico y se mide como tal (PILOTO.md, desafío 2 y R5): no se ajustó a los defectos del banco.
"""
from __future__ import annotations

import ast

from .parches import Unidad, instrucciones, unidad_en

MAX_SALTOS = 6


def _nombres(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} if node is not None else set()


def _base(target) -> str | None:
    while isinstance(target, (ast.Subscript, ast.Attribute)):
        target = target.value
    return target.id if isinstance(target, ast.Name) else None


def _defs_uses(u: Unidad) -> tuple[set[str], set[str]]:
    s = u.nodo
    defs, uses = set(), set()
    if isinstance(s, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
        targets = s.targets if isinstance(s, ast.Assign) else [s.target]
        for t in targets:
            for el in (t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t]):
                b = _base(el)
                if b:
                    defs.add(b)
                if isinstance(el, ast.Subscript):          # x[k] = v usa k y x
                    uses |= _nombres(el)
        uses |= _nombres(s.value)
        if isinstance(s, ast.AugAssign):
            uses |= defs
    elif isinstance(s, ast.For):
        defs |= _nombres(s.target)
        uses |= _nombres(s.iter)
    elif isinstance(s, (ast.If, ast.While)):
        uses |= _nombres(s.test)
    elif isinstance(s, ast.Expr):
        uses |= _nombres(s.value)
        if isinstance(s.value, ast.Call) and isinstance(s.value.func, ast.Attribute):
            b = _base(s.value.func.value)
            if b:
                defs.add(b)                                  # x.append(...), x.update(...)
    elif isinstance(s, ast.Return):
        uses |= _nombres(s.value)
    return defs, uses


def _semillas(codigo: str, campos: set[str]) -> tuple[set[str], set[int]]:
    """Nombres que producen los campos en la salida y líneas de las instrucciones que arman esa salida."""
    tree = ast.parse(codigo)
    nombres, lineas = set(), set()
    comprehension_iters = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)):
            for g in n.generators:
                for t in _nombres(g.target):
                    comprehension_iters.setdefault(t, set()).update(_nombres(g.iter))
    en_return = {id(x) for r in ast.walk(tree) if isinstance(r, ast.Return) for x in ast.walk(r)}
    for n in ast.walk(tree):
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value in campos:
                    usados = _nombres(v)
                    for x in list(usados):
                        usados |= comprehension_iters.get(x, set())
                    nombres |= usados
                    if id(n) in en_return:      # solo el armado de la salida devuelta va al final
                        lineas.add(k.lineno)
        elif isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and n.slice.value in campos:
            b = _base(n)
            if b:
                nombres.add(b)
        elif isinstance(n, ast.Name) and n.id in campos:
            nombres.add(n.id)
    return nombres, lineas


def localizar(codigo: str, fallas: list[dict], k: int = 3) -> list[dict]:
    """[{"unidad": Unidad, "motivo": str}] — hasta k candidatas."""
    unidades = [u for u in instrucciones(codigo)]
    cands: dict[int, dict] = {}

    def agregar(u: Unidad, orden: tuple, motivo: str):
        if u.lineno not in cands or orden < cands[u.lineno]["orden"]:
            cands[u.lineno] = {"unidad": u, "orden": orden, "motivo": motivo}

    for f in fallas:
        linea = (f.get("lugar") or {}).get("linea")
        if linea:
            u = unidad_en(codigo, int(linea))
            if u:
                agregar(u, (-1, 0), f"excepción en la línea {linea}")
    campos = {c for f in fallas for c in (f.get("campos") or [])}
    sin_campos = not campos
    if sin_campos:
        campos = {"resultado", "cobertura", "filas_analizadas"}
    nombres, lineas_salida = _semillas(codigo, campos)
    info = [(u, *_defs_uses(u)) for u in unidades]
    vistos, frontera, salto = set(), set(nombres), 0
    while frontera and salto < MAX_SALTOS:
        salto += 1
        siguiente = set()
        for nombre in frontera - vistos:
            for u, defs, uses in info:
                if nombre in defs:
                    arma_salida = u.lineno in lineas_salida or isinstance(u.nodo, ast.Return)
                    agregar(u, (salto + (100 if arma_salida else 0), -u.lineno),
                            f"define «{nombre}» ({salto} salto{'s' if salto > 1 else ''} desde "
                            f"{'la salida' if sin_campos else 'los campos ' + ', '.join(sorted(campos))})")
                    siguiente |= uses
        vistos |= frontera
        frontera = siguiente - vistos
    orden = sorted(cands.values(), key=lambda c: c["orden"])
    return [{"unidad": c["unidad"], "motivo": c["motivo"]} for c in orden[:k]]


def acierta(candidatas: list[dict], lineas: tuple[int, int]) -> int | None:
    """Posición (1-based) de la primera candidata cuya región EDITABLE se superpone con las líneas sembradas."""
    for i, c in enumerate(candidatas, 1):
        u = c["unidad"]
        if u.lineno <= lineas[1] and lineas[0] <= u.edit_end:
            return i
    return None
