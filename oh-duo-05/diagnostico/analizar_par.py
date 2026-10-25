"""Diagnóstico de un par control/intervenida de OH-DUO-05, SOLO LECTURA. Sin inferencia ni red.

  <venv>/bin/python -I analizar_par.py --protocolo ~/oh-duo-05/informes/qwen-p1-s11-<etiqueta> [--salida DIR]
  <venv>/bin/python -I analizar_par.py --control CORRIDA --intervenida CORRIDA [--salida DIR]

Necesita el venv del SDK (OpenHands 1.58.1) solo para reproducir el detector `stuck` del SDK sobre los eventos
guardados; lo demás es biblioteca estándar. Responde, con los registros de las dos corridas:
  1. entradas: ¿el primer pedido al modelo fue idéntico? (system prompt + herramientas, tarea, bytes del pedido,
     semilla/temperatura/num_ctx/stop de cada pedido, según el relay)
  2. primera divergencia: las acciones de los dos brazos lado a lado, con tokens y latencia de cada llamada
  3. estado de Ollama al empezar: modelo cargado o no, latencia y prompt_eval_count de la primera llamada
  4. stuck: el detector del SDK reproducido evento por evento (qué patrón, en qué evento, qué ventana) y los
     mensajes de usuario que reinician su ventana
  5. mensaje del coordinador: dónde entró, cuánto creció el prompt en la llamada siguiente, primera acción
Escribe diagnostico.json y diagnostico.md en --salida (por omisión, <protocolo>/diagnostico/).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

PREFIJO = "oh-duo-04:"
FORMATO_INVALIDO = ("for function '", "not found in available tools", "is expected to be")


def sha(x) -> str:
    return hashlib.sha256((x if isinstance(x, str) else json.dumps(x, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()[:16]


def leer(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def lineas(p: Path) -> list[dict]:
    try:
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return []


def texto(msg: dict | None) -> str:
    return " ".join(c.get("text", "") for c in (msg or {}).get("content") or [] if isinstance(c, dict))


def corto(s, n=160) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + "…"


# ------------------------------------------------------------------------------------------------ una corrida

def localizar(corrida: Path) -> dict:
    ejs = sorted(p for p in (corrida / "ejecuciones").glob("*") if p.is_dir())
    if len(ejs) != 1:
        raise SystemExit(f"{corrida}: se esperaba una ejecución, hay {len(ejs)}")
    e = ejs[0]
    rs = sorted((e / "instancia" / "resultados").glob("*"))
    r = rs[-1] if rs else None
    convs = sorted((r / "estado" / "conversacion").glob("*/events")) if r else []
    return {"ejecucion": e, "resultado": r, "events": convs[-1] if convs else None,
            "base_state": convs[-1].parent / "base_state.json" if convs else None}


def cargar_eventos(d: Path) -> list[dict]:
    fs = sorted(p for p in d.iterdir() if p.name.startswith("event-") and p.suffix == ".json")
    return [dict(leer(p) or {}, _archivo=p.name) for p in fs]


def stuck_reproducido(d: Path) -> dict:
    """El StuckDetector del SDK aplicado a cada prefijo de la conversación guardada, como lo hace run():
    antes de cada paso del agente. Devuelve el primer punto en que daría stuck y con qué patrón."""
    try:
        os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
        import openhands.tools.file_editor  # noqa: F401   registran sus tipos para deserializar
        import openhands.tools.terminal  # noqa: F401
        from openhands.sdk.conversation.stuck_detector import MAX_EVENTS_TO_SCAN_FOR_STUCK_DETECTION as VENTANA
        from openhands.sdk.conversation.stuck_detector import StuckDetector
        from openhands.sdk.event import ActionEvent, MessageEvent
        from openhands.sdk.event.base import Event
    except Exception as e:     # sin el SDK o sin poder importarlo: el resto del diagnóstico sigue
        return {"disponible": False, "motivo": f"no se pudo importar el SDK: {e!r}"[:300]}
    fs = sorted(p for p in d.iterdir() if p.name.startswith("event-") and p.suffix == ".json")
    evs = [Event.model_validate_json(p.read_text(encoding="utf-8")) for p in fs]

    class Estado:
        def __init__(self, e):
            self.e = e

        def active_branch(self, limit=None):
            return self.e[-limit:] if limit else self.e

    def patron(det: StuckDetector) -> str | None:
        ev = det._events_since_last_user_message()
        mx = max(det.action_observation_threshold, det.action_error_threshold + 1)
        a, o = det._collect_actions_and_observations(ev, mx)
        if det._is_stuck_repeating_action_observation(a, o):
            return "accion_y_observacion_repetidas (4 iguales, pensamiento incluido)"
        if det._is_stuck_repeating_action_error(a, o):
            return "accion_con_error_repetida"
        if det._is_stuck_monologue(ev):
            return "monologo (3 mensajes del agente seguidos)"
        if len(ev) >= det.alternating_pattern_threshold and det._is_stuck_alternating_action_observation(ev):
            return "patron_alternado (A,B,A,B,A,B)"
        return None

    # run() lo consulta antes de cada agent.step(): después de una observación, error o mensaje
    for k in range(1, len(evs) + 1):
        if isinstance(evs[k - 1], ActionEvent):
            continue
        det = StuckDetector(Estado(evs[:k]))
        if det.is_stuck():
            ventana = det._events_since_last_user_message()
            primero = evs[:k].index(ventana[0]) if ventana else None
            return {"disponible": True, "stuck_tras_evento": k - 1, "patron": patron(det),
                    "ventana_desde_evento": primero, "ventana_eventos": len(ventana), "ventana_maxima": VENTANA}
    # sin stuck: la racha más larga de acciones idénticas consecutivas (como las compara el SDK)
    acciones = [e for e in evs if isinstance(e, ActionEvent)]
    racha = mejor = 1 if acciones else 0
    for x, y in zip(acciones, acciones[1:]):
        racha = racha + 1 if StuckDetector(Estado([]))._event_eq(x, y) else 1
        mejor = max(mejor, racha)
    reinicios = [i for i, e in enumerate(evs) if isinstance(e, MessageEvent) and e.source == "user"]
    return {"disponible": True, "stuck_tras_evento": None, "racha_maxima_acciones_identicas": mejor,
            "mensajes_user_que_reinician_la_ventana": reinicios}


def contexto_reconstruido(d: Path, hasta: int, buscado: str) -> dict:
    """Los mensajes que el SDK arma para el LLM con los eventos 0..hasta-1 (la llamada que produjo el evento
    `hasta`), con la misma función que usa el agente sin condensador: ¿está el texto del coordinador y dónde?"""
    try:
        os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
        import openhands.tools.file_editor  # noqa: F401
        import openhands.tools.terminal  # noqa: F401
        from openhands.sdk.event.base import Event, LLMConvertibleEvent
    except Exception as e:
        return {"disponible": False, "motivo": f"no se pudo importar el SDK: {e!r}"[:300]}
    fs = sorted(p for p in d.iterdir() if p.name.startswith("event-") and p.suffix == ".json")[:hasta]
    evs = [Event.model_validate_json(p.read_text(encoding="utf-8")) for p in fs]
    msgs = LLMConvertibleEvent.events_to_messages([e for e in evs if isinstance(e, LLMConvertibleEvent)])
    pos = [j for j, m in enumerate(msgs) if any(buscado in getattr(c, "text", "") for c in m.content)]
    return {"disponible": True, "mensajes_en_el_contexto": len(msgs), "posiciones_del_texto": pos,
            "rol": msgs[pos[-1]].role if pos else None,
            "mensajes_despues_del_texto": len(msgs) - 1 - pos[-1] if pos else None,
            "caracteres_del_contexto": sum(len(getattr(c, "text", "")) for m in msgs for c in m.content)}


def analizar_corrida(corrida: Path, con_stuck: bool = True) -> dict:
    loc = localizar(corrida)
    r, d = loc["resultado"], loc["events"]
    reg = leer(loc["ejecucion"] / "ejecucion.json") or {}
    eventos = cargar_eventos(d) if d else []
    base = leer(loc["base_state"]) if loc["base_state"] else None
    met = (((base or {}).get("stats") or {}).get("usage_to_metrics") or {}).get("agente") or {}
    tokens = {t.get("response_id"): t for t in met.get("token_usages") or []}
    latencias = {t.get("response_id"): t.get("latency") for t in met.get("response_latencies") or []}
    relay = lineas(r / "puente.jsonl") if r else []
    chats = [x for x in relay if x.get("ruta") == "/api/chat" and x.get("permitido")]

    sp = next((e for e in eventos if e.get("kind") == "SystemPromptEvent"), None)
    usuario = [(i, e) for i, e in enumerate(eventos) if e.get("kind") == "MessageEvent" and e.get("source") == "user"]
    respuestas, acciones = [], []
    for i, e in enumerate(eventos):
        if e.get("source") == "agent" and e.get("kind") in ("ActionEvent", "MessageEvent"):
            rid = e.get("llm_response_id") or f"sin-id-{i}"
            if rid not in respuestas:
                respuestas.append(rid)
        if e.get("kind") == "ActionEvent":
            a, tc = e.get("action") or {}, e.get("tool_call") or {}
            t = tokens.get(e.get("llm_response_id")) or {}
            acciones.append({"n": len(acciones), "evento": i, "llamada": respuestas.index(e.get("llm_response_id") or f"sin-id-{i}"),
                             "herramienta": e.get("tool_name"), "argumentos": corto(tc.get("arguments") or a, 220),
                             "pensamiento": corto(" ".join(x.get("text", "") for x in e.get("thought") or []), 220),
                             "firma": sha([e.get("tool_name"), tc.get("arguments")]),
                             "prompt_tokens": t.get("prompt_tokens"), "completion_tokens": t.get("completion_tokens"),
                             "latencia": round(latencias.get(e.get("llm_response_id")) or 0, 2) or None})
    llamadas = [{"n": j, "response_id": rid, "prompt_tokens": (tokens.get(rid) or {}).get("prompt_tokens"),
                 "completion_tokens": (tokens.get(rid) or {}).get("completion_tokens"),
                 "latencia": round(latencias.get(rid) or 0, 2) or None} for j, rid in enumerate(respuestas)]
    mensajes = []
    for i, e in usuario[1:]:
        t = texto(e.get("llm_message"))
        sig_ev = next((a["evento"] for a in acciones if a["evento"] > i), None)
        tipo = "coordinador" if (e.get("sender") or "").startswith(PREFIJO) else \
            "correccion_de_formato_del_sdk" if any(k in t for k in FORMATO_INVALIDO) else "otro"
        previa = sum(1 for a in acciones if a["evento"] < i)
        sig = next((a for a in acciones if a["evento"] > i), None)
        mensajes.append({"evento": i, "tipo": tipo, "sender": e.get("sender"), "texto": corto(t, 400),
                         "acciones_antes": previa, "primera_accion_despues": sig,
                         "prompt_tokens_llamada_anterior": (llamadas[sig["llamada"] - 1]["prompt_tokens"] if sig and sig["llamada"] else None),
                         "prompt_tokens_llamada_siguiente": sig["prompt_tokens"] if sig else None,
                         "contexto_de_la_llamada_siguiente": contexto_reconstruido(d, sig_ev, t)
                         if (con_stuck and sig_ev is not None and (e.get("sender") or "").startswith(PREFIJO)) else None})
    errores = [{"evento": i, "codigo": e.get("code"), "detalle": corto(e.get("detail") or e.get("error"), 200)}
               for i, e in enumerate(eventos) if e.get("kind") in ("ConversationErrorEvent", "AgentErrorEvent")]
    return {
        "corrida": corrida.name, "ejecucion": loc["ejecucion"].name, "resultado": str(r) if r else None,
        "estado_final_sdk": (base or {}).get("execution_status"),
        "condensador": ((base or {}).get("agent") or {}).get("condenser"), "estado_ejecucion": reg.get("estado_ejecucion"),
        "ollama": {"modelos_cargados_antes": reg.get("modelos_cargados_antes"), "cambio_de_modelo": reg.get("cambio_de_modelo"),
                   "api_ps_al_terminar": leer(r / "api_ps.json") if r else None},
        "entradas": {
            "system_prompt": sha(sp.get("system_prompt")) if sp else None, "herramientas": sha(sp.get("tools")) if sp else None,
            "tarea": sha(texto(usuario[0][1].get("llm_message"))) if usuario else None,
            "bytes_primer_pedido": chats[0].get("bytes_enviados") if chats else None,
            "opciones_de_los_pedidos": sorted({json.dumps({k: c.get(k) for k in ("seed", "temperature", "num_ctx", "num_predict", "stop")},
                                                           sort_keys=True) for c in chats}),
            "pedidos_chat": len(chats), "pedidos_rechazados_por_el_relay": sum(1 for x in relay if x.get("permitido") is False),
            "segundos_primer_pedido_segun_relay": chats[0].get("segundos") if chats else None,
        },
        "llamadas": llamadas, "acciones": acciones, "mensajes_de_usuario_posteriores": mensajes, "errores": errores,
        "entregas_del_puente": lineas(r / "estado" / "entregas.jsonl") if r else [],
        "stuck": stuck_reproducido(d) if (con_stuck and d) else {"disponible": False},
    }


# ---------------------------------------------------------------------------------------------------- el par

def comparar(c: dict, i: dict) -> dict:
    ec, ei = c["entradas"], i["entradas"]
    iguales = {k: ec[k] == ei[k] for k in ("system_prompt", "herramientas", "tarea", "bytes_primer_pedido", "opciones_de_los_pedidos")}
    n = next((j for j, (x, y) in enumerate(zip(c["acciones"], i["acciones"])) if x["firma"] != y["firma"]), None)
    if n is None and len(c["acciones"]) != len(i["acciones"]):
        n = min(len(c["acciones"]), len(i["acciones"]))
    msg = next((m for m in i["mensajes_de_usuario_posteriores"] if m["tipo"] == "coordinador"), None)
    lado = lambda a, k: a["acciones"][k] if k is not None and k < len(a["acciones"]) else None  # noqa: E731
    l1c, l1i = (c["llamadas"] or [{}])[0], (i["llamadas"] or [{}])[0]
    hip = []
    hip.append({"hipotesis": "H1 las entradas del primer pedido fueron distintas",
                "veredicto": "descartada" if all(iguales.values()) else "CONFIRMADA",
                "evidencia": iguales})
    frio = {"control": c["ollama"]["cambio_de_modelo"], "intervenida": i["ollama"]["cambio_de_modelo"]}
    hip.append({"hipotesis": "H2 Ollama empezó en estados distintos (modelo sin cargar en un brazo)",
                "veredicto": "CONFIRMADA" if frio["control"] != frio["intervenida"] else
                ("indeterminada" if None in frio.values() else "descartada"),
                "evidencia": {"cambio_de_modelo": frio, "latencia_primera_llamada": {"control": l1c.get("latencia"), "intervenida": l1i.get("latencia")},
                              "modelos_cargados_antes": {"control": c["ollama"]["modelos_cargados_antes"],
                                                         "intervenida": i["ollama"]["modelos_cargados_antes"]}}})
    pt = {"control": l1c.get("prompt_tokens"), "intervenida": l1i.get("prompt_tokens")}
    hip.append({"hipotesis": "H3 Ollama reutilizó caché de prefijo en un brazo (prompt_eval_count menor con el mismo pedido)",
                "veredicto": "indeterminada" if None in pt.values() or not iguales["bytes_primer_pedido"] else
                ("COMPATIBLE" if pt["control"] != pt["intervenida"] else "sin indicio"),
                "evidencia": {"prompt_tokens_primera_llamada": pt,
                              "nota": "Ollama informa en prompt_eval_count solo los tokens que evaluó: con caché, menos"}})
    hip.append({"hipotesis": "H4 variabilidad residual del muestreo con entradas idénticas (semilla 11, temperatura 0,2)",
                "veredicto": "a evaluar con repeticiones" if all(iguales.values()) else "no aplica mientras H1 no se descarte",
                "evidencia": {"primera_divergencia": n}})
    return {"entradas_iguales": iguales, "primera_divergencia": n,
            "acciones_en_la_divergencia": {"control": lado(c, n), "intervenida": lado(i, n)},
            "acciones_previas_identicas": n if n is not None else len(c["acciones"]),
            "mensaje": msg, "hipotesis": hip,
            "stuck": {"control": c["stuck"], "intervenida": i["stuck"],
                      "estado_final_sdk": {"control": c["estado_final_sdk"], "intervenida": i["estado_final_sdk"]}}}


def markdown(c: dict, i: dict, p: dict) -> str:
    ls = [f"# Diagnóstico del par `{c['corrida']}` / `{i['corrida']}`", ""]
    ls += ["## 1. Entradas del primer pedido", "", "| | control | intervenida | iguales |", "|---|---|---|---|"]
    for k, v in p["entradas_iguales"].items():
        ls.append(f"| {k} | `{c['entradas'][k]}` | `{i['entradas'][k]}` | {'sí' if v else '**NO**'} |")
    ls += ["", f"Pedidos al modelo: {c['entradas']['pedidos_chat']} / {i['entradas']['pedidos_chat']}; "
               f"rechazados por el relay: {c['entradas']['pedidos_rechazados_por_el_relay']} / {i['entradas']['pedidos_rechazados_por_el_relay']}.", ""]
    ls += ["## 2. Primera divergencia", "",
           f"Acciones idénticas antes de divergir: **{p['acciones_previas_identicas']}** (divergencia en la acción {p['primera_divergencia']}).", "",
           "| n | brazo | llamada | herramienta | argumentos | pensamiento | prompt_tokens | completion | latencia s |", "|---|---|---|---|---|---|---|---|---|"]
    lim = (p["primera_divergencia"] or 0) + 3
    for k in range(min(lim, max(len(c["acciones"]), len(i["acciones"])))):
        for nombre, a in (("control", c), ("intervenida", i)):
            if k < len(a["acciones"]):
                x = a["acciones"][k]
                ls.append(f"| {k} | {nombre} | {x['llamada']} | {x['herramienta']} | `{x['argumentos'][:90]}` | {x['pensamiento'][:70]} | "
                          f"{x['prompt_tokens']} | {x['completion_tokens']} | {x['latencia']} |")
    ls += ["", "## 3. Hipótesis sobre la divergencia", ""]
    for h in p["hipotesis"]:
        ls.append(f"- **{h['hipotesis']}** → {h['veredicto']}  \n  `{json.dumps(h['evidencia'], ensure_ascii=False)[:400]}`")
    ls += ["", "## 4. Detector stuck del SDK (reproducido sobre los eventos guardados)", ""]
    for nombre, a in (("control", c), ("intervenida", i)):
        s = a["stuck"]
        ls.append(f"- **{nombre}** (estado final del SDK: `{a['estado_final_sdk']}`): " + (
            "sin SDK para reproducirlo" if not s.get("disponible") else
            f"stuck tras el evento {s['stuck_tras_evento']}, patrón **{s['patron']}**, ventana desde el evento "
            f"{s['ventana_desde_evento']} ({s['ventana_eventos']} eventos; máximo {s['ventana_maxima']})" if s.get("stuck_tras_evento") is not None else
            f"nunca stuck; racha máxima de acciones idénticas: {s['racha_maxima_acciones_identicas']} (hacen falta 4); "
            f"mensajes de usuario que reinician la ventana en los eventos {s['mensajes_user_que_reinician_la_ventana']}"))
    ls += ["", "Mensajes de usuario después de la tarea (cada uno reinicia la ventana del detector):", ""]
    for nombre, a in (("control", c), ("intervenida", i)):
        tipos = [f"{m['evento']}:{m['tipo']}" for m in a["mensajes_de_usuario_posteriores"]]
        ls.append(f"- {nombre}: {tipos or 'ninguno'}")
    ls += ["", "## 5. Mensaje del coordinador", ""]
    m = p["mensaje"]
    if not m:
        ls.append("No hay mensaje del coordinador en la conversación de la intervenida.")
    else:
        sig = m["primera_accion_despues"] or {}
        ls += [f"- Evento {m['evento']}, `{m['sender']}`, después de {m['acciones_antes']} acciones.",
               f"- Texto: «{m['texto']}»",
               f"- prompt_tokens de la llamada anterior → la siguiente: {m['prompt_tokens_llamada_anterior']} → "
               f"{m['prompt_tokens_llamada_siguiente']} (si crece al menos el largo del mensaje, entró en el contexto)",
               f"- Primera acción posterior: acción {sig.get('n')} (evento {sig.get('evento')}), `{sig.get('herramienta')}` "
               f"`{sig.get('argumentos', '')[:120]}` — pensamiento: «{sig.get('pensamiento', '')[:160]}»",
               f"- Puente: {[x.get('resultado') for x in i['entregas_del_puente']]}"]
        cx = m.get("contexto_de_la_llamada_siguiente") or {}
        if cx.get("disponible"):
            ls.append(f"- Contexto reconstruido de esa llamada (con el SDK, condensador: `{i['condensador']}`): "
                      f"{cx['mensajes_en_el_contexto']} mensajes, {cx['caracteres_del_contexto']} caracteres; el texto del "
                      f"coordinador está en la posición {cx['posiciones_del_texto']} como `{cx['rol']}`, seguido de "
                      f"{cx['mensajes_despues_del_texto']} mensaje(s)" if cx["posiciones_del_texto"] else
                      "- Contexto reconstruido de esa llamada: **el texto del coordinador NO está**")
    ls += ["", "## Errores y estado final", ""]
    for nombre, a in (("control", c), ("intervenida", i)):
        ls.append(f"- {nombre}: `{a['estado_ejecucion']}` / SDK `{a['estado_final_sdk']}`; errores: "
                  f"{[(e['evento'], e['codigo']) for e in a['errores']] or 'ninguno'}")
    return "\n".join(ls) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--protocolo", type=Path, help="informes/qwen-p1-s<semilla>-<etiqueta> (lee protocolo.json)")
    ap.add_argument("--corridas", type=Path, default=Path(__file__).resolve().parent.parent / "corridas")
    ap.add_argument("--control", type=Path)
    ap.add_argument("--intervenida", type=Path)
    ap.add_argument("--salida", type=Path)
    a = ap.parse_args(argv)
    if a.protocolo:
        pr = json.loads((a.protocolo / "protocolo.json").read_text())
        ctrl, inter = (a.corridas / pr["brazos"][b]["corrida"] for b in ("control", "intervenida"))
    elif a.control and a.intervenida:
        ctrl, inter = a.control, a.intervenida
    else:
        ap.error("hace falta --protocolo o --control y --intervenida")
    salida = a.salida or ((a.protocolo / "diagnostico") if a.protocolo else Path.cwd() / "diagnostico")
    for x in (ctrl, inter):
        if salida.resolve() == x.resolve() or x.resolve() in salida.resolve().parents:
            raise SystemExit("la salida no puede estar dentro de una corrida")
    c, i = analizar_corrida(ctrl), analizar_corrida(inter)
    p = comparar(c, i)
    salida.mkdir(parents=True, exist_ok=True)
    (salida / "diagnostico.json").write_text(json.dumps({"control": c, "intervenida": i, "par": p}, ensure_ascii=False, indent=1))
    (salida / "diagnostico.md").write_text(markdown(c, i, p))
    print(markdown(c, i, p))
    print(f"Diagnóstico: {salida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
