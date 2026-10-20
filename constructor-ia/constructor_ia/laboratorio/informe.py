"""Informe del laboratorio: resultados.json (todo lo medido) y resumen.md; comparación de dos corridas."""
from __future__ import annotations

import json
from pathlib import Path

METRICAS = [
    ("puntaje_medio", "Puntaje medio (0–1, contra la verdad)"),
    ("resueltas", "Tareas resueltas"),
    ("codigo_con_formato", "Código con el formato pedido"),
    ("evaluacion_con_formato", "Autoevaluación con el formato pedido"),
    ("rechazadas_estatico", "Rechazos de la revisión estática"),
    ("ejecuciones_ok", "Ejecuciones correctas"),
    ("cobertura_demostrada", "Cobertura demostrada (ejecuciones ok)"),
    ("parcial_sin_declarar", "Parciales sin declarar"),
    ("autoevaluacion_calibrada", "Autoevaluación calibrada"),
    ("sobreconfianza", "Sobreconfianza (dijo «sí» y falló)"),
    ("correcciones", "Correcciones escritas"),
    ("correcciones_sin_cambios", "Correcciones que repitieron el mismo código"),
    ("correccion_exitosa", "Tareas corregidas que terminaron resueltas"),
    ("detecto_insuficiencia", "Detectó el instrumento insuficiente (C)"),
    ("reutilizo", "Reutilizó sin reescribir (R)"),
    ("llamadas", "Llamadas al modelo"),
    ("llamadas_fallidas", "Llamadas fallidas"),
    ("tokens_generados", "Tokens generados"),
    ("segundos_modelo", "Segundos de modelo"),
    ("segundos_totales", "Segundos totales"),
]

AVISO = ("Puntajes medidos contra la verdad de una base sintética conocida. Con pocas repeticiones, una diferencia de "
         "una tarea puede deberse al azar del muestreo del modelo; compárense varias repeticiones antes de concluir.")


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "sí" if v else "no"
    return str(v)


def _por_tarea(res: dict) -> dict:
    out: dict = {}
    for s in res["sesiones"]:
        for r in s["registros"]:
            out.setdefault(r["tarea"], {}).setdefault(s["modelo"], []).append(r["puntaje"])
    return {t: {m: round(sum(v) / len(v), 2) for m, v in d.items()} for t, d in out.items()}


def markdown(res: dict) -> str:
    modelos = list(res["resumen_por_modelo"])
    lines = [f"# Laboratorio de instrumentos — {res['estado']}", "",
             f"Versión {res['version']} · semilla {res['semilla']} · escala {res['escala']} "
             f"({res.get('dias')} días × {res.get('por_dia')} lecturas/día) · repeticiones {res['repeticiones']} · "
             f"máximo de correcciones {res['max_correcciones']}", "",
             f"Presupuesto por ejecución: `{json.dumps(res['presupuesto'])}`", ""]
    if res.get("motivo"):
        lines += [f"**Abortado:** {res['motivo']}", ""]
    lines += ["| Métrica | " + " | ".join(modelos) + " |", "|---|" + "---|" * len(modelos)]
    for key, label in METRICAS:
        lines.append(f"| {label} | " + " | ".join(_fmt(res["resumen_por_modelo"][m].get(key)) for m in modelos) + " |")
    lines += ["", "## Puntaje por tarea (promedio de repeticiones)", "",
              "| Tarea | " + " | ".join(modelos) + " |", "|---|" + "---|" * len(modelos)]
    for t, d in _por_tarea(res).items():
        lines.append(f"| {t} | " + " | ".join(_fmt(d.get(m)) for m in modelos) + " |")
    lines += ["", "## Detalle", ""]
    for s in res["sesiones"]:
        lines.append(f"### {s['modelo']} — repetición {s['repeticion']}")
        for r in s["registros"]:
            vers = ", ".join(f"{v['modulo']}:{v['estado']}" for v in r["versiones"]) or "sin ejecuciones"
            lines.append(f"- **{r['tarea']}** {r['titulo']}: puntaje {r['puntaje']} — {r['detalle_puntaje']}. "
                         f"Autoevaluación {_fmt(r.get('autoevaluacion_final'))} ({_fmt(r.get('calibracion'))}); "
                         f"correcciones {r['correcciones']}; {vers}.")
        lines.append("")
    lines += ["## Opciones de los modelos (idénticas salvo el nombre)", "", "```json",
              json.dumps(res["modelos"], ensure_ascii=False, indent=1), "```", "", f"_{AVISO}_", ""]
    return "\n".join(lines)


def escribir(res: dict, salida: Path) -> tuple[Path, Path]:
    salida = Path(salida)
    j, m = salida / "resultados.json", salida / "resumen.md"
    j.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    m.write_text(markdown(res))
    return j, m


def comparar(a: dict, b: dict) -> str:
    """Compara dos corridas (p. ej. una por modelo, hechas en momentos distintos). Avisa si las condiciones difieren."""
    cond = ("semilla", "escala", "dias", "por_dia", "tareas", "max_correcciones", "presupuesto")
    distintas = [k for k in cond if a.get(k) != b.get(k)]
    opts = [{k: v for k, v in m.items() if k != "modelo"} for m in a["modelos"] + b["modelos"]]
    if any(o != opts[0] for o in opts):
        distintas.append("opciones de los modelos (contexto, temperatura, tokens o timeout)")
    cols = [(f"A:{m}", a["resumen_por_modelo"][m]) for m in a["resumen_por_modelo"]] + \
           [(f"B:{m}", b["resumen_por_modelo"][m]) for m in b["resumen_por_modelo"]]
    lines = ["# Comparación de corridas del laboratorio", ""]
    lines += ([f"**Atención: las condiciones NO son iguales ({', '.join(distintas)}); la comparación no es justa.**"]
              if distintas else ["Condiciones idénticas (semilla, escala, tareas, presupuestos y opciones)."]) + [""]
    lines += ["| Métrica | " + " | ".join(c for c, _ in cols) + " |", "|---|" + "---|" * len(cols)]
    for key, label in METRICAS:
        lines.append(f"| {label} | " + " | ".join(_fmt(r.get(key)) for _, r in cols) + " |")
    ta, tb = _por_tarea(a), _por_tarea(b)
    lines += ["", "| Tarea | " + " | ".join(c for c, _ in cols) + " |", "|---|" + "---|" * len(cols)]
    for t in sorted(set(ta) | set(tb)):
        vals = [ta.get(t, {}).get(c[2:]) if c.startswith("A:") else tb.get(t, {}).get(c[2:]) for c, _ in cols]
        lines.append(f"| {t} | " + " | ".join(_fmt(v) for v in vals) + " |")
    return "\n".join(lines + ["", f"_{AVISO}_", ""])
