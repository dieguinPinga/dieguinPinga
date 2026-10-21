"""Banco del piloto: referencia, defectos sembrados, mutantes y juez. SOLO PARA EL LABORATORIO.

La referencia la escribió Claude para medir a los brazos; nunca se muestra a ningún brazo ni entra en un catálogo.
El juez la ejecuta con los parámetros visibles y con unos de RESERVA que ningún brazo usa; los brazos reciben el juez
como None (ver PILOTO.md, «El juez»).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass

REFERENCIA = '''import datetime


def medir(db, p):
    ancho = int(p.get("ancho_min", 15))
    top = int(p.get("top", 3))
    r = db.consultar("SELECT DATE_FORMAT(momento, '%Y-%m-%d %H:%i') AS minuto, valor FROM lecturas",
                     max_filas=50000)
    cobertura = {"leidas": 0, "validas": 0, "invalidas": 0}
    sumas = {}
    for minuto, valor in r.filas:
        cobertura["leidas"] += 1
        v = float(valor)
        if not 0.5 < v < 900:
            cobertura["invalidas"] += 1
            continue
        cobertura["validas"] += 1
        m = datetime.datetime.strptime(minuto, "%Y-%m-%d %H:%M")
        inicio = m - datetime.timedelta(minutes=m.minute % ancho)
        sumas[inicio] = sumas.get(inicio, 0.0) + v
    mejores = sorted(sumas.items(), key=lambda x: -x[1])[:top]
    ventanas = [{"inicio": i.strftime("%Y-%m-%d %H:%M"), "suma": round(s, 3)} for i, s in mejores]
    return {"resultado": {"ventanas": ventanas, "ancho_min": ancho},
            "cobertura": {"filas_analizadas": cobertura["leidas"], **cobertura},
            "limitaciones": ["valores fuera de (0.5, 900) se cuentan como inválidos"]}
'''

PREGUNTA = ("¿Cuáles son las «top» ventanas de «ancho_min» minutos (alineadas al reloj: un inicio por cada múltiplo "
            "de ancho_min dentro de la hora) con mayor suma de valores válidos de lecturas, y cuántas lecturas se "
            "leyeron, fueron válidas (0.5 < valor < 900) e inválidas?")
PARAMS_VISIBLES = {"ancho_min": 15, "top": 3}
PARAMS_RESERVA = {"ancho_min": 30, "top": 5}
MANIFIESTO = {"nombre": "ventanas", "proposito": PREGUNTA, "parametros": PARAMS_VISIBLES,
              "salida": {"resultado": {"ventanas": [{"inicio": "AAAA-MM-DD HH:MM", "suma": 0.0}], "ancho_min": 15},
                         "cobertura": {"filas_analizadas": 0, "leidas": 0, "validas": 0, "invalidas": 0}}}


@dataclass
class Defecto:
    id: str
    titulo: str
    codigo: str
    sembrada: str          # texto de la instrucción sembrada (para el oráculo y para medir la localización)


def _sembrar(original: str, nuevo: str) -> str:
    assert REFERENCIA.count(original) == 1, original
    return REFERENCIA.replace(original, nuevo)


DEFECTOS = {
    "D1": Defecto("D1", "etiqueta desplazada (observado)", _sembrar(
        "inicio = m - datetime.timedelta(minutes=m.minute % ancho)",
        "inicio = m - datetime.timedelta(minutes=m.minute % ancho + ancho)"),
        "inicio = m - datetime.timedelta(minutes=m.minute % ancho + ancho)"),
    "D2": Defecto("D2", "contador reiniciado (observado)", _sembrar(
        "    mejores = sorted(",
        '    if "cobertura" not in cobertura:\n'
        '        cobertura = {"leidas": 0, "validas": 0, "invalidas": 0}\n'
        "    mejores = sorted("),
        'if "cobertura" not in cobertura:'),
}


def lineas_de(codigo: str, texto: str) -> tuple[int, int]:
    """Rango de líneas (1-based) de la instrucción que contiene el texto sembrado."""
    tree = ast.parse(codigo)
    objetivo = next(i for i, ln in enumerate(codigo.splitlines(), 1) if texto in ln)
    mejor = None
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and not isinstance(node, ast.FunctionDef) and \
                node.lineno <= objetivo <= node.end_lineno:
            if mejor is None or (node.end_lineno - node.lineno) <= (mejor.end_lineno - mejor.lineno):
                mejor = node
    if isinstance(mejor, (ast.If, ast.For, ast.While)):
        return mejor.lineno, mejor.end_lineno
    return mejor.lineno, mejor.end_lineno


# Mutantes de un solo punto (estudio sin modelo) -----------------------------------------------------------

_CMP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq}
_BIN = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mod: ast.FloorDiv, ast.FloorDiv: ast.Mod, ast.Mult: ast.Div,
        ast.Div: ast.Mult}


@dataclass
class Mutante:
    id: str
    descripcion: str
    codigo: str
    lineas: tuple[int, int]   # instrucción mutada, en el código del mutante


def _stmt_de(tree, target) -> ast.stmt:
    padres = {}
    for n in ast.walk(tree):
        for h in ast.iter_child_nodes(n):
            padres[h] = n
    n = target
    while not isinstance(n, ast.stmt):
        n = padres[n]
    return n


def mutantes(codigo: str = REFERENCIA) -> list[Mutante]:
    """Todas las mutaciones de un punto dentro de medir: constante numérica ±1, comparación cambiada, operador
    aritmético cambiado e instrucción eliminada (reemplazada por pass)."""
    base = ast.parse(codigo)
    puntos = []
    medir = next(n for n in base.body if isinstance(n, ast.FunctionDef) and n.name == "medir")
    for i, node in enumerate(ast.walk(medir)):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            puntos += [(i, "const+1"), (i, "const-1")]
        elif isinstance(node, ast.Compare):
            puntos += [(i, f"cmp{k}") for k, op in enumerate(node.ops) if type(op) in _CMP]
        elif isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            puntos.append((i, "bin"))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            puntos.append((i, "not"))
        if isinstance(node, ast.stmt) and not isinstance(node, (ast.FunctionDef, ast.Return)) and node is not medir:
            puntos.append((i, "del"))
    out = []
    for idx, tipo in puntos:
        tree = ast.parse(codigo)
        f = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "medir")
        node = list(ast.walk(f))[idx]
        stmt = _stmt_de(tree, node)
        marca = ast.Constant(value="__MUTADO__")
        if tipo.startswith("const"):
            node.value = node.value + (1 if tipo == "const+1" else -1)
            desc = f"constante {tipo[-2:]} (línea {node.lineno} de la referencia)"
        elif tipo.startswith("cmp"):
            k = int(tipo[3:])
            node.ops[k] = _CMP[type(node.ops[k])]()
            desc = f"comparación cambiada (línea {node.lineno} de la referencia)"
        elif tipo == "bin":
            node.op = _BIN[type(node.op)]()
            desc = f"operador aritmético cambiado (línea {node.lineno} de la referencia)"
        elif tipo == "not":
            node_padre = _padre(tree, node)
            _reemplazar(node_padre, node, node.operand)
            desc = f"«not» eliminado (línea {node.lineno} de la referencia)"
        else:
            nuevo = ast.Expr(value=marca)
            _reemplazar(_padre(tree, node), node, nuevo)
            stmt = nuevo
            desc = f"instrucción eliminada (línea {node.lineno} de la referencia)"
        stmt.__dict__["_mutado"] = True
        ast.fix_missing_locations(tree)
        src = ast.unparse(tree).replace("'__MUTADO__'", "pass")
        nuevo_tree = ast.parse(src)
        lineas = _ubicar_mutada(tree, nuevo_tree)
        if src.strip() == ast.unparse(ast.parse(codigo)).strip() or lineas is None:
            continue
        out.append(Mutante(f"M{len(out) + 1:03d}", desc, src + "\n", lineas))
    return out


def _padre(tree, target):
    for n in ast.walk(tree):
        for campo, valor in ast.iter_fields(n):
            if valor is target or (isinstance(valor, list) and any(v is target for v in valor)):
                return n
    raise ValueError("sin padre")


def _reemplazar(padre, viejo, nuevo) -> None:
    for campo, valor in ast.iter_fields(padre):
        if valor is viejo:
            setattr(padre, campo, nuevo)
            return
        if isinstance(valor, list):
            for i, v in enumerate(valor):
                if v is viejo:
                    valor[i] = nuevo
                    return


def _ubicar_mutada(tree_mutado, tree_reparseado):
    """Líneas de la instrucción mutada en el código regenerado: se recorren ambos árboles en paralelo."""
    a = [n for n in ast.walk(tree_mutado) if isinstance(n, ast.stmt)]
    b = [n for n in ast.walk(tree_reparseado) if isinstance(n, ast.stmt)]
    for x, y in zip(a, b):
        if x.__dict__.get("_mutado"):
            return (y.lineno, y.end_lineno)
    return None


# Juez -----------------------------------------------------------------------------------------------------

def hojas(v, camino=()):
    if isinstance(v, dict):
        for k, x in v.items():
            yield from hojas(x, camino + (str(k),))
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from hojas(x, camino + (i,))
    else:
        yield camino, v


def comparar(candidata, referencia, tol: float = 1e-3) -> dict:
    """Diferencias hoja por hoja (sin saber nada del dominio). Devuelve {"igual", "campos"}: los nombres de las
    claves cuyas hojas difieren (los índices de listas se omiten)."""
    if not isinstance(candidata, dict) or not isinstance(referencia, dict):
        return {"igual": False, "campos": ["(salida)"]}
    a = dict(hojas({k: candidata.get(k) for k in ("resultado", "cobertura")}))
    b = dict(hojas({k: referencia.get(k) for k in ("resultado", "cobertura")}))
    campos = set()
    for camino in set(a) | set(b):
        x, y = a.get(camino, "∅"), b.get(camino, "∅")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and not isinstance(x, bool):
            distinto = abs(float(x) - float(y)) > tol
        else:
            distinto = x != y
        if distinto:
            nombres = [c for c in camino if isinstance(c, str)]
            campos.add(nombres[-1] if nombres else "(salida)")
    return {"igual": not campos, "campos": sorted(campos)}
