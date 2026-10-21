"""Informe del laboratorio: resultados.json (todo lo medido) y resumen.md; comparación de dos corridas."""
from __future__ import annotations

import json
from pathlib import Path

METRICAS = [
    ("puntaje_medio", "Puntaje medio (0–1, contra la verdad)"),
    ("resueltas", "Tareas resueltas"),
    ("codigo_con_formato", "Código con el formato pedido"),
    ("evaluacion_con_formato", "Autoevaluación con el formato pedido (primera respuesta)"),
    ("evaluaciones_formato_reparado", "Autoevaluaciones con formato reparado"),
    ("evaluaciones_rescatadas", "Autoevaluaciones rescatadas (JSON cortado)"),
    ("evaluaciones_ilegibles", "Autoevaluaciones ilegibles"),
    ("rechazadas_estatico", "Rechazos de la revisión estática"),
    ("ejecuciones_ok", "Ejecuciones técnicamente correctas"),
    ("mediciones_validas", "Mediciones válidas (con datos detrás)"),
    ("errores_de_calculo", "Errores de cálculo (excepción capturada)"),
    ("sin_evidencia", "Mediciones sin evidencia"),
    ("cobertura_demostrada", "Cobertura demostrada (mediciones válidas)"),
    ("parcial_sin_declarar", "Parciales sin declarar"),
    ("autoevaluacion_calibrada", "Autoevaluación calibrada"),
    ("sobreconfianza", "Sobreconfianza (dijo «sí» y falló)"),
    ("correcciones", "Versiones nuevas escritas"),
    ("reparaciones", "Reparaciones del instrumento"),
    ("reparaciones_efectivas", "Reparaciones que eliminaron la falla"),
    ("reparaciones_mismo_error", "Reparaciones con el mismo error o sin cambios"),
    ("errores_persistentes", "Tareas cortadas por error persistente"),
    ("revisiones_metodo", "Revisiones del método"),
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
             f"máximo de versiones nuevas {res['max_correcciones']} · reparaciones de formato "
             f"{res.get('reparaciones_formato', 0)} · corte por mismo error {res.get('max_mismo_error', '—')}", "",
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
            vers = ", ".join(f"{v['modulo']}:{v['estado']}" + (f"/{v['medicion']}" if v.get("medicion") else "")
                             for v in r["versiones"]) or "sin ejecuciones"
            lines.append(f"- **{r['tarea']}** {r['titulo']}: puntaje {r['puntaje']} — {r['detalle_puntaje']}. "
                         f"Autoevaluación {_fmt(r.get('autoevaluacion_final'))} ({_fmt(r.get('calibracion'))}); "
                         f"correcciones {r['correcciones']}; {vers}.")
            for i in r.get("intentos") or []:
                if i.get("firma") or i["resultado"] not in ("primera", "sin_falla"):
                    lines.append(f"  - v{i['version']}: {i.get('firma') or 'sin falla'} ({i['resultado']})"
                                 + (f" — causa declarada: {i['causa_declarada']}" if i.get("causa_declarada") else ""))
            for e in r.get("evaluaciones") or []:
                for d in e.get("diagnostico_formato") or []:
                    lines.append(f"  - autoevaluación {e.get('formato')}: {d['causa']} — {d['detalle'][:160]}")
            if r.get("error_persistente"):
                lines.append(f"  - error persistente ({r['error_persistente']['veces']} veces): "
                             f"{r['error_persistente']['firma']}")
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
    cond = ("semilla", "escala", "dias", "por_dia", "tareas", "max_correcciones", "reparaciones_formato",
            "max_mismo_error", "presupuesto", "version")
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


def revisar(carpeta: Path) -> str:
    """Revisión de SOLO LECTURA de una corrida (también de 0.6.0): cada llamada al modelo con su fin y sus tokens, las
    autoevaluaciones con su texto crudo, y el código y las consultas de cada instrumento (bases SQLite en modo ro)."""
    import sqlite3

    carpeta = Path(carpeta)
    res = json.loads((carpeta / "resultados.json").read_text())
    lines = [f"Corrida {carpeta.name} — versión {res.get('version')} — {res.get('estado')}", ""]
    for s in res.get("sesiones", []):
        lines.append(f"== {s['modelo']} — repetición {s['repeticion']}")
        for r in s["registros"]:
            lines.append(f"-- {r['tarea']}: puntaje {r['puntaje']} — {r['detalle_puntaje']}")
            for c in r["llamadas"]:
                lines.append(f"   llamada {c['tipo']}: {c['resultado']}, {c.get('tokens_generados')} tokens generados, "
                             f"formato {'ok' if c.get('formato_ok') else 'INVÁLIDO'}")
            for e in r.get("evaluaciones", []):
                crudos = [d.get("crudo") for d in e.get("diagnostico_formato") or []] or [e.get("crudo")]
                for d in e.get("diagnostico_formato") or []:
                    lines.append(f"   evaluación {e.get('formato')}: {d['causa']} — {d['detalle']}")
                for crudo in crudos:
                    if crudo:
                        lines.append(f"   texto crudo ({len(crudo)} caracteres guardados):\n{crudo}")
                if e.get("responde"):
                    lines.append(f"   evaluación: {e['responde']} — {e.get('motivo')}")
        from .corrida import _slug

        db_path = carpeta / f"{_slug(s['modelo'])}_r{s['repeticion']}" / "lab.sqlite"
        if not db_path.exists():
            continue
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for iid, name, v, code in db.execute("SELECT id, name, version, code FROM inst_instruments ORDER BY id"):
                lines.append(f"\n--- instrumento {name}_v{v}\n{code}")
                for st, err, out in db.execute("SELECT status, error, output_json FROM inst_runs WHERE instrument_id=? "
                                               "ORDER BY id", (iid,)):
                    lines.append(f"    ejecución: {st}" + (f" — {err[:300]}" if err else "")
                                 + (f"\n    salida: {out[:600]}" if out else ""))
            lines.append("\n--- consultas de los instrumentos")
            for tool, sql, st, n in db.execute("SELECT tool, sql_text, status, row_count FROM explore_queries WHERE "
                                               "origin='instrumento' ORDER BY id"):
                lines.append(f"    {tool} [{st}, {n} filas] {sql[:400]}")
        finally:
            db.close()
        lines.append("")
    return "\n".join(lines)
