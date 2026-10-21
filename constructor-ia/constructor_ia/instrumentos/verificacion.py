"""Verificación determinista de la salida de un instrumento.

* Contrato: un objeto JSON pequeño con «resultado» (contenido libre: lo inventa el instrumento), «cobertura» (qué se
  analizó: filas, ventana, muestreo…) y «limitaciones».
* Cobertura: lo que el instrumento DECLARA haber analizado se contrasta con lo que el canal realmente le entregó. Un
  instrumento no puede afirmar que analizó un millón de filas si recibió diez mil y ninguna consulta agregada (un
  COUNT, una suma…) devolvió esa cifra.
* Topes: si alguna consulta se recortó por un presupuesto operativo, el resultado es PARCIAL; se distingue si el
  instrumento lo declaró o no.

* Medición: que el programa termine sin error no significa que haya medido algo. Estados, de peor a mejor:
    - contrato_invalido: la salida no tiene la forma acordada;
    - error_de_calculo: el instrumento capturó una excepción y devolvió un resultado vacío o la mencionó en su salida;
    - sin_evidencia: no hay datos que respalden el resultado (0 filas analizadas, ninguna fila recibida o resultado
      vacío);
    - parcial: algún tope recortó los datos;
    - valida: resultado estructurado y respaldado por datos recibidos. Puede llevar advertencias (por ejemplo,
      excepciones capturadas que no vaciaron el resultado).

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


def _vacio(v) -> bool:
    return v is None or v == "" or (isinstance(v, (dict, list)) and (not v or all(_vacio(x) for x in (
        v.values() if isinstance(v, dict) else v))))


def verify(output, accounting: dict, agg_numbers: set, max_bytes: int, excepciones=None) -> dict:
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
        elif float(declared) <= 0:
            coverage = "nula"  # cero filas no respaldan nada, aunque algún resultado agregado haya sido 0
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
    excepciones = list(excepciones or [])
    rows_seen = sum(q["filas"] for q in queries)
    vacio = _vacio(output.get("resultado"))
    sin_datos = coverage == "nula" or rows_seen == 0 or vacio
    texto = json.dumps(output, ensure_ascii=False, default=str)
    mencionadas = [e for e in excepciones if any(len(x) >= 4 and x in texto
                                                 for x in (e.get("tipo") or "", (e.get("mensaje") or "")[:40]))]
    advertencias = []
    if problems:
        medicion = "contrato_invalido"
    elif excepciones and (sin_datos or mencionadas):
        medicion = "error_de_calculo"
    elif sin_datos:
        medicion = "sin_evidencia"
    elif truncated or needs:
        medicion = "parcial"
    else:
        medicion = "valida"
    if excepciones and medicion != "error_de_calculo":
        advertencias.append(f"el instrumento capturó {sum(e.get('veces', 1) for e in excepciones)} excepción(es) "
                            "sin que el resultado quedara vacío: revisar que no oculten un error")
    if coverage == "no_demostrada":
        advertencias.append("la cobertura declarada no coincide con lo recibido")
    motivos = {"error_de_calculo": "el instrumento capturó una excepción y " + (
                   "la informó en su salida" if mencionadas else "devolvió un resultado sin datos"),
               "sin_evidencia": "0 filas analizadas" if coverage == "nula" else (
                   "ninguna consulta devolvió filas" if rows_seen == 0 else "resultado vacío"),
               "contrato_invalido": "; ".join(problems)}
    return {"contrato": not problems, "problemas": problems, "bytes": size, "cobertura": coverage,
            "filas_recibidas": raw_rows, "truncada": truncated, "parcial_declarada": declared_partial,
            "parcial_sin_declarar": (truncated or bool(needs)) and not declared_partial,
            "necesidades": needs, "medicion": medicion, "motivo_medicion": motivos.get(medicion),
            "excepciones_capturadas": excepciones, "advertencias": advertencias}
