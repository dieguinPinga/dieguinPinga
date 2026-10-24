"""Experimento 7.8: diagnóstico causal con PRESERVACIÓN DE COMPORTAMIENTO (OFICINA.md, «Experimento 7.8»).

En el 7.7 Gemma identificó la causa, pero Qwen invirtió la condición (`!=` → `==`) tres veces: el parche registraba
las anulaciones y dejaba de registrar los ingresos y egresos. El 7.8 es el MISMO recorrido del 7.7
(reparacion_coordinada.correr, con sus «ajustes») con tres diferencias, todas generadas o medidas, nunca escritas:
1. la diagnosticadora también describe el COMPORTAMIENTO DEFECTUOSO y el COMPORTAMIENTO A PRESERVAR (lo que hoy
   funciona y podría romperse al corregir); los dos campos salen de su respuesta y se validan;
2. el programador recibe esos campos completos y se le pide que, en «justificacion», explique cómo su parche corrige
   el defecto sin romper cada comportamiento a preservar (sin decirle qué condición cambiar ni cómo);
3. si un parche se aplica y la discrepancia persiste, el intento siguiente recibe SOLO eso: que persiste y la
   diferencia observable del caso sintético (entrada, lo que pide el contrato, lo que devolvió su parche y qué falta
   o sobra); además, si rompió un ejemplo visible del contrato (público), ese informe.
Mismos límites (2 diagnósticos, 3 parches, 2400 s), sandbox, normalización de la sangría, auditoría y batería.
"""
from __future__ import annotations

import json
import re

from . import reparacion_coordinada as RC
from . import reparacion_guiada as RGD
from . import revision_existente as RE
from . import revisor as RV

VERSION_8 = ("experimento 7.8: diagnóstico causal con preservación de comportamiento (gemma) y reparación (qwen) "
             "coordinados, con normalización de sangría")
CLASIFICACION_8 = "diagnóstico y reparación automáticos coordinados, con preservación de comportamiento"
MAX_PRESERVAR = 6

SISTEMA_DIAGNOSTICO_8 = (
    "Sos la DIAGNOSTICADORA de un equipo. Un código no cumple su contrato en un caso concreto que el coordinador "
    "ejecutó en un entorno aislado. Tu tarea es encontrar la CAUSA en el código, no corregirlo.\n"
    "Seguí la entrada del caso paso a paso por las líneas que se te muestran y explicá qué condición o qué estado "
    "produce la salida incorrecta. Además, pensá en quien va a corregirlo: describí el COMPORTAMIENTO DEFECTUOSO "
    "(qué hace mal el código y en qué situación) y el COMPORTAMIENTO A PRESERVAR (qué hace bien hoy, según el "
    "contrato, y podría romperse al corregir la causa).\n"
    "Respondé SOLO con un objeto JSON con esta forma:\n"
    '{"causa": "<la causa concreta, en una o dos oraciones>", "lineas_citadas": ["<línea copiada LITERALMENTE del '
    'código, sin el número>"], "mecanismo": ["<paso 1>", "<paso 2>", "..."], "relacion_con_la_discrepancia": "<por '
    'qué esa causa produce exactamente la diferencia observada>", "comportamiento_defectuoso": "<qué hace mal el '
    'código y cuándo>", "comportamiento_a_preservar": ["<comportamiento correcto que la corrección no debe romper>"]}\n'
    "No escribas código corregido, ni bloques de código, ni el programa.")

PEDIDO_8 = ("Proponé el parche mínimo que corrige esa causa y respondé solo con el JSON pedido. En «justificacion» "
            "explicá cómo tu parche corrige el comportamiento defectuoso y por qué no rompe cada uno de los "
            "comportamientos a preservar. No escribas el programa completo.")


def mensajes_diagnostico(f: dict, disc: str, nota: str = "") -> list[dict]:
    msgs = RC.mensajes_diagnostico(f, disc, nota)
    return [{"role": "system", "content": SISTEMA_DIAGNOSTICO_8}, msgs[1]]


def interpretar(texto: str, codigo: str, zonas, funcion: str) -> dict:
    """La validación del 7.7 más los dos campos nuevos (ValueError si faltan o están vacíos)."""
    d = RC.interpretar_diagnostico(texto, codigo, zonas, funcion)
    crudo = json.loads(re.search(r"\{.*\}", texto, re.S).group(0))
    defectuoso = crudo.get("comportamiento_defectuoso")
    defectuoso = RV._limpio(defectuoso, RC.MAX_CAMPO) if isinstance(defectuoso, str) else ""
    preservar = crudo.get("comportamiento_a_preservar")
    preservar = [preservar] if isinstance(preservar, str) else preservar if isinstance(preservar, list) else []
    preservar = [RV._limpio(x, RC.MAX_CAMPO) for x in preservar if isinstance(x, str) and x.strip()][:MAX_PRESERVAR]
    if not defectuoso:
        raise ValueError("falta «comportamiento_defectuoso»")
    if not preservar:
        raise ValueError("falta «comportamiento_a_preservar» (al menos un comportamiento que la corrección no debe "
                         "romper)")
    return dict(d, comportamiento_defectuoso=defectuoso, comportamiento_a_preservar=preservar)


def texto_para_el_programador(d: dict) -> str:
    """El texto del 7.7 más los dos campos, completos (tal como quedaron después de validar)."""
    return (RC.texto_para_el_programador(d)
            + f"\nComportamiento defectuoso: {d['comportamiento_defectuoso']}"
            + "\nComportamiento a preservar (la corrección no debe romperlo):\n"
            + "\n".join(f"    - {x}" for x in d["comportamiento_a_preservar"]))


def retroalimentacion(n, caso, funcion, esperado, cand, disc, visibles, normalizado) -> str:
    """Solo la diferencia observable del caso sintético (y, si los rompió, el informe de los ejemplos visibles)."""
    salida = cand.get("salida") if cand.get("ejecucion") == "completa" else None
    l = [f"Tu parche del intento {n} se aplicó" + (" (el coordinador ajustó solo su sangría)" if normalizado else "")
         + ("." if disc == "eliminada" else ", pero la DISCREPANCIA PERSISTE en el caso sintético.")
         + " Tu próximo parche se aplica de nuevo sobre el código de arriba (el original).",
         f"Caso: {funcion}({RE._llamada(caso)})",
         f"Lo que pide el contrato: {RE._json(esperado)}"]
    if salida is not None:
        l.append(f"Lo que devuelve el código con tu parche: {RE._json(salida)}")
        comp = RE.comparar_salidas(esperado, salida)
        if comp["comparable"] and comp["distintas"]:
            l.append("Claves distintas: " + ", ".join(f"'{k}'" for k in comp["distintas"]))
            for k in comp["distintas"]:
                dd = comp["detalle"].get(k) or {}
                if "faltan" in dd:
                    l.append(f"  - '{k}': falta {RE._json(dd['faltan'], 300)} · sobra {RE._json(dd['sobran'], 300)}")
    else:
        l.append(f"Con tu parche el caso no devolvió una salida comparable: {cand.get('ejecucion')}")
    if not visibles["ok"]:
        l.append("Además no cumple los ejemplos visibles del contrato:\n" + visibles["informe"])
    return "\n".join(l)


AJUSTES = {"version": VERSION_8, "clasificacion": CLASIFICACION_8, "mensajes_diagnostico": mensajes_diagnostico,
           "interpretar": interpretar, "texto": texto_para_el_programador, "pedido": PEDIDO_8,
           "retroalimentacion": retroalimentacion}


def carpeta(carpeta_lab, corrida):
    try:
        return RGD.carpeta_de_reparacion(carpeta_lab, corrida, "experimento-7-8")
    except RGD.ErrorReparacion as e:
        raise RC.ErrorCoordinada(str(e)) from None


def correr(destino, f, executor, cliente, gate, diagnosticador, programador, max_segundos: float = 2400.0,
           cancel=None) -> dict:
    inf = RC.correr(destino, f, executor, cliente, gate, diagnosticador, programador, max_segundos, cancel,
                    ajustes=AJUSTES)
    return inf
