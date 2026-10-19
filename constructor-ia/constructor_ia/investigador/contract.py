"""Contrato entre el investigador (Qwen3), el motor de evidencia y la memoria.

El modelo nunca escribe una herramienta ni sus argumentos: dice QUÉ quiere medir y SOBRE QUÉ objetos. El motor lo
traduce a SQL verificable o devuelve una aclaración. Dos respuestas posibles del modelo:

* PEDIDO (solo al empezar una línea, o si la lectura anterior no trajo un siguiente válido):
  {"pregunta", "porque", "depende_de": [C#|F#|D#], "medir": {...}}
* LECTURA (después de recibir la evidencia REAL; interpreta y formula la siguiente pregunta en la misma respuesta):
  {"lectura", "afirmaciones": [{texto, evidencia, alcance}], "dudas": [...], "cierra": [D#], "siguiente": PEDIDO}
"""
from __future__ import annotations

import json
import re

# Qué se puede pedir medir (vocabulario abierto: «sql» permite cualquier SELECT de solo lectura).
QUE = ("conteo", "distribucion", "rango", "ejemplos", "relacion", "tendencia", "sql")
QUE_HELP = {
    "conteo": "cuántas filas (de una tabla, o con un filtro)",
    "distribucion": "valores más frecuentes de una columna",
    "rango": "nulos, distintos, mínimo, máximo y promedio de una columna",
    "ejemplos": "filas reales de una tabla (o de sus columnas indicadas)",
    "relacion": "si los valores de una columna existen en otra (dos objetos tabla.columna)",
    "tendencia": "cantidad por día de una columna de fecha",
    "sql": "un SELECT propio de solo lectura en «sql»",
}
REF_RE = re.compile(r"^[CFD]\d{1,9}$")
MAX_ENUM_NAMES = 160  # hasta aquí, los nombres reales van como opciones de la gramática (no se pueden inventar)


def medir_schema(names: list[str] | None) -> dict:
    obj = {"type": "string", "enum": names} if names else {"type": "string", "maxLength": 80}
    return {"type": "object", "properties": {
        "que": {"type": "string", "enum": list(QUE)},
        "objetos": {"type": "array", "maxItems": 3, "items": obj},
        "filtro": {"type": "object", "properties": {"objeto": obj, "valor": {"type": "string", "maxLength": 80}},
                   "required": ["objeto", "valor"]},
        "sql": {"type": "string", "maxLength": 500},
        "remedir": {"type": "boolean"}},
        "required": ["que"]}


def pedido_schema(names: list[str] | None) -> dict:
    return {"type": "object", "properties": {
        "pregunta": {"type": "string", "maxLength": 200},
        "porque": {"type": "string", "maxLength": 200},
        "depende_de": {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": 12}},
        "medir": medir_schema(names),
        "nueva_linea": {"type": "boolean"}},
        "required": ["pregunta", "porque", "medir"]}


def lectura_schema(names: list[str] | None) -> dict:
    return {"type": "object", "properties": {
        "lectura": {"type": "string", "maxLength": 240},
        "afirmaciones": {"type": "array", "maxItems": 2, "items": {"type": "object", "properties": {
            "texto": {"type": "string", "maxLength": 280},
            "evidencia": {"type": "array", "maxItems": 2, "items": {"type": "string", "maxLength": 12}},
            "alcance": {"type": "string", "enum": ["muestra", "subconjunto", "tabla"]}},
            "required": ["texto", "evidencia"]}},
        "dudas": {"type": "array", "maxItems": 2, "items": {"type": "string", "maxLength": 200}},
        "cierra": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 12}},
        "siguiente": pedido_schema(names)},
        "required": ["lectura", "siguiente"]}


def refs(items) -> list[str]:
    """Referencias válidas en forma (C#, F#, D#); lo demás se descarta."""
    out = []
    for x in items if isinstance(items, list) else []:
        x = str(x).strip().upper()
        if REF_RE.match(x) and x not in out:
            out.append(x)
    return out[:6]


def normalize_pedido(d) -> dict | None:
    """Pedido en forma canónica, o None si no es un pedido (sin pregunta o sin «medir.que»)."""
    if not isinstance(d, dict):
        return None
    m = d.get("medir") if isinstance(d.get("medir"), dict) else {}
    que = str(m.get("que") or "").strip().lower()
    question = " ".join(str(d.get("pregunta") or "").split())
    if not question or que not in QUE:
        return None
    filtro = m.get("filtro") if isinstance(m.get("filtro"), dict) else None
    if filtro and not (str(filtro.get("objeto") or "").strip() and str(filtro.get("valor") or "") != ""):
        filtro = None
    return {"pregunta": question[:300], "porque": " ".join(str(d.get("porque") or "").split())[:300],
            "depende_de": refs(d.get("depende_de")), "nueva_linea": bool(d.get("nueva_linea")),
            "medir": {"que": que,
                      "objetos": [str(o).strip() for o in (m.get("objetos") or []) if str(o).strip()][:3],
                      "filtro": {"objeto": str(filtro["objeto"]).strip(), "valor": str(filtro["valor"])[:120]}
                      if filtro else None,
                      "sql": str(m.get("sql") or "").strip()[:2000] or None,
                      "remedir": bool(m.get("remedir"))}}


def parse_json(text: str):
    try:
        data = json.loads(text[text.find("{"):text.rfind("}") + 1])
        return data if isinstance(data, dict) else None
    except ValueError:
        return None
