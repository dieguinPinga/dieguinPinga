"""Rol REVISOR DE CÓDIGO (OFICINA.md, «Revisor»): PROGRAMADOR → REVISOR → PROGRAMADOR → EVALUADOR.

Un rol separado del programador, con su propio prompt, su propia llamada de inferencia (puede ser el mismo modelo
en otra sesión) y su propio registro. Interviene SOLO cuando el programador se estanca; examina el contrato y el
código candidato, y devuelve una revisión CAUSAL (quinta experiencia): UNA hipótesis principal anclada en citas
literales del código (el coordinador comprueba que existan: eso es un HECHO), la secuencia causal (condición
evaluada → estado que falta o es incorrecto → rama del contrato que no se alcanza), la PROPIEDAD del código que
tiene que cambiar, hechos separados de hipótesis y a lo sumo dos hipótesis secundarias. El programador lo recibe en
un bloque delimitado, separado del informe de pruebas.

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
MAX_HECHOS = 4
MAX_SECUNDARIAS = 2
MAX_PASOS = 6
MAX_CAMPO = 400
MAX_CITA = 200

# Revisión CAUSAL (quinta experiencia): una hipótesis principal, anclada en citas LITERALES del código candidato
# (que el coordinador comprueba mecánicamente), con la secuencia causal, la rama que no se alcanza y la PROPIEDAD del
# código que tiene que cambiar, sin escribir la solución. Hechos (verificables) separados de hipótesis.
SISTEMA_REVISOR = (
    "Sos el REVISOR DE CÓDIGO de un equipo. Otro programador escribió una función Python que no pasa todas las "
    "pruebas y lleva varios intentos sin mejorar. Buscá la CAUSA, no repitas el informe de pruebas.\n"
    "Método: (1) elegí UNA hipótesis principal, la más probable, fundada en una regla del contrato; (2) anclala en "
    "condiciones o líneas CONCRETAS del código, copiándolas LITERALMENTE (el coordinador comprueba que existan); "
    "(3) describí la secuencia causal paso a paso: qué condición se evalúa, qué estado falta o es incorrecto en ese "
    "momento, y qué rama del contrato resulta inaccesible o se toma en lugar de otra; (4) decí qué PROPIEDAD del "
    "código tiene que cambiar (por ejemplo, «el registro X tiene que incluir también Y» o «la condición A tiene que "
    "evaluarse antes que B»), sin escribir el código corregido.\n"
    "Separá HECHOS (lo que se ve en el código, con citas) de HIPÓTESIS. Como máximo dos hipótesis secundarias, "
    "solo si son importantes. NO escribas código corregido ni bloques de código; no apruebes nada.\n"
    "Respondé SOLO con un objeto JSON con esta forma:\n"
    '{"hipotesis_principal": {"regla_del_contrato": "<regla en juego>", "citas_del_codigo": ["<fragmento literal '
    'del código>"], "secuencia_causal": ["<paso 1>", "<paso 2>", "..."], "rama_inaccesible": "<qué rama o motivo del '
    'contrato no se alcanza o se reemplaza por otro>", "propiedad_a_cambiar": "<qué propiedad del código cambiar>", '
    '"confianza": "alta | media | baja"}, "hechos": ["<hecho observable en el código>"], '
    '"hipotesis_secundarias": [{"descripcion": "<...>", "propiedad_a_cambiar": "<...>"}]}')


# Experimento 6 (opcional): además de la hipótesis, un caso sintético mínimo para contrastarla en el sandbox.
SISTEMA_CASO = (
    "\nAdemás, para que la hipótesis se pueda comprobar, agregá al JSON un CASO SINTÉTICO MÍNIMO construido SOLO "
    "desde el contrato (no conocés las pruebas ocultas): una entrada pequeña donde, si tu hipótesis es correcta, el "
    "código del programador devuelve algo distinto de lo que pide el contrato. Formato:\n"
    '"caso_sintetico": {"args": [<argumentos posicionales en JSON>], "kwargs": {<argumentos por nombre>}, '
    '"que_deberia_pasar": "<qué pide el contrato para esa entrada, en palabras>"}\n'
    "El coordinador lo ejecuta con una implementación de referencia y con el código del programador: si dan "
    "distinto, la hipótesis queda confirmada; si dan igual, queda sin confirmar.")


# Experimento 7 (oficina/revision_existente.py): el MISMO rol revisa una solución existente por su cuenta, sin
# estancamiento previo ni resultados de pruebas: solo el contrato público y el código.
SISTEMA_REVISOR_AUTONOMO = (
    "Sos el REVISOR DE CÓDIGO de un equipo. Te dan el contrato de una función y una implementación existente. "
    "No hay resultados de pruebas: revisala vos, comparando cada regla del contrato con el código, y buscá un DEFECTO "
    "FUNCIONAL, es decir, una entrada válida según el contrato para la que el código devuelve algo distinto de lo "
    "que el contrato pide.\n"
    "Método: (1) elegí UNA hipótesis principal, la más probable, fundada en una regla del contrato; (2) anclala en "
    "condiciones o líneas CONCRETAS del código, copiándolas LITERALMENTE (el coordinador comprueba que existan); "
    "(3) describí la secuencia causal paso a paso: qué condición se evalúa, qué estado falta o es incorrecto en ese "
    "momento, y qué rama del contrato resulta inaccesible o se toma en lugar de otra; (4) decí qué PROPIEDAD del "
    "código tiene que cambiar, sin escribir el código corregido.\n"
    "Separá HECHOS (lo que se ve en el código, con citas) de HIPÓTESIS. Como máximo dos hipótesis secundarias. NO "
    "escribas código corregido ni bloques de código; no apruebes nada.\n"
    "Respondé SOLO con un objeto JSON con esta forma:\n"
    '{"hipotesis_principal": {"regla_del_contrato": "<regla en juego>", "citas_del_codigo": ["<fragmento literal '
    'del código>"], "secuencia_causal": ["<paso 1>", "<paso 2>", "..."], "rama_inaccesible": "<qué rama o motivo del '
    'contrato no se alcanza o se reemplaza por otro>", "propiedad_a_cambiar": "<qué propiedad del código cambiar>", '
    '"confianza": "alta | media | baja"}, "hechos": ["<hecho observable en el código>"], '
    '"hipotesis_secundarias": [{"descripcion": "<...>", "propiedad_a_cambiar": "<...>"}], '
    '"caso_sintetico": {"args": [<argumentos posicionales en JSON>], "kwargs": {<argumentos por nombre>}, '
    '"que_deberia_pasar": "<qué pide el contrato para esa entrada, en palabras>"}}\n'
    "El caso sintético es OBLIGATORIO: una entrada MÍNIMA, construida solo desde el contrato, donde según tu "
    "hipótesis el código devuelve algo distinto de lo que pide el contrato. El coordinador la ejecuta en un entorno "
    "aislado con una implementación de referencia y con este código. Si las salidas difieren, queda demostrado que "
    "el código no cumple el contrato en esa entrada (la causa sigue siendo tu hipótesis); si coinciden, tu hipótesis "
    "no tiene respaldo en ese caso.")


def mensajes_autonomos(descripcion: str, spec: dict, codigo: str, nota: str = "") -> list[dict]:
    """Pedido del experimento 7: contrato, firma, ejemplos visibles y el código numerado. Ningún resultado de pruebas."""
    numerado = "\n".join(f"{i:3d}| {ln}" for i, ln in enumerate((codigo or "").splitlines(), 1))
    usuario = (f"CONTRATO DE LA MISIÓN:\n{descripcion}\n\n{prompts._sig(spec)}\n\n"
               f"Ejemplos visibles:\n{prompts._examples(spec, 3000) or '(sin ejemplos visibles)'}\n\n"
               "CÓDIGO EXISTENTE A REVISAR (solucion.py; los números de línea NO son parte del código: al citar, copiá "
               f"solo el texto después de «| »):\n{numerado}\n\n"
               + (f"NOTA DEL COORDINADOR:\n{nota}\n\n" if nota else "")
               + "Revisalo y respondé con el JSON pedido, con el caso sintético.")
    return [{"role": "system", "content": SISTEMA_REVISOR_AUTONOMO}, {"role": "user", "content": usuario}]


@dataclass
class ConfigRevisor:
    modelo: ModelConfig
    max_revisiones: int = 2
    max_segundos: float = 1200.0       # tiempo total de revisión por misión
    max_caracteres: int = 2500         # lo que llega al programador
    hipotesis_verificables: bool = False   # experimento 6: pedir y contrastar un caso sintético
    contraste: object = None           # contraste.Contraste de la misión en curso (lo completa el Programador)


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


def mensajes(descripcion: str, spec: dict, codigo: str, feedback: str, con_caso: bool = False) -> list[dict]:
    numerado = "\n".join(f"{i:3d}| {ln}" for i, ln in enumerate((codigo or "").splitlines(), 1))
    usuario = (f"CONTRATO DE LA MISIÓN:\n{descripcion}\n\n{prompts._sig(spec)}\n\n"
               f"Ejemplos visibles:\n{prompts._examples(spec, 3000) or '(sin ejemplos visibles)'}\n\n"
               "CÓDIGO CANDIDATO (solucion.py; los números de línea NO son parte del código: al citar, copiá solo el "
               f"texto después de «| »):\n{numerado}\n\n"
               f"FEEDBACK AUTORIZADO DE LAS PRUEBAS (los casos ocultos no muestran sus datos):\n{feedback}\n\n"
               "Buscá la causa y respondé con el JSON pedido.")
    return [{"role": "system", "content": SISTEMA_REVISOR + (SISTEMA_CASO if con_caso else "")},
            {"role": "user", "content": usuario}]


_BLOQUE = re.compile(r"```.*?(```|$)", re.S)


def _limpio(v, limite=MAX_CAMPO) -> str:
    return _BLOQUE.sub("[código omitido: el revisor no escribe la solución]", str(v or "")).strip()[:limite]


def _norm(t: str) -> str:
    return " ".join(t.split())


def cita_en_codigo(cita: str, codigo: str) -> bool:
    """Una cita es un HECHO si aparece literalmente en el código (ignorando espacios repetidos)."""
    c = _norm(cita)
    return bool(c) and c in _norm(codigo)


def interpretar(texto: str, codigo: str = "") -> dict:
    """Valida y SANEA la respuesta causal: descarta bloques de código, recorta campos y comprueba las citas contra el
    código candidato. ValueError si es inválida."""
    t = (texto or "").strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError("la respuesta no contiene un objeto JSON")
    try:
        d = json.loads(m.group(0))
    except ValueError as e:
        raise ValueError(f"JSON ilegible: {e}") from None
    hp = d.get("hipotesis_principal") if isinstance(d, dict) else None
    if not isinstance(hp, dict):
        raise ValueError("falta «hipotesis_principal»")
    pasos = [_limpio(x) for x in (hp.get("secuencia_causal") or []) if isinstance(x, str) and x.strip()][:MAX_PASOS]
    propiedad = _limpio(hp.get("propiedad_a_cambiar"))
    if len(pasos) < 2:
        raise ValueError("la secuencia causal necesita al menos 2 pasos")
    if not propiedad:
        raise ValueError("falta «propiedad_a_cambiar»")
    citas = []
    for c in (hp.get("citas_del_codigo") or [])[:3]:
        if not isinstance(c, str) or c.strip().count("\n") > 1 or "```" in c:
            continue                       # una cita es un fragmento corto (1 o 2 líneas), nunca un programa
        c = re.sub(r"^\s*\d+\|", "", c.strip()).strip()[:MAX_CITA]
        if c:
            citas.append({"texto": c, "verificada": cita_en_codigo(c, codigo)})
    confianza = str(hp.get("confianza") or "").strip().lower()
    principal = {"regla_del_contrato": _limpio(hp.get("regla_del_contrato")), "citas": citas,
                 "secuencia_causal": pasos, "rama_inaccesible": _limpio(hp.get("rama_inaccesible")),
                 "propiedad_a_cambiar": propiedad, "confianza": confianza if confianza in ("alta", "media", "baja")
                 else "sin indicar"}
    hechos = [_limpio(x) for x in (d.get("hechos") or []) if isinstance(x, str) and x.strip()][:MAX_HECHOS]
    secundarias = [{"descripcion": _limpio(x.get("descripcion")), "propiedad_a_cambiar": _limpio(x.get("propiedad_a_cambiar"))}
                   for x in (d.get("hipotesis_secundarias") or [])[:MAX_SECUNDARIAS]
                   if isinstance(x, dict) and _limpio(x.get("descripcion"))]
    return {"principal": principal, "hechos": hechos, "secundarias": secundarias,
            "citas_verificadas": sum(c["verificada"] for c in citas), "citas_total": len(citas),
            "caso_sintetico": d.get("caso_sintetico")}     # sin interpretar: lo valida el coordinador


INICIO = "════ DIAGNÓSTICO DEL REVISOR — rol distinto; NO es un resultado de pruebas ════"
FIN = "════ FIN DEL DIAGNÓSTICO DEL REVISOR ════"


ESTADOS_HIPOTESIS = {      # clave: la discrepancia del caso sintético (oficina/contraste.py)
    "demostrada": "el caso sintético DEMOSTRÓ que tu código no cumple el contrato en esa entrada (ver la evidencia). "
                  "La explicación causal de abajo es compatible con eso, pero NO está demostrada",
    "no_observada": "el caso sintético NO mostró ninguna discrepancia: en esa entrada tu código ya cumple el contrato. "
                    "La hipótesis no tiene respaldo ahí y la causa puede ser otra",
    "inconclusa": "SIN CONTRASTAR (el caso sintético no se pudo usar); tomala solo como hipótesis",
}


def texto_para_programador(diag: dict, limite: int, estado: str | None = None) -> str:
    p = diag["principal"]
    l = [INICIO,
         f"HIPÓTESIS PRINCIPAL (confianza {p['confianza']}) — regla del contrato: {p['regla_del_contrato'] or '—'}"]
    if estado in ESTADOS_HIPOTESIS:
        l.append(f"ESTADO DE LA HIPÓTESIS: {ESTADOS_HIPOTESIS[estado]}")
    verif = [c["texto"] for c in p["citas"] if c["verificada"]]
    no_verif = [c["texto"] for c in p["citas"] if not c["verificada"]]
    if verif:
        l.append("HECHO — está en tu código (citas comprobadas por el coordinador):")
        l += [f"    «{c}»" for c in verif]
    if no_verif:
        l.append("Citas del revisor que NO aparecen literalmente en tu código (tomalas como hipótesis): "
                 + "; ".join(f"«{c}»" for c in no_verif))
    l.append("SECUENCIA CAUSAL (hipótesis del revisor):")
    l += [f"    {i}. {paso}" for i, paso in enumerate(p["secuencia_causal"], 1)]
    if p["rama_inaccesible"]:
        l.append(f"RAMA QUE NO SE ALCANZA: {p['rama_inaccesible']}")
    l.append(f"PROPIEDAD DEL CÓDIGO QUE TIENE QUE CAMBIAR: {p['propiedad_a_cambiar']}")
    if diag["hechos"]:
        l.append("Otros hechos señalados por el revisor: " + " · ".join(diag["hechos"]))
    if diag["secundarias"]:
        l.append("Hipótesis secundarias (menos prioritarias): " + " · ".join(
            f"{s['descripcion']}" + (f" (cambiar: {s['propiedad_a_cambiar']})" if s["propiedad_a_cambiar"] else "")
            for s in diag["secundarias"]))
    l.append("QUÉ HACER: partí de tu versión anterior, ubicá las líneas citadas y cambiá esa propiedad conservando lo "
             "que ya funciona. Si no estás de acuerdo con el diagnóstico, decilo en una línea fuera del bloque de "
             "código. Las pruebas deciden si está bien.")
    cuerpo = prompts.clip("\n".join(l), max(500, limite - len(FIN) - 1))
    return cuerpo + "\n" + FIN


def revisar(ollama, gate, cfg: ConfigRevisor, descripcion: str, spec: dict, codigo: str, feedback: str,
            cancel, msgs: list[dict] | None = None) -> dict:
    """Una llamada al revisor. Nunca lanza por fallas del revisor: devuelve {'ok': False, 'motivo': …}.
    msgs: un pedido ya armado (experimento 7, mensajes_autonomos); por omisión, el de siempre."""
    msgs = msgs or mensajes(descripcion, spec, codigo, feedback, con_caso=cfg.hipotesis_verificables)
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
               tokens_generados=gen.eval_tokens, cortada=bool(gen.truncated), fin=gen.done_reason)
    try:
        reg["diagnostico"] = interpretar(gen.text, codigo)
        reg["ok"] = True
    except ValueError as e:
        reg["motivo"] = f"respuesta inválida del revisor: {e}"
    return reg
