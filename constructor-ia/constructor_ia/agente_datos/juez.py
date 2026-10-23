"""Juez del laboratorio: compara el informe de una misión con la VERDAD oculta del generador.

Corre DESPUÉS de la misión y en otro proceso. El agente nunca recibe la verdad, su ruta ni el veredicto (el
veredicto no se escribe en agente.db). Métrica principal: CONFIRMACIONES FALSAS, es decir, respuestas que el
coordinador dio por «verificadas» y que la verdad marca como incorrectas.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path


def evaluar(informe: dict, verdad: dict, pregunta_id: str) -> dict:
    v = verdad["preguntas"][pregunta_id]
    nivel = informe["nivel"]
    r = (informe.get("respuesta") or {}).get("estructurada")
    if nivel == "abstencion" or not r:
        return {"pregunta": pregunta_id, "nivel": nivel, "correcta": None, "confirmacion_falsa": False,
                "error_declarado": False, "abstencion": True}
    grupo = str(r["grupo"][0]) if r["grupo"] else None
    try:
        valor_ok = Decimal(str(r["valor"])) == Decimal(str(v["valor"]))
    except Exception:
        valor_ok = False
    # Desde la fase 2c la respuesta trae la CLAVE aparte de la etiqueta: si la verdad la tiene, también se compara
    # (dos clientes con el mismo nombre no son la misma respuesta).
    clave_ok = True
    if v.get("clave") is not None and r.get("clave"):
        clave_ok = str(r["clave"][0]) == str(v["clave"])
    correcta = grupo == v["respuesta"] and valor_ok and clave_ok
    return {"pregunta": pregunta_id, "nivel": nivel, "correcta": correcta,
            "confirmacion_falsa": nivel == "verificada" and not correcta,
            "error_declarado": nivel == "incierta" and not correcta, "abstencion": False,
            "respondio": [grupo, r["valor"]] + ([r["clave"][0]] if r.get("clave") else [])}


def auditar_fugas(rutas: list[Path], verdad: dict) -> list[str]:
    """El canario y las descripciones de la verdad no pueden aparecer en ningún archivo del agente (prompts,
    respuestas, informes, decisiones ni la memoria)."""
    fugas = []
    marcas = [verdad["canario"]] + list(verdad["tablas"].values())
    for p in rutas:
        p = Path(p)
        archivos = [p] if p.is_file() else [x for x in p.rglob("*") if x.is_file()]
        for f in archivos:
            datos = f.read_bytes()
            for m in marcas:
                if m.encode() in datos:
                    fugas.append(f"{f.name}: {m[:40]}")
    return fugas


def cargar(path: Path) -> dict:
    return json.loads(Path(path).read_text())
