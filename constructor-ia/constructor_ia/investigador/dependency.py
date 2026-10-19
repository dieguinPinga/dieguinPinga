"""¿La siguiente pregunta depende de lo observado? Declarada frente a comprobable.

* DECLARADA: el modelo cita (en «depende_de») algo del ciclo anterior: su consulta, un hecho calculado o una
  afirmación respaldada de ese ciclo, o una duda que ese ciclo abrió. Es lo que el modelo DICE; no prueba nada.
* COMPROBABLE: el pedido nuevo usa un valor que apareció en los DATOS del resultado anterior y que el modelo no tenía
  antes de verlo (no figuraba en su contexto ni en su pedido anterior): un valor de filtro o un literal de su SQL.
  Eso sí demuestra que la evidencia cambió la decisión.

Citar un hecho anterior es una referencia, no aprendizaje: la reutilización de una conclusión respaldada cuenta como
comprobable solo si el pedido USA un valor que esa conclusión contiene.
"""
from __future__ import annotations

import re

from ..explorer import knowledge as K

_STR_RE = re.compile(r"'((?:''|\\.|[^'])*)'|\"((?:\"\"|\\.|[^\"])*)\"")
_NUM_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")


def _norm(v) -> str:
    s = K.fold(" ".join(str(v).split()))
    try:
        f = float(s)
        return repr(int(f)) if f == int(f) else repr(f)
    except ValueError:
        return s


def candidates(request: dict) -> list[str]:
    """Valores concretos que el pedido usa: el valor del filtro y los literales de su SQL (textos y cifras de 2+
    dígitos)."""
    m = request.get("medir") or {}
    out = []
    if m.get("filtro"):
        out.append(str(m["filtro"]["valor"]))
    sql = m.get("sql") or ""
    if sql:
        out += [a or b for a, b in _STR_RE.findall(sql)]
        out += [n for n in _NUM_RE.findall(_STR_RE.sub(" ", sql)) if len(n.lstrip("-").replace(".", "")) >= 2]
    return [c for c in dict.fromkeys(out) if str(c).strip()]


def assess(prev: dict | None, request: dict, prior_text: str) -> dict:
    """Dependencia del pedido nuevo respecto del ciclo anterior.

    prev: {"refs": [C#, F#, D# producidos por el ciclo anterior], "rows": filas del resultado anterior}
    prior_text: lo que el modelo tenía ANTES de ver esa evidencia (su contexto y su pedido anterior).
    """
    if prev is None:
        return {"tipo": "inicial", "declarada": False, "comprobable": False, "referencias": [], "anclas": []}
    declared = [r for r in request.get("depende_de") or [] if r in set(prev.get("refs") or [])]
    cells = {_norm(c) for row in prev.get("rows") or [] for c in row if c is not None and str(c).strip() != ""}
    prior = K.fold(prior_text or "")
    anchors = []
    for v in candidates(request):
        n = _norm(v)
        seen_before = re.search(r"(?<![\w.])" + re.escape(K.fold(str(v).strip())) + r"(?![\w.])", prior)
        if n in cells and not seen_before:
            anchors.append(str(v))
    kind = "comprobable" if anchors else ("declarada" if declared else "ninguna")
    return {"tipo": kind, "declarada": bool(declared), "comprobable": bool(anchors), "referencias": declared,
            "anclas": anchors[:5]}


def reuse(request: dict, cited_facts: dict) -> list[dict]:
    """Conclusiones respaldadas anteriores que el pedido cita: declarada siempre; comprobable solo si el pedido USA un
    valor que esa conclusión contiene (citarla sin usarla es una referencia, no reutilización).

    cited_facts: {"F12": {"respaldada": bool, "valores": set}} para las F# citadas.
    """
    out = []
    anchor_vals = {_norm(a) for a in candidates(request)}
    for ref in request.get("depende_de") or []:
        f = cited_facts.get(ref)
        if not f or not f.get("respaldada"):
            continue
        vals = {_norm(v) for v in f.get("valores") or ()}
        out.append({"hecho": ref, "declarada": True, "comprobable": bool(vals & anchor_vals)})
    return out
