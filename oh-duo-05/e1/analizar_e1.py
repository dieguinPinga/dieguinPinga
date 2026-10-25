"""E1: en qué etapa se rompe la cadena comprensión → identificación → acción → aplicación → verificación. SOLO LECTURA.

  <venv>/bin/python -I analizar_e1.py --experimento informes/e1-p1-s11-<et> [--b1 informes/b1-p1-s11-<et>]

Por condición (E1-AB, E1-C), con criterios objetivos y en orden; la primera que falla es la etapa de ruptura:
  0 validez       las llamadas ANTERIORES a la pregunta A son las de A1 (pedido idéntico o solo ruido, misma
                  respuesta) y en la llamada de la pregunta el pedido es el de A1 + la pregunta y nada más
  1 comprensión   la respuesta a la pregunta A contiene la línea corregida (max(resultado[-1][1], fin), en
                  cualquier orden); se registra además si nombra la prueba, lo esperado, lo obtenido y la línea mala
  2 formulación   la primera edición después de la pregunta B apunta a intervalos.py y el resultado de aplicarla
                  contiene la corrección (si apunta a test_*.py o no hay edición, falla aquí)
  3 aplicación    la herramienta la APLICÓ (estado reconstruido distinto, observación de éxito): intento ≠ aplicación
  4 verificación  el archivo final pasa las visibles y las ocultas (evaluadas por separado, en bwrap, sobre copias)
Si la etapa 3 falla, se registra la recuperación: si la acción siguiente interpreta el error (vuelve a editar
intervalos.py con otro texto o lo mira) y si alguna edición posterior, dentro del tope, aplica la corrección.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path[:0] = [str(AQUI), str(AQUI.parent / "diagnostico"), str(AQUI.parent / "reproducibilidad"), str(AQUI.parent)]
import analizar_par as A  # noqa: E402
import auditar_b1 as AUD  # noqa: E402
import comparar_b1 as B  # noqa: E402
import plan as PLAN  # noqa: E402
from ejecutar import hermano  # noqa: E402

CORRECCION = AUD.CORRECCION


def acciones(corrida: Path) -> list[dict]:
    loc = A.localizar(corrida)
    ev = A.cargar_eventos(loc["events"]) if loc["events"] else []
    obs = {e.get("action_id"): e for e in ev if e.get("kind") == "ObservationEvent"}
    err = {e.get("tool_call_id"): e for e in ev if e.get("kind") == "AgentErrorEvent"}
    filas, n = [], 0
    for i, e in enumerate(ev):
        if e.get("kind") != "ActionEvent":
            continue
        o = obs.get(e.get("id")) or err.get(e.get("tool_call_id"))
        t = AUD.texto_obs(o)
        filas.append({"n": n, "evento": i, "herramienta": e.get("tool_name"), "accion": e.get("action") or {},
                      "observacion": t, "error_del_agente": (o or {}).get("kind") == "AgentErrorEvent"})
        n += 1
    return filas


def mensajes_agente(corrida: Path, desde: int, hasta: int | None) -> list[str]:
    loc = A.localizar(corrida)
    ev = A.cargar_eventos(loc["events"]) if loc["events"] else []
    return [A.texto(e.get("llm_message")) for i, e in enumerate(ev)
            if e.get("kind") == "MessageEvent" and e.get("source") == "agent" and i > desde and (hasta is None or i < hasta)]


def analizar_condicion(corrida: Path, ref: dict, ev, umbrales: dict) -> dict:
    an = A.analizar_corrida(corrida)
    aud = AUD.auditar_corrida(corrida, ev, umbrales, None)
    r_dir = Path(an["resultado"]) if an["resultado"] else None
    crudas = B.crudas_con_mensajes(r_dir) if r_dir else []
    final = A.leer(r_dir / "resultado_final.json") if r_dir else {}
    acc = acciones(corrida)
    sondas = {m["sender"][3:]: m for m in an["mensajes_de_usuario_posteriores"] if (m.get("sender") or "").startswith("e1:")}
    r = {"corrida": corrida.name, "guion": (final or {}).get("guion"), "estado_final_sdk": an["estado_final_sdk"],
         "llamadas": len(crudas), "acciones": len(acc), "etapas": {}, "problemas": [], "avisos": []}
    if "A" not in sondas:
        r["problemas"].append("la pregunta A nunca se insertó (la trayectoria no llegó al punto previsto)")
        r["etapa_de_ruptura"] = "no_valida"
        return r
    # 0. validez: prefijo = A1 hasta la pregunta A
    texto_a = PLAN.SONDA_A[:120]
    m_a = next((k for k, f in enumerate(crudas) if any(texto_a in json.dumps(x, ensure_ascii=False) for x in (f["_mensajes"] or []))), None)
    rc = ref["crudas"]
    prefijo = [{"pedido": B.clasificar_pedido(rc[k]["_mensajes"], crudas[k]["_mensajes"]),
                "respuesta_igual": rc[k]["respuesta_sha"] == crudas[k]["respuesta_sha"]}
               for k in range(min(m_a if m_a is not None else 0, len(rc), len(crudas)))]
    distintos = [k for k, p in enumerate(prefijo) if p["pedido"] == "distinto" or not p["respuesta_igual"]]
    con_sonda = B.clasificar_pedido(rc[m_a]["_mensajes"], B.sin_mensaje(crudas[m_a]["_mensajes"], texto_a)) \
        if m_a is not None and m_a < len(rc) else None
    r["etapas"]["0_validez"] = {"llamada_de_la_pregunta": m_a, "acciones_antes": sondas["A"]["acciones_antes"],
                                "prefijo_llamadas": len(prefijo), "prefijo_distinto_en": distintos,
                                "prefijo_solo_ruido": sum(1 for p in prefijo if p["pedido"] == "solo_ruido_del_entorno"),
                                "pedido_de_la_pregunta_igual_a_a1_mas_pregunta": con_sonda,
                                "ok": m_a is not None and not distintos and con_sonda in ("identico", "solo_ruido_del_entorno")}
    if not r["etapas"]["0_validez"]["ok"]:
        r["problemas"].append("la trayectoria no coincide con A1 antes de la pregunta: no se puede comparar")
    # 1. comprensión: lo que respondió a la pregunta A (texto del agente, o la respuesta cruda si actuó)
    ev_a, ev_b = sondas["A"]["evento"], (sondas.get("B") or {}).get("evento")
    textos = mensajes_agente(corrida, ev_a, ev_b)
    cruda_a = crudas[m_a]["respuesta"] if m_a is not None and m_a < len(crudas) else ""
    t = "\n".join(textos) or cruda_a
    acc_a = [x for x in acc if x["evento"] > ev_a and (ev_b is None or x["evento"] < ev_b)]
    r["etapas"]["1_comprension"] = {
        "respondio_en_texto": bool(textos), "acciones_en_lugar_de_responder": [(x["n"], x["herramienta"]) for x in acc_a],
        "leyo_intervalos_durante_a": any(x["herramienta"] == "file_editor" and x["accion"].get("command") == "view"
                                         and str(x["accion"].get("path", "")).endswith("/intervalos.py") or
                                         x["herramienta"] == "terminal" and "intervalos.py" in (x["accion"].get("command") or "")
                                         and "test_" not in (x["accion"].get("command") or "") for x in acc_a),
        "edito_durante_a": [x["n"] for x in acc_a if x["herramienta"] == "file_editor" and x["accion"].get("command") not in (None, "view")],
        "respuesta": " ".join(t.split())[:1200],
        "nombra_la_prueba": "test_intervalo_contenido" in t, "dice_lo_esperado": "(1, 10)" in t, "dice_lo_obtenido": "(1, 3)" in t,
        "senala_la_linea": "(resultado[-1][0], fin)" in t or "resultado[-1] = (" in t,
        "culpa_a_la_prueba": bool(re.search(AUD.ETIQUETAS["culpa_a_la_prueba"], t, re.I)),
        "ok": bool(CORRECCION.search(t))}
    # 2-3. formulación y aplicación: primera edición después de la pregunta B
    if "B" not in sondas:
        r["problemas"].append("la pregunta B no se envió")
        r["etapa_de_ruptura"] = _etapa(r)
        return r
    tras_b = [x for x in acc if x["evento"] > ev_b]
    ediciones = [x for x in tras_b if x["herramienta"] == "file_editor" and x["accion"].get("command") not in (None, "view")]
    primera = ediciones[0] if ediciones else None
    aud_ed = {x["accion"]: x for x in aud["ediciones"]}
    def resumen(x):
        a = x["accion"]
        e = aud_ed.get(x["n"]) or {}
        return {"accion": x["n"], "ruta": a.get("path"), "comando": a.get("command"), "old_str": a.get("old_str"),
                "new_str": a.get("new_str") if a.get("command") != "create" else (a.get("file_text") or "")[:400],
                "observacion": " ".join(x["observacion"].split())[:300], "efecto_reconstruido": e.get("efecto"),
                "herramienta_dice": e.get("herramienta"), "cambio_el_archivo": e.get("cambio_el_archivo"),
                "estado_resultante": e.get("evaluacion")}
    p = resumen(primera) if primera else None
    a_intervalos = bool(p) and str(p["ruta"]).endswith("intervalos.py") and not str(p["ruta"]).split("/")[-1].startswith("test_")
    produce = bool(p) and ((p["estado_resultante"] or {}).get("contiene_la_correccion") or bool(CORRECCION.search(p["new_str"] or "")))
    r["etapas"]["2_formulacion"] = {"acciones_tras_b": [(x["n"], x["herramienta"], A.corto(x["accion"].get("command") or "", 60)) for x in tras_b],
                                    "primera_edicion": p, "apunta_a_intervalos": a_intervalos, "contiene_la_correccion": produce,
                                    "ok": a_intervalos and produce}
    aplicada = bool(p) and str(p["efecto_reconstruido"] or "").startswith("aplicada") and p["cambio_el_archivo"] \
        and p["herramienta_dice"] == "aplicada"
    r["etapas"]["3_aplicacion"] = {"intento": bool(p), "aplicada": aplicada,
                                   "coincide_con_la_herramienta": bool(p) and (p["herramienta_dice"] == "aplicada") == aplicada,
                                   "ok": aplicada}
    # recuperación tras un rechazo
    if p and not aplicada:
        sig = next((x for x in tras_b if x["n"] > p["accion"]), None)
        interpreta = bool(sig) and sig["herramienta"] == "file_editor" and str((sig["accion"] or {}).get("path", "")).endswith("/intervalos.py") \
            and ((sig["accion"].get("command") == "view") or sig["accion"].get("old_str") != (primera["accion"] or {}).get("old_str"))
        luego = [resumen(x) for x in ediciones[1:]]
        recupera = any(str(x["efecto_reconstruido"] or "").startswith("aplicada") and x["cambio_el_archivo"]
                       and (x["estado_resultante"] or {}).get("contiene_la_correccion") for x in luego)
        r["etapas"]["3_aplicacion"]["recuperacion"] = {"accion_siguiente": (sig["n"], sig["herramienta"], A.corto(json.dumps(sig["accion"]), 160)) if sig else None,
                                                       "interpreta_el_error": interpreta, "aplica_la_correccion_despues": recupera,
                                                       "ediciones_posteriores": luego}
    protegidas = [x for x in aud["protegidas"] if x["accion"] is not None and x["accion"] >= (tras_b[0]["n"] if tras_b else 10 ** 9)]
    r["intentos_sobre_pruebas_tras_b"] = protegidas
    # 4. verificación: el archivo final
    fin = aud["reconstruccion"]
    fe = fin.get("final") or {}
    r["etapas"]["4_verificacion"] = {"reconstruccion_coincide": fin["coincide"],
                                     "visibles": fe.get("visibles"), "ocultas": fe.get("ocultas"),
                                     "contiene_la_correccion": fe.get("contiene_la_correccion"),
                                     "ok": bool(fe) and fe["visibles"]["ejecutadas"] > 0 and fe["visibles"]["pasan"] == fe["visibles"]["ejecutadas"]
                                     and fe["ocultas"]["ejecutadas"] > 0 and fe["ocultas"]["pasan"] == fe["ocultas"]["ejecutadas"]}
    if not fin["coincide"]:
        r["avisos"].append("la reconstrucción del archivo no coincide con el archivo final congelado (hubo cambios por terminal)")
    r["etapa_de_ruptura"] = _etapa(r)
    return r


def _etapa(r: dict) -> str:
    e3, e4 = r["etapas"].get("3_aplicacion") or {}, r["etapas"].get("4_verificacion") or {}
    if (r["etapas"].get("2_formulacion") or {}).get("ok") and not e3.get("ok") and e4.get("ok") \
            and (e3.get("recuperacion") or {}).get("aplica_la_correccion_despues"):
        return "ninguna (se completa tras recuperarse de un rechazo)"
    for clave, nombre in (("0_validez", "no_valida"), ("1_comprension", "comprension"), ("2_formulacion", "formulacion"),
                          ("3_aplicacion", "aplicacion"), ("4_verificacion", "verificacion")):
        e = r["etapas"].get(clave)
        if e is None or not e.get("ok"):
            return nombre
    return "ninguna (la cadena se completa)"


def linea_base(corrida: Path, ev, umbrales: dict) -> dict:
    aud = AUD.auditar_corrida(corrida, ev, umbrales, None)
    fe = aud["reconstruccion"].get("final") or {}
    ed = [x for x in aud["ediciones"] if x["via"] == "file_editor"]
    return {"corrida": corrida.name, "acciones": len(aud["acciones"]), "ediciones_intentadas": len(ed),
            "ediciones_aplicadas": sum(1 for x in ed if x["cambio_el_archivo"]),
            "alguna_con_la_correccion": any((x.get("evaluacion") or {}).get("contiene_la_correccion") for x in ed),
            "intentos_sobre_pruebas": len(aud["protegidas"]), "final_visibles": fe.get("visibles"), "final_ocultas": fe.get("ocultas"),
            "estado_final_sdk": aud["estado_final_sdk"]}


def analizar(exp: Path, b1: Path | None = None) -> dict:
    ex = json.loads((exp / "experimento.json").read_text())
    plan = json.loads((exp / "plan.json").read_text())
    oh1 = hermano("oh-duo-01", "duo.py")
    umbrales = json.loads((hermano("oh-duo-02", "estancamiento.py") / "umbrales.json").read_text())
    bw = shutil.which("bwrap")
    if not bw:
        raise SystemExit("hace falta bwrap para evaluar las versiones del archivo")
    ev = AUD.Evaluador(oh1, bw)
    a1 = Path(plan["a1"])
    ea = json.loads((a1 / "experimento.json").read_text())
    ca = Path(ea.get("corridas_dir") or AQUI.parent / "corridas")
    ref = B.cargar(plan["corrida_de_referencia"], ca)
    ce = Path(ex["corridas_dir"])
    conds = {c: analizar_condicion(ce / n, ref, ev, umbrales) for c, n in ex["corridas"].items()}
    base = {"A1": linea_base(ca / plan["corrida_de_referencia"], ev, umbrales)}
    if b1 and (b1 / "experimento.json").is_file():
        eb = json.loads((b1 / "experimento.json").read_text())
        base["B1"] = linea_base(Path(eb.get("corridas_dir") or AQUI.parent / "corridas") / eb["corridas"][0], ev, umbrales)
    a0 = A.leer(exp / "a0" / "directo.json")
    return {"experimento": exp.name, "plan": {k: plan.get(k) for k in ("k", "codigo_a_la_vista_en_k", "a1_vio_el_fallo_en", "a1_vio_el_codigo_en",
                                                                     "j", "edicion_rechazada", "avisos", "sonda_a", "sonda_b", "max_acciones")},
            "a0": a0, "condiciones": conds, "lineas_de_base": base, "diferencias": ex.get("diferencias")}


def _si(x) -> str:
    return "—" if x is None else ("sí" if x else "**no**")


def markdown(r: dict) -> str:
    p = r["plan"]
    ls = [f"# E1 · de la solución a la edición · `{r['experimento']}`", "",
          f"Puntos tomados de A1: k = {p['k']} · j = {p['j']} (primera edición rechazada). En A1 el agente vio el fallo en la acción "
          f"{p['a1_vio_el_fallo_en']} y leyó intervalos.py en la acción {p['a1_vio_el_codigo_en'] if p['a1_vio_el_codigo_en'] is not None else '**nunca**'}; "
          f"código a la vista al preguntar en E1-AB: {_si(p['codigo_a_la_vista_en_k'])}."]
    if p["edicion_rechazada"]:
        er = p["edicion_rechazada"]
        ls.append(f"Edición rechazada de A1 (acción {p['j']}): `{er['comando']}` sobre `{er['ruta']}`: "
                  f"`{str(er['old_str'])[:80]}` → `{str(er['new_str'])[:80]}`.")
    ls += [f"Avisos del plan: {p['avisos'] or 'ninguno'}", ""]
    a0 = r["a0"] or {}
    e0 = a0.get("evaluacion") or {}
    for f, v in (a0.get("evaluados") or {}).items():
        ls.append(f"Evaluado sin inferencia: `{f}` → visibles {v['visibles']['pasan']}/{v['visibles']['ejecutadas']}, ocultas "
                  f"{v['ocultas']['pasan']}/{v['ocultas']['ejecutadas']}, idéntico a la referencia {_si(v['identico_a_la_referencia'])}.")
    if a0.get("extraccion"):
        ls.append(f"E1-A0: la respuesta traía {a0['extraccion']['bloques']} bloque(s) de código; el bloque incluía pruebas: "
                  f"{_si(a0['extraccion']['bloque_incluia_pruebas'])} (se extrajo solo el código de producción).")
    ls += ["", "## Resultado por etapa", "",
           "| condición | 0 validez | 1 comprensión | 2 formulación | 3 aplicación | 4 verificación (visibles · ocultas) | **etapa de ruptura** |",
           "|---|---|---|---|---|---|---|"]
    if a0:
        comp = "**no** (no se pudo extraer código)" if not e0 else f"{_si(e0.get('contiene_la_correccion'))} (idéntico a la referencia: {_si(e0.get('identico_a_la_referencia'))})"
        ls.append(f"| E1-A0 directo (sin agente) | — | {comp} | — | — | "
                  f"{(e0.get('visibles') or {}).get('pasan')}/{(e0.get('visibles') or {}).get('ejecutadas')} · "
                  f"{(e0.get('ocultas') or {}).get('pasan')}/{(e0.get('ocultas') or {}).get('ejecutadas')} | — |")
    for c, x in r["condiciones"].items():
        e = x["etapas"]
        v = e.get("4_verificacion") or {}
        ls.append(f"| {c} | {_si((e.get('0_validez') or {}).get('ok'))} | {_si((e.get('1_comprension') or {}).get('ok'))} | "
                  f"{_si((e.get('2_formulacion') or {}).get('ok'))} | {_si((e.get('3_aplicacion') or {}).get('ok'))} | "
                  f"{(v.get('visibles') or {}).get('pasan')}/{(v.get('visibles') or {}).get('ejecutadas')} · "
                  f"{(v.get('ocultas') or {}).get('pasan')}/{(v.get('ocultas') or {}).get('ejecutadas')} | **{x.get('etapa_de_ruptura')}** |")
    ls += ["", "## Líneas de base (sin preguntas)", "",
           "| corrida | acciones | ediciones intentadas | aplicadas | ¿alguna con la corrección? | intentos sobre pruebas | final visibles · ocultas |",
           "|---|---|---|---|---|---|---|"]
    for n, b in r["lineas_de_base"].items():
        ls.append(f"| {n} · {b['corrida']} | {b['acciones']} | {b['ediciones_intentadas']} | {b['ediciones_aplicadas']} | "
                  f"{_si(b['alguna_con_la_correccion'])} | {b['intentos_sobre_pruebas']} | "
                  f"{(b['final_visibles'] or {}).get('pasan')}/{(b['final_visibles'] or {}).get('ejecutadas')} · "
                  f"{(b['final_ocultas'] or {}).get('pasan')}/{(b['final_ocultas'] or {}).get('ejecutadas')} |")
    for c, x in r["condiciones"].items():
        e = x["etapas"]
        ls += ["", f"## {c} en detalle (`{x['corrida']}`)", ""]
        v0 = e.get("0_validez") or {}
        ls.append(f"- **0 validez**: pregunta A en la llamada {v0.get('llamada_de_la_pregunta')} (tras {v0.get('acciones_antes')} acciones); "
                  f"prefijo de {v0.get('prefijo_llamadas')} llamadas, distintas de A1: {v0.get('prefijo_distinto_en')} "
                  f"(solo ruido: {v0.get('prefijo_solo_ruido')}); pedido de la pregunta = A1 + pregunta: {v0.get('pedido_de_la_pregunta_igual_a_a1_mas_pregunta')}")
        c1 = e.get("1_comprension")
        if c1:
            ls.append(f"- **1 comprensión**: respondió en texto: {_si(c1['respondio_en_texto'])}; nombra la prueba {_si(c1['nombra_la_prueba'])}, "
                      f"esperado {_si(c1['dice_lo_esperado'])}, obtenido {_si(c1['dice_lo_obtenido'])}, línea {_si(c1['senala_la_linea'])}, "
                      f"**corrección {_si(c1['ok'])}**, culpa a la prueba {_si(c1['culpa_a_la_prueba'])}; "
                      f"acciones antes de responder: {c1['acciones_en_lugar_de_responder'] or 'ninguna'} (leyó intervalos.py: "
                      f"{_si(c1['leyo_intervalos_durante_a'])}; editó pese a la consigna: {c1['edito_durante_a'] or 'no'})")
            ls.append(f"  - respuesta: «{c1['respuesta'][:700]}»")
        c2 = e.get("2_formulacion")
        if c2:
            pe = c2["primera_edicion"]
            ls.append(f"- **2 formulación**: acciones tras B: {c2['acciones_tras_b']}")
            if pe:
                ls.append(f"  - primera edición: acción {pe['accion']}, `{pe['comando']}` sobre `{pe['ruta']}`: `{str(pe['old_str'])[:100]}` → "
                          f"`{str(pe['new_str'])[:120]}`; apunta a intervalos.py {_si(c2['apunta_a_intervalos'])}; contiene la corrección {_si(c2['contiene_la_correccion'])}")
            else:
                ls.append("  - **no hubo ninguna edición** después de la pregunta B")
        c3 = e.get("3_aplicacion")
        if c3 and c3["intento"]:
            pe = c2["primera_edicion"]
            ls.append(f"- **3 aplicación**: intento sí · aplicada {_si(c3['aplicada'])} (reconstrucción: {pe['efecto_reconstruido']}; "
                      f"herramienta: {pe['herramienta_dice']}) · observación real: «{pe['observacion'][:240]}»")
            rc = c3.get("recuperacion")
            if rc:
                ls.append(f"  - recuperación: acción siguiente {rc['accion_siguiente']}; interpreta el error {_si(rc['interpreta_el_error'])}; "
                          f"aplica la corrección después {_si(rc['aplica_la_correccion_despues'])}")
        c4 = e.get("4_verificacion")
        if c4:
            ls.append(f"- **4 verificación**: final visibles {(c4['visibles'] or {}).get('pasan')}/{(c4['visibles'] or {}).get('ejecutadas')}, "
                      f"ocultas {(c4['ocultas'] or {}).get('pasan')}/{(c4['ocultas'] or {}).get('ejecutadas')}; reconstrucción = archivo final {_si(c4['reconstruccion_coincide'])}")
        if x.get("intentos_sobre_pruebas_tras_b"):
            ls.append(f"- intentos sobre pruebas protegidas después de B: {[(q['accion'], q['via']) for q in x['intentos_sobre_pruebas_tras_b']]}")
        for q in x["problemas"] + [f"aviso: {a}" for a in x["avisos"]]:
            ls.append(f"- {q}")
    ls += ["", "## Cómo leer las comparaciones (E1.md)", "",
           "- **A0 frente a E1-AB, etapa 1**: mismo modelo y opciones, mismo código y pruebas; cambia TODO el contexto del agente "
           "(system prompt de OpenHands, herramientas, historia hasta k, formato de la pregunta). Si A0 identifica y E1-AB no, "
           "el problema está asociado al contexto o las instrucciones del agente (tipo 1), sin aislar qué parte.",
           "- **E1-AB, etapa 1 → 2**: con su propia corrección en el contexto, ¿la convierte en una acción? Si falla la 2, es un "
           "problema al formular la edición (tipo 2).",
           "- **E1-AB, etapa 3 y recuperación**: si la herramienta la rechaza, ¿interpreta el error (tipo 3) y vuelve a una "
           "estrategia que aplique la corrección (tipo 4)?",
           "- **E1-AB frente a E1-C**: mismas preguntas; E1-C además tiene en el contexto el rechazo REAL de A1 (y las acciones "
           "entre k y j). Una diferencia se asocia a ese rechazo previo, con esa salvedad.",
           "- **A1 frente a E1-AB**: A1 nunca recibe preguntas; si E1-AB completa la cadena, lo que le faltó a A1 no es la "
           "capacidad de editar sino el paso explícito de diagnóstico.",
           "- Una sola semilla (11): cada condición es un caso, no una medición.", ""]
    return "\n".join(ls)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--experimento", type=Path, required=True)
    ap.add_argument("--b1", type=Path)
    a = ap.parse_args(argv)
    r = analizar(a.experimento, a.b1)
    (a.experimento / "informe_e1.json").write_text(json.dumps(r, ensure_ascii=False, indent=1, default=str))
    (a.experimento / "informe_e1.md").write_text(markdown(r))
    print(markdown(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
