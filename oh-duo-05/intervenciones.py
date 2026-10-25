"""OH-DUO-05: analiza las intervenciones de una corrida y compara una corrida de control con una intervenida.

  python3 intervenciones.py analizar --corrida corridas/NOMBRE
  python3 intervenciones.py comparar --control corridas/CONTROL --intervenida corridas/INTERVENIDA [--salida DIR]

Por ejecución (problema × modelo) junta, sin modificar nada:
  - los mensajes del coordinador que el SDK registró (MessageEvent de usuario con sender «oh-duo-04:<id>»);
  - lo que anotó el puente (entregas.jsonl) y el despachador (despachos.jsonl, si se indica);
  - las tres confirmaciones: 1) el mensaje está en la conversación, 2) hubo una acción del agente después,
    3) cambió la conducta: editó el código productivo y/o las pruebas aprobaron después del mensaje;
  - acciones e iteraciones (el límite es el de siempre: 30), terminación y veredicto de OH-DUO-02;
  - si la corrida ya se verificó (ejecutar.py verificar), la categoría de OH-DUO-01 con las pruebas ocultas.
Solo biblioteca estándar.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
from ejecutar import cargar_duo, hermano  # noqa: E402

PREFIJO = "oh-duo-04:"
LIMITE = 30


def cargar_detector(oh_duo_02: Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("oh_duo_02_estancamiento", oh_duo_02 / "estancamiento.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def leer_lineas(p: Path) -> list[dict]:
    if not p.is_file():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def texto_mensaje(e: dict) -> str:
    return " ".join(c.get("text", "") for c in (e.get("llm_message") or {}).get("content") or [] if isinstance(c, dict))


def firma(a: dict) -> str:
    """Huella de una acción del agente: herramienta y argumentos tal como los generó el modelo."""
    t = a.get("tool_call") or {}
    return hashlib.sha256(json.dumps([a.get("tool_name"), t.get("arguments")], ensure_ascii=False).encode()).hexdigest()[:16]


def analizar_ejecucion(e: Path, E, u: dict, despachos: list[dict]) -> dict:
    loc = E.localizar(e)
    eventos = E.cargar_eventos_sdk(loc["events_dir"])[0] if loc["events_dir"] else []
    mod = (loc["mision"] or {}).get("modulo")
    codigo = f"{mod}.py" if mod else None
    det = E.analizar_ejecucion(e, u)
    acciones = [i for i, x in enumerate(eventos) if x.get("kind") == "ActionEvent"]
    respuestas = {x.get("llm_response_id") or i for i, x in enumerate(eventos)
                  if x.get("source") == "agent" and x.get("kind") in ("ActionEvent", "MessageEvent")}
    obs = {x.get("action_id"): x for x in eventos if x.get("kind") == "ObservationEvent"}

    mensajes = []
    for k, x in enumerate(eventos):
        if x.get("kind") == "MessageEvent" and (x.get("sender") or "").startswith(PREFIJO):
            despues = [i for i in acciones if i > k]
            edito = probo_ok = False
            for i in despues:
                a = eventos[i]
                act = a.get("action") or {}
                if a.get("tool_name") == "file_editor" and act.get("command") not in (None, "view") \
                        and codigo and os.path.basename(str(act.get("path", ""))) == codigo:
                    edito = True
                if a.get("tool_name") == "terminal" and "unittest" in (act.get("command") or ""):
                    o = obs.get(a.get("id"))
                    if o and E.resultado_pruebas(o.get("observation")).get("estado") == "aprueban":
                        probo_ok = True
            antes = len({eventos[i].get("llm_response_id") or i for i in acciones if i < k})
            mensajes.append({
                "id": x["sender"][len(PREFIJO):], "evento": k, "iteracion_al_recibir": antes, "texto": texto_mensaje(x),
                "1_en_la_conversacion": True,
                "2_accion_posterior": {"evento": despues[0], "herramienta": eventos[despues[0]].get("tool_name")} if despues else None,
                "3_cambio_de_conducta": {"edito_el_codigo_despues": edito, "pruebas_aprueban_despues": probo_ok},
                "acciones_despues": len(despues), "acciones_antes": sum(1 for i in acciones if i < k),
            })
    resultados = sorted((e / "instancia" / "resultados").glob("*")) if (e / "instancia").is_dir() else []
    entregas = leer_lineas(resultados[-1] / "estado" / "entregas.jsonl") if resultados else []
    final = json.loads((resultados[-1] / "resultado_final.json").read_text()) if resultados and \
        (resultados[-1] / "resultado_final.json").is_file() else {}
    reg = loc["ejecucion"] or {}
    return {
        "ejecucion": e.name, "problema": reg.get("problema"), "programador": reg.get("programador"),
        "estado_ejecucion": reg.get("estado_ejecucion"), "visibles_pasan": reg.get("visibles_pasan"),
        "originales_intactos": reg.get("originales_intactos"), "cambio_de_modelo": reg.get("cambio_de_modelo"),
        "semilla": reg.get("semilla"), "modelo": reg.get("modelo"),
        "acciones": len(acciones), "iteraciones": len(respuestas), "firmas": [firma(eventos[i]) for i in acciones], "limite": LIMITE,
        "dentro_del_limite": len(respuestas) <= LIMITE,
        "terminacion": det.get("terminacion"), "estancamiento": (det.get("veredicto") or {}).get("estancamiento"),
        "alertas_oh_duo_02": sorted({a.get("regla") for a in det.get("alertas") or [] if isinstance(a, dict)} - {None}),
        "mensajes_del_coordinador": mensajes,
        "puente": {"entregas": entregas, "resumen": final.get("intervenciones")},
        "despachos": [d for d in despachos if d.get("ejecucion") == e.name],
    }


def categorias(corrida: Path, oh_duo_01: Path) -> dict:
    verif = corrida / "verificacion" / "verificacion.json"
    if not verif.is_file():
        return {}
    duo = cargar_duo(oh_duo_01)
    v = json.loads(verif.read_text())["resultados"]
    out = {}
    for e in sorted(p for p in (corrida / "ejecuciones").iterdir() if (p / "ejecucion.json").is_file()):
        reg = json.loads((e / "ejecucion.json").read_text())
        if e.name in v:
            out[e.name] = duo.categoria(reg["estado_ejecucion"], v[e.name]["pasa"])
    return out


def analizar(corrida: Path, E, u: dict, oh_duo_01: Path, despachos: Path | None = None) -> dict:
    corrida = corrida.resolve()
    desp = leer_lineas(despachos) if despachos else []
    cats = categorias(corrida, oh_duo_01)
    filas = []
    for e in sorted(p for p in (corrida / "ejecuciones").iterdir() if p.is_dir()):
        f = analizar_ejecucion(e, E, u, desp)
        f["categoria_con_ocultas"] = cats.get(e.name)
        verif = corrida / "verificacion" / "verificacion.json"
        f["fugas_ocultas"] = sum(1 for x in json.loads(verif.read_text())["auditoria_fugas"] if x["ejecucion"] == e.name) \
            if verif.is_file() else None
        filas.append(f)
    return {"corrida": corrida.name, "verificada": bool(cats), "ejecuciones": filas}


def comparar(control: dict, intervenida: dict) -> dict:
    c = {f["ejecucion"]: f for f in control["ejecuciones"]}
    filas = []
    for f in intervenida["ejecuciones"]:
        g = c.get(f["ejecucion"])
        m = f["mensajes_del_coordinador"]
        fila = {"ejecucion": f["ejecucion"], "intervenciones": len(m),
                "confirmadas_por_accion": sum(1 for x in m if x["2_accion_posterior"]),
                "cambio_de_conducta": any(any(x["3_cambio_de_conducta"].values()) for x in m)}
        for clave in ("acciones", "iteraciones", "dentro_del_limite", "estancamiento", "visibles_pasan",
                      "estado_ejecucion", "categoria_con_ocultas", "originales_intactos", "cambio_de_modelo",
                      "fugas_ocultas", "semilla", "modelo"):
            fila[clave] = {"control": g.get(clave) if g else None, "intervenida": f.get(clave)}
        # validez: hasta el primer mensaje, las dos corridas tienen que haber hecho exactamente lo mismo
        n = m[0]["acciones_antes"] if m else len(f.get("firmas", []))
        previas_c, previas_i = (g or {}).get("firmas", [])[:n], f.get("firmas", [])[:n]
        div = next((j for j, (a, b) in enumerate(zip(previas_c, previas_i)) if a != b),
                   None if len(previas_c) == len(previas_i) else min(len(previas_c), len(previas_i)))
        fila["identicas_antes_del_mensaje"] = {"acciones_comparadas": n, "identicas": div is None,
                                              "primera_divergencia": div}
        fila["terminacion"] = {"control": ((g or {}).get("terminacion") or {}).get("tipo"),
                               "intervenida": (f.get("terminacion") or {}).get("tipo")}
        fila["mensajes_en_control"] = len((g or {}).get("mensajes_del_coordinador", []))
        fila["validez"] = validez(fila, f)
        filas.append(fila)
    sin_par = sorted(set(c) - {f["ejecucion"] for f in intervenida["ejecuciones"]})
    return {"control": control["corrida"], "intervenida": intervenida["corrida"], "filas": filas,
            "solo_en_control": sin_par,
            "nota": "con una semilla por lado es un caso, no una medición: repetir con varias semillas antes de concluir"}


def validez(fila: dict, f: dict) -> dict:
    """Criterios del protocolo (PROTOCOLO.md): si alguno falla, el par no sirve para atribuir nada al mensaje."""
    motivos, avisos = [], []
    for clave in ("semilla", "modelo"):
        if fila[clave]["control"] != fila[clave]["intervenida"]:
            motivos.append(f"{clave} distinta")
    if fila["mensajes_en_control"]:
        motivos.append("el control recibió mensajes del coordinador")
    for lado in ("control", "intervenida"):
        if fila["dentro_del_limite"][lado] is not True:
            motivos.append(f"{lado}: más de 30 iteraciones")
        if fila["originales_intactos"][lado] != "ok":
            motivos.append(f"{lado}: originales alterados")
        if fila["fugas_ocultas"][lado] not in (0,):
            motivos.append(f"{lado}: sin verificar o con fugas de ocultas")
        if fila["cambio_de_modelo"][lado] is not False:      # cambia los tiempos, no las respuestas
            avisos.append(f"{lado}: el modelo no estaba cargado en Ollama al empezar (o no se pudo saber)")
    if not fila["identicas_antes_del_mensaje"]["identicas"]:
        motivos.append(f"las corridas divergen antes del mensaje (acción {fila['identicas_antes_del_mensaje']['primera_divergencia']})")
    if fila["intervenciones"] and fila["confirmadas_por_accion"] < fila["intervenciones"]:
        motivos.append("hay mensajes entregados sin acción posterior")
    descartes = [x for x in f["puente"]["entregas"] if x.get("resultado") in ("descartado", "rechazado_formato")]
    if descartes:
        motivos.append(f"el puente descartó o rechazó {len(descartes)} mensaje(s)")
    return {"valida": not motivos, "motivos": motivos, "avisos": avisos,
            "informativa": not motivos and fila["intervenciones"] > 0,
            "nota": None if fila["intervenciones"] else "el coordinador no intervino: el par no mide el efecto del mensaje"}


def markdown(r: dict) -> str:
    ls = [f"# Control `{r['control']}` vs intervenida `{r['intervenida']}`", "",
          "| ejecución | idénticas antes del mensaje | intervenciones (confirmadas) | cambio de conducta | acciones | iteraciones | terminación | estancamiento | visibles | categoría (ocultas) |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    par = lambda d: f"{d['control']} → {d['intervenida']}"  # noqa: E731
    for f in r["filas"]:
        v = f["identicas_antes_del_mensaje"]
        ls.append(f"| {f['ejecucion']} | {'sí' if v['identicas'] else 'NO (acción ' + str(v['primera_divergencia']) + ')'} "
                  f"({v['acciones_comparadas']} acciones) | {f['intervenciones']} ({f['confirmadas_por_accion']}) | "
                  f"{'sí' if f['cambio_de_conducta'] else 'no'} | {par(f['acciones'])} | {par(f['iteraciones'])} | "
                  f"{par(f['terminacion'])} | {par(f['estancamiento'])} | {par(f['visibles_pasan'])} | "
                  f"{par(f['categoria_con_ocultas'])} |")
    ls.append("")
    for f in r["filas"]:
        v = f["validez"]
        ls.append(f"- **{f['ejecucion']}**: par {'VÁLIDO' if v['valida'] else 'NO VÁLIDO'}"
                  + ("" if v["valida"] else ": " + "; ".join(v["motivos"])) + (f" ({v['nota']})" if v["nota"] else "")
                  + ("" if not v["avisos"] else " · avisos: " + "; ".join(v["avisos"])))
    ls += ["", f"_{r['nota']}._", ""]
    return "\n".join(ls)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--umbrales", type=Path)
    sub = p.add_subparsers(dest="modo", required=True)
    a_ = sub.add_parser("analizar")
    a_.add_argument("--corrida", type=Path, required=True)
    a_.add_argument("--despachos", type=Path)
    c_ = sub.add_parser("comparar")
    c_.add_argument("--control", type=Path, required=True)
    c_.add_argument("--intervenida", type=Path, required=True)
    c_.add_argument("--despachos", type=Path, help="despachos.jsonl de la intervenida (por omisión, el de cadena.py)")
    c_.add_argument("--salida", type=Path)
    a = p.parse_args(argv)
    E = cargar_detector(hermano("oh-duo-02", "estancamiento.py"))
    u = E.cargar_umbrales(a.umbrales)
    oh1 = hermano("oh-duo-01", "duo.py")
    if a.modo == "analizar":
        desp = a.despachos or AQUI / "informes" / a.corrida.resolve().name / "despachos.jsonl"
        print(json.dumps(analizar(a.corrida, E, u, oh1, desp), ensure_ascii=False, indent=1))
        return 0
    desp = a.despachos or AQUI / "informes" / a.intervenida.resolve().name / "despachos.jsonl"
    r = comparar(analizar(a.control, E, u, oh1), analizar(a.intervenida, E, u, oh1, desp))
    salida = (a.salida or AQUI / "informes" / f"comparacion-{a.control.resolve().name}-vs-{a.intervenida.resolve().name}").resolve()
    salida.mkdir(parents=True, exist_ok=True)
    (salida / "comparacion.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))
    (salida / "comparacion.md").write_text(markdown(r))
    print(markdown(r))
    print(f"Informe: {salida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
