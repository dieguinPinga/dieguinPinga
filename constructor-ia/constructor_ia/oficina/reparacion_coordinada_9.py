"""Experimento 7.9: revisión CRÍTICA de parches entre agentes (OFICINA.md, «Experimento 7.9»).

En el 7.8 Qwen propuso tres veces `if ref is None:` → `if ref is None or ref['tipo'] == 'anulacion':`: los parches se
aplicaban y la discrepancia seguía. El 7.9 es el recorrido del 7.8 con un rol más, interpretado por el MISMO modelo
(gemma4:12b) en una invocación separada:
1. DIAGNOSTICADORA (gemma): como en el 7.8 (causa, mecanismo, comportamiento defectuoso y a preservar);
2. PROGRAMADOR (qwen): propone un parche; el coordinador lo valida y lo aplica sobre una copia (con la normalización
   de la sangría) como siempre — un parche inválido ni siquiera llega a revisión;
3. REVISORA (gemma, otra invocación): recibe el contrato, el código ORIGINAL (las zonas), el diagnóstico, el parche
   aplicado, su justificación y la diferencia resultante. NINGÚN resultado de pruebas (ni el caso sintético sobre el
   parche, ni ejemplos, ni la batería). Decide «aprobar» o «rechazar» con una justificación causal y, si rechaza, una
   crítica;
4. COORDINADOR: si rechaza, la crítica llega ÍNTEGRA al intento siguiente del programador; si aprueba (o la revisión
   es inválida), siguen las comprobaciones deterministas de siempre (caso sintético, ejemplos visibles y la batería
   independiente una sola vez). La aprobación NO garantiza nada.
Capacidad crítica: cada decisión se compara con lo OBSERVABLE (el caso sintético y los ejemplos visibles sobre el
parche, calculados igual aunque no se le muestren) y, si se ejecutó, con la batería: se registran las aprobaciones
incorrectas (aprobó un parche defectuoso) y los rechazos de parches observablemente correctos.
Límites: 2 diagnósticos, 3 propuestas de Qwen (cada una con a lo sumo una revisión) y 2400 s para todo.
"""
from __future__ import annotations

import json
import re
import time

from .. import prompts
from ..ollama import OllamaError
from . import parches as PA
from . import reparacion_coordinada as RC
from . import reparacion_coordinada_8 as RC8
from . import reparacion_guiada as RGD
from . import revision_existente as RE
from . import revisor as RV
from .misiones_programacion import MISIONES

VERSION_9 = ("experimento 7.9: diagnóstico (gemma), parche (qwen) y revisión crítica del parche (gemma, otra "
             "invocación) coordinados, con preservación de comportamiento y normalización de sangría")
CLASIFICACION_9 = "diagnóstico, reparación y revisión crítica automáticos coordinados"
MAX_CRITICA = 1500

SISTEMA_REVISORA = (
    "Sos la REVISORA de parches de un equipo (un rol distinto de quien diagnosticó y de quien programó). Recibís el "
    "contrato, el código original, un diagnóstico de la causa y un parche que otro programador propuso. NO tenés "
    "resultados de pruebas: razoná sobre el código.\n"
    "Evaluá dos cosas: (1) si el parche elimina la CAUSA diagnosticada (no solo un síntoma) y (2) si conserva el "
    "comportamiento correcto que hay que preservar. Seguí mentalmente la entrada del caso con el código parcheado.\n"
    "Respondé SOLO con un objeto JSON con esta forma:\n"
    '{"decision": "aprobar | rechazar", "elimina_la_causa": true | false, "preserva_comportamiento": true | false, '
    '"justificacion_causal": "<por qué el parche elimina o no la causa, y qué comportamiento conserva o rompe>", '
    '"critica": "<si rechazás: qué está mal y qué propiedad tendría que cumplir la corrección, sin escribir código>"}\n'
    "No escribas código corregido ni bloques de código. Aprobá solo si las dos cosas se cumplen.")
PEDIDO_REVISION = "Revisá el parche y respondé solo con el JSON pedido."


def mensajes_revision(f: dict, parche: dict, original: str, nuevo: str) -> list[dict]:
    fabrica, _ = MISIONES[f["mision"]]
    m = fabrica()
    spec = m["spec"]
    cambios = "\n\n".join(f"Cambio {i}:\n  ORIGINAL:\n{c['original']}\n  REEMPLAZO:\n{c['reemplazo']}"
                          for i, c in enumerate(parche["cambios"], 1))
    diff = RE.cambio_de_codigo(original, nuevo)["diff"]
    usuario = (f"CONTRATO:\n{m['description']}\n\n{prompts._sig(spec)}\n\n"
               f"Ejemplos visibles:\n{prompts._examples(spec, 3000) or '(sin ejemplos visibles)'}\n\n"
               "CÓDIGO ORIGINAL (las zonas que se pueden cambiar y un contexto de solo lectura; los números de línea "
               f"NO son parte del código):\n\n{PA.texto_zonas(original, spec['function'], {'zonas': f['zonas']})}\n\n"
               f"DIAGNÓSTICO DE LA CAUSA (de la diagnosticadora, otra invocación):\n{f['causa']}\n\n"
               f"PARCHE PROPUESTO POR EL PROGRAMADOR (el coordinador ya comprobó que se puede aplicar):\n{cambios}\n\n"
               f"Justificación del programador: {parche.get('justificacion') or '(sin justificación)'}\n\n"
               f"DIFERENCIA RESULTANTE EN EL CÓDIGO (sin comentarios):\n{diff}\n\n{PEDIDO_REVISION}")
    return [{"role": "system", "content": SISTEMA_REVISORA}, {"role": "user", "content": usuario}]


def interpretar_revision(texto: str, funcion: str) -> dict:
    """ValueError si la revisión no es utilizable."""
    t = (texto or "").strip()
    if re.search(rf"\bdef\s+{re.escape(funcion)}\s*\(", t):
        raise ValueError("incluyó el programa: la revisora no escribe código")
    mt = re.search(r"\{.*\}", t, re.S)
    if not mt:
        raise ValueError("la respuesta no contiene un objeto JSON")
    try:
        d = json.loads(mt.group(0))
    except ValueError as e:
        raise ValueError(f"JSON ilegible: {e}") from None
    if not isinstance(d, dict):
        raise ValueError("la respuesta no es un objeto JSON")
    decision = str(d.get("decision") or "").strip().lower()
    if decision not in ("aprobar", "rechazar"):
        raise ValueError("«decision» tiene que ser «aprobar» o «rechazar»")
    justificacion = RV._limpio(d.get("justificacion_causal"), MAX_CRITICA) if isinstance(
        d.get("justificacion_causal"), str) else ""
    critica = RV._limpio(d.get("critica"), MAX_CRITICA) if isinstance(d.get("critica"), str) else ""
    if not justificacion:
        raise ValueError("falta «justificacion_causal»")
    if decision == "rechazar" and not critica:
        raise ValueError("un rechazo necesita «critica»")

    def bool_o_none(v):
        return v if isinstance(v, bool) else None
    return {"decision": decision, "elimina_la_causa": bool_o_none(d.get("elimina_la_causa")),
            "preserva_comportamiento": bool_o_none(d.get("preserva_comportamiento")),
            "justificacion_causal": justificacion, "critica": critica}


def _si_no(v) -> str:
    return "sí" if v is True else "no" if v is False else "sin indicar"


def texto_critica(n: int, r: dict) -> str:
    """Lo que recibe el programador cuando la revisora rechaza (la crítica completa)."""
    return (f"REVISIÓN DE TU PARCHE DEL INTENTO {n} (otra IA, la revisora; NO es un resultado de pruebas): RECHAZADO.\n"
            f"¿Elimina la causa?: {_si_no(r['elimina_la_causa'])} · ¿Preserva el comportamiento?: "
            f"{_si_no(r['preserva_comportamiento'])}\n"
            f"Justificación causal: {r['justificacion_causal']}\n"
            f"Crítica: {r['critica']}\n"
            "Tu próximo parche se aplica de nuevo sobre el código original de arriba.")


def revisora(cliente, gate, modelo, cancel=None):
    """La función de revisión que usa reparacion_guiada.correr (una invocación de gemma por parche aplicado)."""
    def revisar(n, f, parche, original, nuevo):
        fabrica, _ = MISIONES[f["mision"]]
        m = fabrica()
        msgs = mensajes_revision(f, parche, original, nuevo)
        registro = {"intento": n, "pedido": msgs, "valida": False, "decision": None}
        auditoria = RE.auditar([msgs], m["spec"], f["defecto"], m["description"])
        if not auditoria["sin_fugas"]:
            registro["motivo"] = "auditoría: información prohibida en el pedido; no se envió (" + ", ".join(
                auditoria["hallazgos"]) + ")"
            return {"decision": None, "registro": registro, "segundos": 0}
        t0 = time.monotonic()
        try:
            gate.acquire("revisora 7.9", cancel)
            try:
                gen = cliente.chat(modelo, msgs, cancel, fmt="json")
            finally:
                gate.release()
        except (OllamaError, OSError) as e:
            registro.update(motivo=f"la revisora no respondió: {e}", segundos=round(time.monotonic() - t0, 1))
            return {"decision": None, "registro": registro, "segundos": registro["segundos"]}
        registro.update(respuesta=gen.text, segundos=round(time.monotonic() - t0, 1), tokens_prompt=gen.prompt_tokens,
                        tokens_generados=gen.eval_tokens, fin=gen.done_reason, cortada=bool(gen.truncated))
        try:
            r = interpretar_revision(gen.text, m["spec"]["function"])
        except ValueError as e:
            # Revisión inválida: se registra y deciden las comprobaciones deterministas (no se inventa una decisión).
            registro["motivo"] = str(e) + (" (respuesta cortada)" if gen.truncated else "")
            return {"decision": None, "registro": registro, "segundos": registro["segundos"]}
        registro.update(valida=True, **r)
        aviso = texto_critica(n, r) if r["decision"] == "rechazar" else ""
        registro["aviso_al_programador"] = aviso or None
        return {"decision": r["decision"], "aviso": aviso, "registro": registro, "segundos": registro["segundos"]}
    return revisar


def capacidad_critica(intentos: list[dict]) -> dict:
    """Cada decisión de la revisora frente a lo observable (caso sintético y ejemplos visibles) y la batería."""
    filas = []
    for i in intentos:
        rv = i.get("revision")
        if not rv:
            continue
        observable_ok = i.get("discrepancia_sintetica") == "eliminada" and bool((i.get("visibles") or {}).get("ok"))
        bateria = (i.get("bateria") or {}).get("ok")
        d = rv.get("decision")
        filas.append({
            "intento": i["intento"], "decision": d, "valida": rv.get("valida"),
            "observable_correcto": observable_ok, "bateria": bateria,
            "aprobacion_incorrecta": d == "aprobar" and (not observable_ok or bateria is False),
            "rechazo_de_parche_observablemente_correcto": d == "rechazar" and observable_ok,
            "deteccion_correcta": d == "rechazar" and not observable_ok,
        })
    return {"decisiones": filas,
            "aprobaciones_incorrectas": sum(x["aprobacion_incorrecta"] for x in filas),
            "rechazos_de_parches_observablemente_correctos": sum(x["rechazo_de_parche_observablemente_correcto"]
                                                                 for x in filas),
            "detecciones_correctas": sum(x["deteccion_correcta"] for x in filas),
            "revisiones_invalidas": sum(not x["valida"] for x in filas),
            "nota": "«observable» = caso sintético y ejemplos visibles sobre el parche (calculados aunque no se le "
                    "muestren a nadie); la batería solo se ejecuta sobre un parche aprobado que pasa lo observable"}


def carpeta(carpeta_lab, corrida):
    try:
        return RGD.carpeta_de_reparacion(carpeta_lab, corrida, "experimento-7-9")
    except RGD.ErrorReparacion as e:
        raise RC.ErrorCoordinada(str(e)) from None


def correr(destino, f, executor, cliente, gate, diagnosticador, programador, max_segundos: float = 2400.0,
           cancel=None) -> dict:
    ajustes = dict(RC8.AJUSTES, version=VERSION_9, clasificacion=CLASIFICACION_9,
                   revision=revisora(cliente, gate, diagnosticador, cancel))
    inf = RC.correr(destino, f, executor, cliente, gate, diagnosticador, programador, max_segundos, cancel,
                    ajustes=ajustes)
    intentos = (inf.get("reparacion") or {}).get("intentos", [])
    inf["capacidad_critica_de_la_revisora"] = capacidad_critica(intentos)
    verificacion = (inf.get("reparacion") or {}).get("verificacion")
    verificado = next((i for i in intentos if verificacion and i["intento"] == verificacion["intento"]), None)
    inf["criterios"]["5_revisora_aprueba_el_parche_verificado"] = bool(
        verificado and (verificado.get("revision") or {}).get("decision") == "aprobar")
    inf["exito"] = bool(inf["exito"] and inf["criterios"]["5_revisora_aprueba_el_parche_verificado"])
    if not inf["exito"]:
        inf["clasificacion"] = None
    (destino / "informe.json").write_text(json.dumps(inf, ensure_ascii=False, indent=2, default=str))
    return inf
