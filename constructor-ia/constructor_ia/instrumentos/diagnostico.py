"""Diagnóstico compacto de una ejecución de instrumento, para que un modelo pueda repararlo.

No es un volcado de registros: reúne lo que hace falta para encontrar la causa de una falla y entra en pocas líneas.

* Qué pasó: estado técnico (¿terminó?) y estado de la medición (¿midió algo?), por separado.
* Dónde: tipo de excepción, mensaje original, archivo, línea y el código de esa línea; también las excepciones que
  el instrumento capturó por su cuenta (un error atrapado que devuelve un resultado vacío no es una medición).
* Con qué datos: cada consulta con sus filas y, por columna, el tipo MariaDB, el tipo Python con el que llegó y un
  ejemplo (un DECIMAL llega como texto: compararlo con un número falla).
* Qué devolvió: la salida (recortada) y la verificación determinista.
* Historia: cada intento con su firma de error y qué cambió; si el mismo error persiste, se dice explícitamente.
"""
from __future__ import annotations

import difflib
import json
import re

MAX_CONSULTAS = 6


def _j(v, limit: int) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def _norm(msg: str) -> str:
    return re.sub(r"\d+", "N", str(msg or "").strip().splitlines()[0] if msg else "")[:160]


def firma(run: dict) -> str:
    """Identidad de la falla, estable entre versiones (sin números de línea ni cifras): sirve para detectar que una
    reparación no cambió el error. Vacía si no hay falla."""
    v = run.get("verdict") or {}
    if v.get("falla"):
        return f"{v['falla'].get('tipo')}: {_norm(v['falla'].get('mensaje'))}"
    if run.get("status") not in ("ok", None):
        return f"{run.get('status')}: {_norm(run.get('error'))}"
    med = v.get("medicion")
    if med in ("error_de_calculo", "sin_evidencia", "contrato_invalido"):
        exc = (v.get("excepciones_capturadas") or [None])[0]
        return f"{exc['tipo']}: {_norm(exc['mensaje'])}" if exc else f"medicion: {med}"
    return ""


def _lugar_txt(lugar: dict | None) -> str:
    if not lugar or not lugar.get("linea"):
        return ""
    return (f" en {lugar.get('archivo') or 'instrumento.py'} línea {lugar['linea']}"
            + (f" ({lugar['funcion']})" if lugar.get("funcion") else "")
            + (f": `{lugar['codigo']}`" if lugar.get("codigo") else ""))


def diagnosticar(run: dict) -> dict:
    v = run.get("verdict") or {}
    acc = run.get("accounting") or {}
    consultas = [q for q in acc.get("consultas") or [] if q.get("op") == "consultar"]
    out = {"estado": run.get("status"), "medicion": v.get("medicion"), "motivo_medicion": v.get("motivo_medicion"),
           "firma": firma(run), "falla": v.get("falla"), "excepciones_capturadas": v.get("excepciones_capturadas") or [],
           "consultas": [{"consulta": q.get("consulta"), "sql": (q.get("sql") or "")[:220], "estado": q.get("estado"),
                          "filas": q.get("filas"), "truncado": q.get("truncado"), "columnas": q.get("columnas") or []}
                         for q in consultas[-MAX_CONSULTAS:]],
           "consultas_omitidas": max(0, len(consultas) - MAX_CONSULTAS),
           "salida": run.get("output"), "cobertura": v.get("cobertura"), "problemas": v.get("problemas") or [],
           "advertencias": v.get("advertencias") or [], "necesidades": v.get("necesidades") or []}
    if run.get("status") not in ("ok", None) and not v.get("falla"):
        out["error"] = (run.get("error") or "")[:600]
    return out


def texto(d: dict) -> str:
    lines = [f"Estado técnico: {d['estado']}."
             + (f" Medición: {d['medicion']}" + (f" ({d['motivo_medicion']})" if d.get("motivo_medicion") else "") + "."
                if d.get("medicion") else "")]
    if d.get("falla"):
        f = d["falla"]
        lines.append(f"Error sin capturar: {f.get('tipo')}: {f.get('mensaje')}{_lugar_txt(f.get('lugar'))}")
    if d.get("error"):
        lines.append(f"Error: {d['error']}")
    for e in d.get("excepciones_capturadas") or []:
        lines.append(f"Excepción CAPTURADA por el propio instrumento: {e.get('tipo')}: {e.get('mensaje')}"
                     f"{_lugar_txt(e)}" + (f" (×{e['veces']})" if e.get("veces", 1) > 1 else ""))
    if d.get("consultas"):
        lines.append("Datos recibidos:")
        for q in d["consultas"]:
            cols = ", ".join(f"{c['nombre']} {c.get('tipo_mariadb') or '?'}→{c.get('llega_como')}"
                             + (f" {c['ejemplo']}" if c.get("ejemplo") is not None else "") for c in q["columnas"])
            lines.append(f"  {q.get('consulta') or '-'} [{q.get('estado')}] {q['filas']} filas"
                         + (" (TRUNCADO)" if q.get("truncado") else "") + f": {q['sql']}"
                         + (f"\n      columnas: {cols}" if cols else ""))
        if d.get("consultas_omitidas"):
            lines.append(f"  (+{d['consultas_omitidas']} consultas anteriores)")
    elif d.get("estado") == "ok":
        lines.append("Datos recibidos: ninguna consulta.")
    if d.get("salida") is not None:
        lines.append(f"Salida: {_j(d['salida'], 900)}")
    if d.get("cobertura"):
        lines.append(f"Cobertura: {d['cobertura']}.")
    for p in d.get("problemas") or []:
        lines.append(f"Contrato: {p}")
    for a in d.get("advertencias") or []:
        lines.append(f"Advertencia: {a}")
    if d.get("necesidades"):
        lines.append(f"Necesidades de recursos: {_j(d['necesidades'], 400)}")
    return "\n".join(lines)


def cambio(anterior: str, nuevo: str, max_lineas: int = 30) -> dict:
    """Qué cambió entre dos versiones del código (diff unificado compacto)."""
    diff = list(difflib.unified_diff(anterior.splitlines(), nuevo.splitlines(), "anterior", "nuevo", lineterm="", n=0))
    cuerpo = diff[2:]
    return {"identico": anterior.strip() == nuevo.strip(),
            "lineas_cambiadas": sum(1 for x in cuerpo if x[:1] in "+-" and not x.startswith(("+++", "---"))),
            "diff": "\n".join(cuerpo[:max_lineas]) + ("\n…" if len(cuerpo) > max_lineas else "")}


def historial_texto(intentos: list[dict]) -> str:
    """Intentos anteriores: versión, falla, qué cambió y si el error persistió."""
    if not intentos:
        return ""
    lines = ["Intentos anteriores:"]
    for i in intentos:
        res = {"mismo_error": "MISMO ERROR que la versión anterior: el cambio no lo corrigió",
               "sin_cambios": "código IDÉNTICO a la versión anterior",
               "error_distinto": "error distinto", "corregido": "sin falla", "primera": ""}.get(i.get("resultado"), "")
        lines.append(f"- v{i['version']}: " + (i.get("firma") or "sin falla técnica") + (f" → {res}" if res else ""))
    ultimo = intentos[-1].get("cambio")
    if ultimo and ultimo.get("diff"):
        lines.append(f"Último cambio (v{intentos[-2]['version'] if len(intentos) > 1 else '?'} → "
                     f"v{intentos[-1]['version']}):\n{ultimo['diff']}")
    return "\n".join(lines)
