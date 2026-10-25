"""Clasifica las respuestas de medir.py con el convertidor REAL de OpenHands y emite el dictamen de la fase 1.

Corre dentro de un sandbox sin red (lo lanza clasificar.sh) con el venv de oh-smoke montado en solo lectura:
importa openhands.sdk.llm.mixins.fn_call_converter, la misma función que convierte las respuestas del modelo
en llamadas a herramientas durante una corrida. No hace pedidos a Ollama.

Uso (dentro del sandbox): python -I clasificar.py /salida/resultados.json /kit/herramientas.json
"""
import json
import sys
from pathlib import Path

from openhands.sdk.llm.exceptions import FunctionCallConversionError, FunctionCallValidationError
from openhands.sdk.llm.mixins.fn_call_converter import convert_non_fncall_messages_to_fncall_messages

# Lo que haría un buen agente en cada paso del pedido capturado (misión C01, promedio.py).
ESPERADO = {1: {"terminal"}, 2: {"file_editor", "terminal"}}


def clasificar(texto: str, herramientas: list) -> dict:
    """Igual que el SDK: llamada válida, error de formato (el SDK se lo devuelve al modelo) o texto (termina)."""
    rasgos = {"tool_code": "```tool_code" in texto, "json_en_texto": texto.lstrip().startswith("{"),
              "llamadas_en_el_texto": texto.count("<function="), "think": "<think>" in texto}
    try:
        ultimo = convert_non_fncall_messages_to_fncall_messages(
            [{"role": "assistant", "content": texto}], herramientas)[-1]
    except FunctionCallValidationError as e:
        return {"tipo": "error_de_formato", "detalle": str(e)[:300], **rasgos}
    except FunctionCallConversionError as e:
        return {"tipo": "error_de_conversion", "detalle": str(e)[:300], **rasgos}
    tc = ultimo.get("tool_calls")
    if not tc:
        return {"tipo": "texto_sin_herramienta", **rasgos}
    f = tc[0]["function"]
    return {"tipo": "llamada", "herramienta": f["name"], "argumentos": json.loads(f["arguments"]), **rasgos}


def main() -> int:
    resultados, herramientas = (json.loads(Path(x).read_text(encoding="utf-8")) for x in sys.argv[1:3])
    salida = Path(sys.argv[1]).parent
    validas = [x for x in resultados.get("llamadas", []) if x.get("http") == 200]
    filas = []
    for x in validas:
        c = clasificar(x.get("content") or "", herramientas)
        c["plausible"] = c["tipo"] == "llamada" and c["herramienta"] in ESPERADO[x["paso"]]
        filas.append({"paso": x["paso"], "semilla": x["semilla"], **c})

    sg = resultados.get("show_gemma") or {}
    ps = next((m for m in resultados.get("ps_tras_carga") or [] if m.get("name") == resultados.get("modelo")), {})
    llamadas = [f for f in filas if f["tipo"] == "llamada"]
    total = len(resultados.get("llamadas", []))
    carga = [x["load_s"] for x in validas if x.get("load_s") is not None]
    calientes = [x["segundos_reloj"] for x in validas[1:]]
    num_ctx = (validas[0]["num_ctx"] if validas else None) or 16384
    prompts = [x["prompt_eval_count"] for x in validas if x.get("prompt_eval_count")]
    criterios = {
        "C1_capacidad_y_contexto": "completion" in (sg.get("capabilities") or [])
                                   and (sg.get("context_length") or 0) >= 16384,
        "C2_acepta_las_opciones_del_SDK": bool(validas) and len(validas) == total - (1 if "think_false_rechazado" in resultados else 0),
        "C2b_acepta_think_false": "think_false_rechazado" not in resultados,
        "C3_sin_vencimientos": bool(validas) and all(x["segundos_reloj"] < resultados["timeout_chat_s"] for x in validas),
        "C4_formato_todas_validas": len(filas) == 6 and len(llamadas) == 6,
        "C4b_formato_al_menos_5_de_6": len(llamadas) >= 5,
        "C4c_herramienta_plausible": sum(f["plausible"] for f in filas) >= 5,
        "C5_sin_truncado": bool(prompts) and max(prompts) < num_ctx
                           and all(x.get("done_reason") == "stop" for x in validas),
    }
    if criterios["C1_capacidad_y_contexto"] and criterios["C2_acepta_las_opciones_del_SDK"] \
            and criterios["C3_sin_vencimientos"] and criterios["C4_formato_todas_validas"] \
            and criterios["C4c_herramienta_plausible"] and criterios["C5_sin_truncado"]:
        dictamen = "preparada"
    elif criterios["C1_capacidad_y_contexto"] and criterios["C4b_formato_al_menos_5_de_6"] \
            and criterios["C3_sin_vencimientos"] and criterios["C5_sin_truncado"]:
        dictamen = "preparada_con_riesgos"
    else:
        dictamen = "no_preparada"
    if not criterios["C2b_acepta_think_false"] and dictamen != "no_preparada":
        dictamen += " (requiere no enviar think: reasoning_effort=None, que es un cambio de configuración del agente)"
    informe = {
        "dictamen": dictamen, "criterios": criterios,
        "medidas": {
            "cuantizacion": (sg.get("details") or {}).get("quantization_level"),
            "parametros": (sg.get("details") or {}).get("parameter_size"),
            "capacidades": sg.get("capabilities"), "contexto_maximo": sg.get("context_length"),
            "vram_bytes": ps.get("size_vram"), "tamano_cargado_bytes": ps.get("size"),
            "fraccion_en_vram": ps.get("fraccion_en_vram"), "context_length_cargado": ps.get("context_length"),
            "carga_s": carga[0] if carga else None,
            "segundos_por_llamada_caliente": calientes,
            "tokens_prompt": prompts,
            "tokens_por_s_generacion": [x.get("tokens_por_s_generacion") for x in validas],
            "tokens_por_s_prompt": [x.get("tokens_por_s_prompt") for x in validas],
            "vuelta_a_qwen_carga_s": (resultados.get("vuelta_a_qwen") or {}).get("load_s"),
        },
        "respuestas": filas,
    }
    (salida / "dictamen.json").write_text(json.dumps(informe, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"dictamen": dictamen, "criterios": criterios}, ensure_ascii=False, indent=1))
    for f in filas:
        print(f"  paso {f['paso']} semilla {f['semilla']}: {f['tipo']} {f.get('herramienta', '')} "
              f"{json.dumps(f.get('argumentos', f.get('detalle', '')), ensure_ascii=False)[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
