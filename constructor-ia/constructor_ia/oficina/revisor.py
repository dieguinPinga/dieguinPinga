"""Rol REVISOR DE CÓDIGO (OFICINA.md, «Revisor»): PROGRAMADOR → REVISOR → PROGRAMADOR → EVALUADOR.

Un rol separado del programador, con su propio prompt, su propia llamada de inferencia (puede ser el mismo modelo
en otra sesión) y su propio registro. Interviene SOLO cuando el programador se estanca; examina el contrato y el
código candidato, y devuelve un diagnóstico estructurado que se agrega al siguiente pedido del programador.

Qué recibe (solo información autorizada, la misma que ya puede ver el programador):
- el contrato completo de la misión, la firma y los ejemplos VISIBLES;
- el código candidato (el punto de partida del próximo intento: la mejor versión conservada o la última);
- el feedback autorizado de ese candidato: informe de pruebas, pistas seguras y avisos del coordinador.
Nunca recibe argumentos, valores esperados ni salidas de los casos ocultos.

Qué NO puede hacer: escribir la solución (los bloques de código de su respuesta se descartan), aprobar nada ni
reemplazar el código. Sus observaciones son texto para el programador, que decide; la aprobación sigue siendo de las
pruebas en el sandbox y de la verificación central independiente.

Cuándo interviene (criterio general y comprobable, igual para cualquier misión): antes del intento N (N > 1) si
- hay al menos VENTANA intentos ejecutados y el último no superó al mejor de los anteriores (sin mejora), y
- al menos un mismo caso (visible u oculto) falló en cada uno de los últimos VENTANA intentos;
y además quedan revisiones (max_revisiones), tiempo de revisión (max_segundos) y el pedido entra en el contexto.

Si el revisor falla, no responde, devuelve algo inválido o se pasa de límites: se registra el motivo y el
programador sigue con el feedback normal; la mejor versión no se toca.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from .. import prompts
from ..config import ModelConfig
from ..ollama import Cancelled, OllamaError

VENTANA = 2
MAX_OBSERVACIONES = 6
MAX_CAMPO = 400
CAMPOS = ("regla_del_contrato", "problema", "donde", "por_que", "sugerencia")

SISTEMA_REVISOR = (
    "Sos el REVISOR DE CÓDIGO de un equipo. Otro programador escribió una función Python que no pasa todas las "
    "pruebas, y lleva varios intentos sin mejorar. Tu trabajo es leer críticamente el CONTRATO y el CÓDIGO y "
    "encontrar por qué el código no cumple el contrato. No repitas el informe de pruebas: buscá contradicciones "
    "entre el código y el contrato, ramas que nunca se ejecutan, estados que no se registran o se registran tarde, "
    "chequeos en un orden distinto del pedido y casos que el código confunde entre sí.\n"
    "Reglas: NO escribas el código corregido ni bloques de código; describí qué lógica revisar y por qué. No "
    "apruebes nada: las pruebas deciden. Respondé SOLO con un objeto JSON con esta forma:\n"
    '{"resumen": "<una frase>", "observaciones": [{"regla_del_contrato": "<qué regla del contrato está en juego>", '
    '"problema": "<qué hace mal el código>", "donde": "<función, variable o rama>", "por_que": "<por qué viola el '
    'contrato>", "sugerencia": "<qué cambiar, en palabras>"}]}\n'
    f"Entre 1 y {MAX_OBSERVACIONES} observaciones, las más importantes primero.")


@dataclass
class ConfigRevisor:
    modelo: ModelConfig
    max_revisiones: int = 2
    max_segundos: float = 1200.0       # tiempo total de revisión por misión
    max_caracteres: int = 2500         # lo que llega al programador


def debe_revisar(historial: list[dict], ventana: int = VENTANA) -> tuple[bool, str]:
    """(sí/no, motivo). historial: intentos ejecutados con 'n', 'puntaje' y 'fallidos_todos' (n de casos que fallaron)."""
    if len(historial) < ventana:
        return False, f"menos de {ventana} intentos ejecutados"
    ultimos = historial[-ventana:]
    mejor_antes = max(h["puntaje"] for h in historial[:-1])
    if historial[-1]["puntaje"] > mejor_antes:
        return False, f"el intento {historial[-1]['n']} mejoró ({historial[-1]['puntaje']} > {mejor_antes})"
    comunes = set(ultimos[0]["fallidos_todos"])
    for h in ultimos[1:]:
        comunes &= set(h["fallidos_todos"])
    if not comunes:
        return False, "ningún caso falló en todos los últimos intentos"
    return True, (f"estancamiento: el intento {historial[-1]['n']} no superó al mejor anterior ({mejor_antes}) y los "
                  f"casos {', '.join(map(str, sorted(comunes)))} fallaron en los intentos "
                  f"{', '.join(str(h['n']) for h in ultimos)}")


def mensajes(descripcion: str, spec: dict, codigo: str, feedback: str) -> list[dict]:
    usuario = (f"CONTRATO DE LA MISIÓN:\n{descripcion}\n\n{prompts._sig(spec)}\n\n"
               f"Ejemplos visibles:\n{prompts._examples(spec, 3000) or '(sin ejemplos visibles)'}\n\n"
               f"CÓDIGO CANDIDATO (solucion.py):\n```python\n{codigo}```\n\n"
               f"FEEDBACK AUTORIZADO DE LAS PRUEBAS (los casos ocultos no muestran sus datos):\n{feedback}\n\n"
               "Revisá el código contra el contrato y respondé con el JSON pedido.")
    return [{"role": "system", "content": SISTEMA_REVISOR}, {"role": "user", "content": usuario}]


_BLOQUE = re.compile(r"```.*?(```|$)", re.S)


def interpretar(texto: str) -> dict:
    """Valida y SANEA la respuesta: descarta bloques de código y recorta campos. ValueError si es inválida."""
    t = (texto or "").strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError("la respuesta no contiene un objeto JSON")
    try:
        d = json.loads(m.group(0))
    except ValueError as e:
        raise ValueError(f"JSON ilegible: {e}") from None
    obs = d.get("observaciones") if isinstance(d, dict) else None
    if not isinstance(obs, list) or not obs:
        raise ValueError("falta la lista «observaciones»")
    limpias = []
    for o in obs[:MAX_OBSERVACIONES]:
        if not isinstance(o, dict):
            continue
        c = {k: _BLOQUE.sub("[código omitido: el revisor no escribe la solución]", str(o.get(k) or "")).strip()[:MAX_CAMPO]
             for k in CAMPOS}
        if c["problema"] and (c["sugerencia"] or c["por_que"]):
            limpias.append(c)
    if not limpias:
        raise ValueError("ninguna observación tiene «problema» y «sugerencia» o «por_que»")
    resumen = _BLOQUE.sub("", str(d.get("resumen") or "")).strip()[:MAX_CAMPO]
    return {"resumen": resumen, "observaciones": limpias}


def texto_para_programador(diag: dict, limite: int) -> str:
    lineas = ["OBSERVACIONES DEL REVISOR DE CÓDIGO (otro rol leyó tu código contra el contrato; puede equivocarse: "
              "vos decidís qué cambiar, y las pruebas deciden si está bien):"]
    if diag["resumen"]:
        lineas.append(f"Resumen: {diag['resumen']}")
    for i, o in enumerate(diag["observaciones"], 1):
        lineas.append(f"{i}. [{o['regla_del_contrato'] or 'contrato'}] {o['problema']}"
                      + (f" — dónde: {o['donde']}" if o["donde"] else "")
                      + (f" — por qué: {o['por_que']}" if o["por_que"] else "")
                      + (f" — revisar: {o['sugerencia']}" if o["sugerencia"] else ""))
    return prompts.clip("\n".join(lineas), limite)


def revisar(ollama, gate, cfg: ConfigRevisor, descripcion: str, spec: dict, codigo: str, feedback: str,
            cancel) -> dict:
    """Una llamada al revisor. Nunca lanza por fallas del revisor: devuelve {'ok': False, 'motivo': …}."""
    msgs = mensajes(descripcion, spec, codigo, feedback)
    t0 = time.monotonic()
    reg = {"modelo": cfg.modelo.name, "mensajes": msgs, "ok": False}
    try:
        gate.acquire("revisor", cancel)
        try:
            gen = ollama.chat(cfg.modelo, msgs, cancel, fmt="json")
        finally:
            gate.release()
    except Cancelled:
        raise
    except (OllamaError, OSError) as e:
        reg.update(motivo=f"el revisor no respondió: {e}", segundos=round(time.monotonic() - t0, 1))
        return reg
    reg.update(respuesta=gen.text, segundos=round(time.monotonic() - t0, 1), tokens_prompt=gen.prompt_tokens,
               tokens_generados=gen.eval_tokens, cortada=bool(gen.truncated))
    try:
        reg["diagnostico"] = interpretar(gen.text)
        reg["ok"] = True
    except ValueError as e:
        reg["motivo"] = f"respuesta inválida del revisor: {e}"
    return reg
