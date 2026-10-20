"""Verificación determinista de la salida de un instrumento.

* Contrato: un objeto JSON pequeño con «resultado» (contenido libre: lo inventa el instrumento), «cobertura» (qué se
  analizó: filas, ventana, muestreo…) y «limitaciones».
* Cobertura: lo que el instrumento DECLARA haber analizado se contrasta con lo que el canal realmente le entregó. Un
  instrumento no puede afirmar que analizó un millón de filas si recibió diez mil y ninguna consulta agregada (un
  COUNT, una suma…) devolvió esa cifra.
* Topes: si alguna consulta se recortó por un presupuesto operativo, el resultado es PARCIAL; se distingue si el
  instrumento lo declaró o no.

Esto no decide si la medición responde la pregunta: eso lo evalúa el modelo y, en el laboratorio, la verdad conocida.
"""
from __future__ import annotations

import json
import re


def _num(v) -> str | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return str(int(f)) if f == int(f) else repr(f)


def verify(output, accounting: dict, agg_numbers: set, max_bytes: int) -> dict:
    problems = []
    size = len(json.dumps(output, ensure_ascii=False, default=str).encode()) if output is not None else 0
    if not isinstance(output, dict):
        problems.append("la salida no es un objeto JSON")
        output = {}
    if size > max_bytes:
        problems.append(f"la salida ocupa {size} bytes (máximo {max_bytes}): un instrumento sintetiza, no transfiere")
    for k in ("resultado", "cobertura", "limitaciones"):
        if k not in output:
            problems.append(f"falta «{k}»")
    cov = output.get("cobertura") if isinstance(output.get("cobertura"), dict) else {}
    if "cobertura" in output and not isinstance(output.get("cobertura"), dict):
        problems.append("«cobertura» debe ser un objeto (filas_analizadas, ventana, muestreo…)")
    queries = [q for q in accounting.get("consultas", []) if q.get("op") == "consultar" and q.get("estado") == "ok"]
    raw_rows = sum(q["filas"] for q in queries if not q.get("agregada"))
    declared = cov.get("filas_analizadas")
    if declared is None:
        coverage = "no_declarada"
    else:
        d = _num(declared)
        agg = {_num(x) for x in agg_numbers}
        if d is None:
            coverage = "no_declarada"
        elif d in agg or (raw_rows and float(declared) == raw_rows):
            coverage = "demostrada"
        elif raw_rows and float(declared) <= raw_rows:
            coverage = "compatible"  # analizó parte de lo recibido
        else:
            coverage = "no_demostrada"
    truncated = any(q.get("truncado") for q in queries)
    text = json.dumps({"c": cov, "l": output.get("limitaciones")}, ensure_ascii=False, default=str).lower()
    declared_partial = bool(re.search(r"parcial|truncad|tope|incomplet|muestra", text))
    needs = accounting.get("necesidades") or []
    return {"contrato": not problems, "problemas": problems, "bytes": size, "cobertura": coverage,
            "filas_recibidas": raw_rows, "truncada": truncated, "parcial_declarada": declared_partial,
            "parcial_sin_declarar": (truncated or bool(needs)) and not declared_partial,
            "necesidades": needs}
