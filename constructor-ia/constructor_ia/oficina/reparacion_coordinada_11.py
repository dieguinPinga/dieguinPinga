"""Experimento 7.11: localización causal y revisión NO BLOQUEANTE (OFICINA.md, «Experimento 7.11»).

En el 7.10 Gemma diagnosticó bien, Qwen produjo seis planes, Gemma rechazó los seis y no se llegó a ningún parche.
El 7.11 (orquestador propio; reutiliza piezas del 7.6–7.10 sin modificarlas) cambia tres cosas:
1. LOCALIZACIÓN CAUSAL: la diagnosticadora (gemma) separa el ORIGEN del defecto (la operación o condición que
   introduce el estado incorrecto), el PUNTO DE MANIFESTACIÓN (donde ese estado produce el resultado observable), la
   CADENA CAUSAL entre los dos y los COMPORTAMIENTOS A PRESERVAR. Origen y manifestación son líneas del código,
   comprobadas literalmente dentro de las zonas, y distintas.
2. PLAN CONCRETO (qwen): qué estado corregir, dónde intervenir (líneas del código) y con qué acción, por qué eso elimina
   la causa en lugar de ocultar el síntoma, y los invariantes a preservar. Un plan sin intervención concreta (solo
   descriptivo) se rechaza sin gastar la revisión. Un plan repetido (misma intervención, aunque cambie la redacción)
   tampoco se revisa.
3. REVISIÓN NO BLOQUEANTE (gemma): aprueba o rechaza con crítica causal; la crítica vuelve íntegra al planificador.
   Si se agotan los planes del ciclo sin aprobación, el coordinador implementa el ÚLTIMO plan válido BAJO EVALUACIÓN
   EXPERIMENTAL (registrado así), solo para comprobarlo en el sandbox. Nunca saltea nada: validación de parches, zonas,
   normalización de la sangría, copia, bwrap/seccomp, caso sintético, ejemplos visibles, batería y límites.
El criterio principal es el comportamiento comprobado: ni la aprobación de gemma es éxito ni su rechazo es fracaso.
Límites: MAX_CICLOS ciclos, MAX_PLANES_POR_CICLO planes por ciclo (MAX_PLANES en total), MAX_PARCHES parches y 2400 s.

Medición: lo DEMOSTRADO por ejecución (parche aplicado, caso sintético, ejemplos visibles, batería, coherencia del
parche con el plan) separado de las evaluaciones POSTERIORES DETERMINISTAS aproximadas (origen = líneas que difieren
entre el código defectuoso y la referencia de la misión; manifestación = la zona donde se emite el valor equivocado
del caso sintético). Esas referencias se calculan DESPUÉS y no llegan a ningún modelo.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
import threading
import time
import unicodedata

from .. import prompts
from . import contraste as CT
from . import parches as PA
from . import reparacion_coordinada as RC
from . import reparacion_coordinada_8 as RC8
from . import reparacion_coordinada_9 as RC9
from . import reparacion_coordinada_10 as RC10
from . import reparacion_guiada as RGD
from . import revision_existente as RE
from . import revisor as RV
from .autocorreccion import sin_comentarios
from .misiones_programacion import MISIONES, VALIDADORES

VERSION_11 = ("experimento 7.11: localización causal (gemma), plan concreto (qwen), revisión no bloqueante (gemma) y "
              "parche (qwen) coordinados, con normalización de sangría")
CLASIFICACION_11 = "reparación coordinada con localización causal, comprobada por ejecución"
MAX_CICLOS = 3
MAX_PLANES_POR_CICLO = 2
MAX_PLANES = MAX_CICLOS * MAX_PLANES_POR_CICLO
MAX_PARCHES = MAX_CICLOS
MAX_CAMPO = 600
MIN_ACCION = 12

# Diagnóstico con localización causal -------------------------------------------------------------------------------

SISTEMA_DIAGNOSTICO_11 = (
    "Sos la DIAGNOSTICADORA de un equipo. Un código no cumple su contrato en un caso concreto que el coordinador "
    "ejecutó en un entorno aislado. Tu tarea es LOCALIZAR la causa, no corregirla.\n"
    "Distinguí dos lugares del código: el ORIGEN del defecto (la operación o condición que introduce el estado "
    "incorrecto) y el PUNTO DE MANIFESTACIÓN (la operación donde ese estado produce el resultado observable). Suelen "
    "ser líneas distintas. Explicá la CADENA CAUSAL de uno a otro y los comportamientos que el contrato exige y que "
    "una corrección no debe romper.\n"
    "Respondé SOLO con un objeto JSON con esta forma:\n"
    '{"causa": "<la causa en una o dos oraciones>", "origen_del_defecto": {"linea": "<línea copiada LITERALMENTE del '
    'código>", "descripcion": "<qué estado incorrecto introduce>"}, "punto_de_manifestacion": {"linea": "<línea copiada '
    'LITERALMENTE del código>", "descripcion": "<cómo produce el resultado observable>"}, "cadena_causal": ["<paso 1>", '
    '"<paso 2>", "..."], "comportamiento_a_preservar": ["<invariante del contrato>"]}\n'
    "No escribas código corregido, ni bloques de código, ni el programa.")


def _linea_en_zona(texto, codigo: str, zonas) -> dict:
    t = re.sub(r"^\s*\d+\|", "", str(texto or "").strip()).strip()[:RV.MAX_CITA]
    ls = PA.lineas_de_cita(codigo, t) if t else []
    return {"texto": t, "lineas": ls, "en_zona": bool(ls) and all(any(a <= n <= b for a, b in zonas) for n in ls)}


def _json(texto: str, funcion: str) -> dict:
    t = (texto or "").strip()
    if re.search(rf"\bdef\s+{re.escape(funcion)}\s*\(", t):
        raise ValueError("incluyó el programa: no se pide código")
    mt = re.search(r"\{.*\}", t, re.S)
    if not mt:
        raise ValueError("la respuesta no contiene un objeto JSON")
    try:
        d = json.loads(mt.group(0))
    except ValueError as e:
        raise ValueError(f"JSON ilegible: {e}") from None
    if not isinstance(d, dict):
        raise ValueError("la respuesta no es un objeto JSON")
    return d


def _texto(d, k) -> str:
    return RV._limpio(d.get(k), MAX_CAMPO) if isinstance(d.get(k), str) else ""


def _lista(d, k, n=8) -> list[str]:
    v = d.get(k)
    v = [v] if isinstance(v, str) else v if isinstance(v, list) else []
    return [RV._limpio(x, MAX_CAMPO) for x in v if isinstance(x, str) and x.strip()][:n]


def interpretar_diagnostico(texto: str, codigo: str, zonas, funcion: str) -> dict:
    d = _json(texto, funcion)
    out = {"causa": _texto(d, "causa"), "cadena_causal": _lista(d, "cadena_causal"),
           "comportamiento_a_preservar": _lista(d, "comportamiento_a_preservar", 6)}
    if not out["causa"]:
        raise ValueError("falta «causa»")
    for k in ("origen_del_defecto", "punto_de_manifestacion"):
        v = d.get(k) if isinstance(d.get(k), dict) else {}
        loc = _linea_en_zona(v.get("linea"), codigo, zonas)
        loc["descripcion"] = _texto(v, "descripcion")
        if not loc["en_zona"]:
            raise ValueError(f"«{k}.linea» tiene que ser una línea que exista literalmente dentro de las zonas")
        if not loc["descripcion"]:
            raise ValueError(f"falta «{k}.descripcion»")
        out[k] = loc
    if set(out["origen_del_defecto"]["lineas"]) == set(out["punto_de_manifestacion"]["lineas"]):
        raise ValueError("el origen y el punto de manifestación son la misma línea: separalos (dónde se introduce el "
                         "estado incorrecto y dónde produce el resultado observable)")
    if len(out["cadena_causal"]) < 2:
        raise ValueError("«cadena_causal» necesita al menos 2 pasos")
    if not out["comportamiento_a_preservar"]:
        raise ValueError("falta «comportamiento_a_preservar»")
    return out


def texto_diagnostico(d: dict) -> str:
    o, m = d["origen_del_defecto"], d["punto_de_manifestacion"]
    return "\n".join([f"Causa: {d['causa']}",
                      f"Origen del defecto: «{o['texto']}» (línea {', '.join(map(str, o['lineas']))}): {o['descripcion']}",
                      f"Punto de manifestación: «{m['texto']}» (línea {', '.join(map(str, m['lineas']))}): "
                      f"{m['descripcion']}", "Cadena causal:"]
                     + [f"    {i}. {p}" for i, p in enumerate(d["cadena_causal"], 1)]
                     + ["Comportamiento a preservar:"] + [f"    - {x}" for x in d["comportamiento_a_preservar"]])


def mensajes_diagnostico(f: dict, disc: str, nota: str = "") -> list[dict]:
    return [{"role": "system", "content": SISTEMA_DIAGNOSTICO_11}, RC.mensajes_diagnostico(f, disc, nota)[1]]


# Plan concreto ------------------------------------------------------------------------------------------------------

SISTEMA_PLANIFICADOR_11 = (
    "Sos el PLANIFICADOR de un equipo de programación. Antes de que nadie escriba código, hacé un PLAN CONCRETO de la "
    "corrección a partir del diagnóstico: qué estado del programa hay que corregir, DÓNDE intervenir (líneas del "
    "código) y CON QUÉ ACCIÓN, por qué esa intervención elimina la causa en lugar de ocultar el síntoma, y qué "
    "invariantes del contrato se preservan. No escribas código ni el parche.\n"
    "Respondé SOLO con un objeto JSON con esta forma:\n"
    '{"estado_a_corregir": "<qué estado es incorrecto y cuál tendría que ser>", "intervenciones": [{"linea": "<línea '
    'copiada LITERALMENTE del código>", "accion": "<qué se cambia en esa línea, en palabras>"}], '
    '"por_que_elimina_la_causa": "<por qué esto corrige el origen y no solo el síntoma>", '
    '"invariantes_preservados": ["<invariante del contrato que se mantiene>"], "cambio_respecto_del_anterior": "<si '
    'hubo un intento anterior: qué cambia en este plan y por qué>"}')


def interpretar_plan(texto: str, codigo: str, zonas, funcion: str, hubo_anterior: bool) -> dict:
    d = _json(texto, funcion)
    out = {"estado_a_corregir": _texto(d, "estado_a_corregir"),
           "por_que_elimina_la_causa": _texto(d, "por_que_elimina_la_causa"),
           "invariantes_preservados": _lista(d, "invariantes_preservados", 6),
           "cambio_respecto_del_anterior": _texto(d, "cambio_respecto_del_anterior")}
    if not out["estado_a_corregir"]:
        raise ValueError("falta «estado_a_corregir»")
    intervenciones = []
    for x in (d.get("intervenciones") or [])[:4]:
        if isinstance(x, dict):
            loc = _linea_en_zona(x.get("linea"), codigo, zonas)
            loc["accion"] = RV._limpio(x.get("accion"), MAX_CAMPO) if isinstance(x.get("accion"), str) else ""
            intervenciones.append(loc)
    concretas = [x for x in intervenciones if x["en_zona"] and len(x["accion"]) >= MIN_ACCION]
    if not concretas:
        raise ValueError("plan descriptivo: falta una intervención concreta (una línea que exista dentro de las zonas "
                         "y la acción que se le aplica)")
    if not out["por_que_elimina_la_causa"]:
        raise ValueError("falta «por_que_elimina_la_causa»")
    if not out["invariantes_preservados"]:
        raise ValueError("falta «invariantes_preservados»")
    if hubo_anterior and not out["cambio_respecto_del_anterior"]:
        raise ValueError("falta «cambio_respecto_del_anterior» (hubo un intento anterior)")
    out["intervenciones"] = intervenciones
    return out


def texto_plan(p: dict) -> str:
    l = [f"Estado a corregir: {p['estado_a_corregir']}", "Intervenciones:"]
    l += [f"    - «{x['texto']}» (línea {', '.join(map(str, x['lineas']))}): {x['accion']}" if x["en_zona"] else
          f"    - «{x['texto']}» (no está en las zonas: no se puede modificar): {x['accion']}"
          for x in p["intervenciones"]]
    l.append(f"Por qué elimina la causa: {p['por_que_elimina_la_causa']}")
    l.append("Invariantes preservados:")
    l += [f"    - {x}" for x in p["invariantes_preservados"]]
    if p["cambio_respecto_del_anterior"]:
        l.append(f"Cambio respecto del intento anterior: {p['cambio_respecto_del_anterior']}")
    return "\n".join(l)


_VACIAS = {"el", "la", "los", "las", "un", "una", "de", "del", "en", "y", "o", "que", "se", "a", "al", "por", "para",
           "con", "su", "sus", "lo", "es", "linea", "esta", "este", "esa", "ese"}


RAIZ = 5          # «quitar», «quitando» y «quitamos» comparten la raíz «quita»


def _palabras(t: str) -> tuple[str, ...]:
    """Las palabras de la acción, sin acentos, mayúsculas, orden ni palabras vacías, reducidas a su raíz."""
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return tuple(sorted({w[:RAIZ] for w in re.findall(r"[a-z0-9_]+", t) if w not in _VACIAS}))


def clave_plan(p: dict) -> str:
    """Huella de la INTERVENCIÓN: las líneas y las palabras de la acción (sin acentos, mayúsculas, orden ni palabras
    vacías). Dos redacciones de la misma intervención dan la misma huella."""
    inter = sorted((tuple(x["lineas"]), _palabras(x["accion"])) for x in p["intervenciones"] if x["en_zona"])
    return hashlib.sha256(json.dumps(inter).encode()).hexdigest()


def coherencia(plan: dict, aplicacion: list[dict]) -> dict:
    return RC10.coherencia({"lineas_a_modificar": [{"lineas": x["lineas"], "en_zona": x["en_zona"]}
                                                   for x in plan["intervenciones"]]}, aplicacion)


SISTEMA_REVISORA_PLAN_11 = RC10.SISTEMA_REVISORA_PLAN


def mensajes_plan(f: dict, contexto: str) -> list[dict]:
    usuario = RC10._base(f) + (f"\n\n{contexto}" if contexto else "") + f"\n\n{RC10.PEDIDO_PLAN}"
    return [{"role": "system", "content": SISTEMA_PLANIFICADOR_11}, {"role": "user", "content": usuario}]


def mensajes_revision_plan(f: dict, plan: dict) -> list[dict]:
    usuario = (RC10._base(f) + f"\n\nPLAN DE CORRECCIÓN PROPUESTO POR EL PLANIFICADOR:\n{texto_plan(plan)}\n\n"
               + RC10.PEDIDO_REVISION_PLAN)
    return [{"role": "system", "content": SISTEMA_REVISORA_PLAN_11}, {"role": "user", "content": usuario}]


def mensajes_parche(f: dict, plan: dict, aviso: str = "") -> list[dict]:
    usuario = (RC10._base(f) + f"\n\nPLAN A IMPLEMENTAR:\n{texto_plan(plan)}\n\n"
               + (f"AVISO DEL COORDINADOR:\n{aviso}\n\n" if aviso else "") + RC10.PEDIDO_PARCHE)
    return [{"role": "system", "content": PA.SISTEMA_PARCHE}, {"role": "user", "content": usuario}]


def carpeta(carpeta_lab, corrida):
    try:
        return RGD.carpeta_de_reparacion(carpeta_lab, corrida, "experimento-7-11")
    except RGD.ErrorReparacion as e:
        raise RC.ErrorCoordinada(str(e)) from None


# Recorrido ----------------------------------------------------------------------------------------------------------

def correr(destino, f: dict, executor, cliente, gate, diagnosticador, programador, max_segundos: float = 2400.0,
           cancel: threading.Event | None = None) -> dict:
    s = RC10._Sesion(destino, f, cliente, gate, max_segundos, cancel)
    funcion = s.spec["function"]
    fabrica, referencia = MISIONES[f["mision"]]
    contraste = CT.desde_mision(fabrica, referencia, VALIDADORES[f["mision"]])
    codigo = f["codigo"]
    original = RE.programa(codigo)
    ct = RC.discrepancia(executor, f)
    esperado = ct["referencia"]["salida"]
    s.comp_inicial = RE.comparar_salidas(esperado, ct["candidato"]["salida"])
    inf = {"experimento": VERSION_11, "corrida": f["corrida"], "mision": f["mision"], "zonas": f["zonas"],
           "diagnosticador": diagnosticador.name, "programador": programador.name,
           "limites": {"ciclos": MAX_CICLOS, "planes_por_ciclo": MAX_PLANES_POR_CICLO, "planes": MAX_PLANES,
                       "parches": MAX_PARCHES, "segundos": max_segundos},
           "archivos_leidos": f["archivos"], "diagnosticos": [], "diagnostico_usado": None, "ciclos": [],
           "verificacion": None, "detenido": None, "inicio": time.time()}

    disc = RC.texto_discrepancia(funcion, ct)
    nota, valido = "", None
    for k in range(1, RC.MAX_DIAGNOSTICOS + 1):
        if (motivo := s.sin_tiempo()):
            inf["detenido"] = motivo
            break
        gen, error = s.llamar("diagnosticadora", diagnosticador, mensajes_diagnostico(f, disc, nota), f"diagnostico-{k}")
        reg = {"intento": k, "valido": False}
        inf["diagnosticos"].append(reg)
        if gen is None:
            reg["motivo"] = error
            inf["detenido"] = f"la diagnosticadora no produjo un diagnóstico: {error}"
            break
        try:
            reg["diagnostico"] = interpretar_diagnostico(gen.text, codigo, f["zonas"], funcion)
            reg["valido"] = True
            valido = reg["diagnostico"]
        except ValueError as e:
            reg["motivo"] = str(e)
            nota = RC.nota_de_diagnostico_invalido(str(e))
        s.guardar(f"diagnostico-{k}/diagnostico.json", reg)
        if valido:
            break
    if valido is None:
        inf["detenido"] = inf["detenido"] or "la diagnosticadora no produjo un diagnóstico válido"
        return _cerrar(inf, s, f)
    f = dict(f, diagnostico_texto=texto_diagnostico(valido))
    inf["diagnostico_usado"] = {"texto": f["diagnostico_texto"],
                                "huella": hashlib.sha256(f["diagnostico_texto"].encode()).hexdigest()}

    contexto, claves_planes, claves_parches, programas, avisado = "", [], [], [], False
    planes_total = parches_total = 0
    for c in range(1, MAX_CICLOS + 1):
        ciclo = {"ciclo": c, "planes": [], "plan_a_implementar": None, "parche": None}
        inf["ciclos"].append(ciclo)
        ultimo_valido, aprobado, critica = None, None, ""
        for k in range(1, MAX_PLANES_POR_CICLO + 1):
            if planes_total >= MAX_PLANES or (motivo := s.sin_tiempo()):
                inf["detenido"] = inf["detenido"] or (motivo if planes_total < MAX_PLANES else
                                                      f"límite de {MAX_PLANES} planes")
                break
            planes_total += 1
            ctx = "\n\n".join(x for x in (contexto, critica) if x)
            gen, error = s.llamar("planificador", programador, mensajes_plan(f, ctx), f"ciclo-{c}/plan-{k}")
            pl = {"plan": k, "valido": False, "contexto_recibido": ctx or None}
            ciclo["planes"].append(pl)
            if gen is None:
                pl["motivo"] = error
                inf["detenido"] = f"el planificador no respondió: {error}"
                break
            try:
                # «Qué cambia» se exige después de un parche que falló (no tras una crítica dentro del ciclo).
                plan = interpretar_plan(gen.text, codigo, f["zonas"], funcion, hubo_anterior=bool(contexto))
            except ValueError as e:
                pl["motivo"] = str(e)
                critica = f"Tu plan {k} no se pudo usar ({e}). Respondé con el JSON completo."
                s.guardar(f"ciclo-{c}/plan-{k}/plan.json", pl)
                continue
            kp = clave_plan(plan)
            repetido = next((x for x, y in claves_planes if y == kp), None)
            claves_planes.append((f"ciclo {c}, plan {k}", kp))
            pl.update(valido=True, contenido=plan, repite_el_plan=repetido)
            ultimo_valido = (k, plan)
            if repetido:
                # La misma intervención: no se gasta otra revisión; se le pide algo distinto.
                pl["revision"] = {"valida": False, "decision": None, "motivo": "plan repetido: no se revisa"}
                critica = (f"Tu plan {k} propone la MISMA intervención que {repetido} (aunque esté redactado distinto): "
                           "repetirlo no aporta información nueva. Proponé una intervención distinta.")
                pl["critica_enviada"] = critica
                s.guardar(f"ciclo-{c}/plan-{k}/plan.json", pl)
                continue
            s.guardar(f"ciclo-{c}/plan-{k}/plan.json", pl)
            if (motivo := s.sin_tiempo()):
                inf["detenido"] = motivo
                break
            gen, error = s.llamar("revisora del plan", diagnosticador, mensajes_revision_plan(f, plan),
                                  f"ciclo-{c}/revision-plan-{k}")
            rv = {"valida": False, "decision": None}
            if gen is not None:
                try:
                    rv.update(valida=True, **RC9.interpretar_revision(gen.text, funcion))
                except ValueError as e:
                    rv["motivo"] = str(e)
            else:
                rv["motivo"] = error
            pl["revision"] = rv
            s.guardar(f"ciclo-{c}/revision-plan-{k}/revision.json", rv)
            if rv["decision"] == "rechazar":
                critica = RC10.texto_critica_plan(k, rv)
                pl["critica_enviada"] = critica
                continue
            aprobado = (k, plan)
            break
        if inf["detenido"] and not aprobado:
            break
        if aprobado:
            k_impl, plan_impl = aprobado
            estado_plan = ("aprobado por la revisora" if (ciclo["planes"][k_impl - 1].get("revision") or {}).get(
                "decision") == "aprobar" else "sin decisión válida de la revisora")
        elif ultimo_valido:
            # Revisión NO bloqueante: se implementa el último plan válido, rechazado, solo para comprobarlo.
            k_impl, plan_impl = ultimo_valido
            estado_plan = "rechazado por la revisora: se implementa bajo evaluación experimental"
        else:
            ciclo["sin_plan_valido"] = True
            contexto = ("RESULTADO DEL CICLO ANTERIOR: ningún plan válido.\n" + critica) if critica else ""
            continue
        ciclo["plan_a_implementar"] = {"plan": k_impl, "estado": estado_plan,
                                       "bajo_evaluacion_experimental": estado_plan.startswith("rechazado")}
        if parches_total >= MAX_PARCHES or (motivo := s.sin_tiempo()):
            inf["detenido"] = inf["detenido"] or motivo or f"límite de {MAX_PARCHES} parches"
            break
        parches_total += 1
        aviso = "REPETICIÓN: en un ciclo anterior propusiste un parche equivalente." if avisado else ""
        gen, error = s.llamar("programador", programador, mensajes_parche(f, plan_impl, aviso), f"ciclo-{c}/parche")
        pa = {"aplicado": False, "plan": k_impl, "estado_del_plan": estado_plan}
        ciclo["parche"] = pa
        if gen is None:
            pa["motivo"] = error
            inf["detenido"] = f"el programador no respondió: {error}"
            break
        nuevo = None
        try:
            parche = PA.interpretar_parche(gen.text, funcion)
            pa.update(parche)
            kp = PA.clave(parche)
            pa["repite_el_parche_del_ciclo"] = next((x for x, y in claves_parches if y == kp), None)
            claves_parches.append((c, kp))
            try:
                nuevo, aplicacion = PA.aplicar(codigo, parche, f["zonas"], s.spec)
            except PA.ParcheRechazado as e:
                if "no aparece en el código" not in str(e):
                    raise
                pa["rechazo_literal"] = str(e)
                normalizado, just = RGD.normalizar_sangria(codigo, parche, f["zonas"])
                pa.update(parche_normalizado=normalizado, normalizacion=just)
                nuevo, aplicacion = PA.aplicar(codigo, normalizado, f["zonas"], s.spec)
            pa.update(aplicado=True, aplicacion=aplicacion, coherencia_con_el_plan=coherencia(plan_impl, aplicacion))
            prog = RE.programa(nuevo)
            pa["mismo_programa_que_el_ciclo"] = next((x for x, y in programas if y == prog), None)
            programas.append((c, prog))
        except PA.ParcheRechazado as e:
            pa["motivo"] = str(e)
        s.guardar(f"ciclo-{c}/parche/parche.json", pa)
        if pa.get("repite_el_parche_del_ciclo") or pa.get("mismo_programa_que_el_ciclo"):
            if avisado:
                inf["detenido"] = (f"el parche del ciclo {c} repite uno anterior después del aviso: no se gastan más "
                                   "inferencias sin información nueva")
                break
            avisado = True
        if nuevo is None:
            contexto = (f"RESULTADO DEL CICLO {c}: el parche del plan fue RECHAZADO por el coordinador: {pa['motivo']}\n"
                        "En tu nuevo plan explicá qué cambia respecto de este intento.")
            continue
        s.guardar(f"ciclo-{c}/parche/solucion.py", nuevo)
        ctp = CT.contrastar(executor, contraste, nuevo, f["caso"], None)
        vis = RGD._visibles(executor, nuevo, s.spec)
        d = {"no_observada": "eliminada", "demostrada": "persiste"}.get(ctp["discrepancia"], "inconclusa")
        pa.update(discrepancia_sintetica=d, visibles={x: vis[x] for x in ("ok", "aprobados", "total")},
                  modifica_el_programa=RE.programa(nuevo) != original)
        if d == "eliminada" and vis["ok"]:
            ver = RE._bateria(executor, nuevo, fabrica())
            inf["verificacion"] = dict(ver, ciclo=c)
            pa["bateria"] = {x: ver.get(x) for x in ("ok", "aprobados", "total")}
            s.guardar(f"ciclo-{c}/parche/parche.json", pa)
            break
        s.guardar(f"ciclo-{c}/parche/parche.json", pa)
        cand = ctp["candidato"] or {"ejecucion": ctp["discrepancia"], "salida": None, "detalle": ctp["motivo"]}
        retro = RC8.retroalimentacion(c, f["caso"], funcion, esperado, cand, d, vis, "parche_normalizado" in pa)
        s.guardar(f"ciclo-{c}/parche/retroalimentacion.md", retro)
        contexto = (f"RESULTADO DEL CICLO {c} (el plan implementado y su parche):\nPlan:\n{texto_plan(plan_impl)}\n"
                    f"Resultado:\n{retro}\nEn tu nuevo plan explicá qué cambia respecto de este intento.")
    if not inf["detenido"] and not inf["verificacion"]:
        inf["detenido"] = f"ningún ciclo corrigió el defecto en {len(inf['ciclos'])} ciclo(s)"
    return _cerrar(inf, s, f)


# Medición -----------------------------------------------------------------------------------------------------------

def referencias_posteriores(f: dict, comp_inicial: dict | None) -> dict:
    """Referencias para evaluar DESPUÉS (no llegan a ningún modelo):
    - origen: las líneas del código defectuoso que difieren de la referencia de la misión;
    - manifestación: la zona donde el código emite el valor equivocado del caso sintético (lo que «sobra»)."""
    _, referencia = MISIONES[f["mision"]]
    defectuoso = f["codigo"].splitlines()
    ref = (sin_comentarios(referencia) or "").splitlines()
    origen = set()
    for op, a1, a2, _b1, _b2 in difflib.SequenceMatcher(None, defectuoso, ref, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            origen.update(range(a1 + 1, a2 + 1))
    literales = set()

    def hojas(v):
        if isinstance(v, str):
            literales.add(v)
        elif isinstance(v, dict):
            for x in v.values():
                hojas(x)
        elif isinstance(v, list):
            for x in v:
                hojas(x)
    for dd in ((comp_inicial or {}).get("detalle") or {}).values():
        hojas(dd.get("sobran") if "sobran" in dd else dd.get("tu_intento"))
    tree = ast.parse(f["codigo"])
    manifestacion = set()
    for nodo in ast.walk(tree):
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and nodo.value in literales:
            z = PA._zona(tree, nodo.lineno)
            if z:
                manifestacion.update(range(z[0], z[1] + 1))
    return {"origen": sorted(origen), "manifestacion": sorted(manifestacion - origen)}


def medir(inf: dict, f: dict) -> dict:
    refs = inf.get("referencias_posteriores") or {"origen": [], "manifestacion": []}
    origen, manif = set(refs["origen"]), set(refs["manifestacion"])
    diag = next((d["diagnostico"] for d in inf["diagnosticos"] if d["valido"]), None)
    desacuerdos = []
    for c in inf["ciclos"]:
        pa = c.get("parche") or {}
        for p in c["planes"]:
            if not p.get("valido"):
                continue
            plan = p["contenido"]
            lineas = {n for x in plan["intervenciones"] if x["en_zona"] for n in x["lineas"]}
            implementado = (c.get("plan_a_implementar") or {}).get("plan") == p["plan"] and pa
            ejecucion = None
            if implementado and pa.get("aplicado") and (pa.get("coherencia_con_el_plan") or {}).get("coherente"):
                ejecucion = bool(pa.get("bateria", {}).get("ok")) if pa.get("discrepancia_sintetica") == "eliminada" \
                    and (pa.get("visibles") or {}).get("ok") else False
            p["evaluacion"] = {
                "apunta_al_origen": bool(lineas & origen) if origen else None,
                "comprobado_por_ejecucion": ejecucion,
                # Correcto solo si apunta al origen Y la ejecución de su implementación coherente lo confirma.
                "plan_causal_correcto": bool(lineas & origen) and ejecucion is True,
                "decision_de_la_revisora": (p.get("revision") or {}).get("decision"),
            }
            dec = p["evaluacion"]["decision_de_la_revisora"]
            if dec and ejecucion is not None and (dec == "aprobar") != ejecucion:
                desacuerdos.append({"ciclo": c["ciclo"], "plan": p["plan"], "revisora": dec,
                                    "ejecucion": "aprobó la batería" if ejecucion else "no corrigió el defecto",
                                    "tipo": "aprobó un plan cuya implementación falló" if dec == "aprobar" else
                                    "rechazó un plan cuya implementación aprobó la batería"})
    ultimo = next((c for c in reversed(inf["ciclos"]) if c.get("parche")), None)
    pa = (ultimo or {}).get("parche") or {}
    plan_ult = next((p for p in (ultimo or {}).get("planes", []) if ultimo.get("plan_a_implementar")
                     and p["plan"] == ultimo["plan_a_implementar"]["plan"]), None)
    o_lineas = set(diag["origen_del_defecto"]["lineas"]) if diag else set()
    m_lineas = set(diag["punto_de_manifestacion"]["lineas"]) if diag else set()
    planes = [p for c in inf["ciclos"] for p in c["planes"] if p.get("valido")]
    return {
        "posteriores_deterministas_aproximadas": {
            "1_diagnostico_identifica_el_origen": bool(o_lineas & origen) if origen else None,
            "2_diagnostico_identifica_la_manifestacion": bool(m_lineas & manif) if manif else None,
            "3_plan_propone_intervencion_causal_concreta": bool(plan_ult and plan_ult["evaluacion"]["apunta_al_origen"]),
            "4_plan_declara_invariantes": bool(plan_ult and plan_ult["contenido"]["invariantes_preservados"]),
        },
        "opinion_del_modelo": {
            "5_revision_de_gemma": (plan_ult or {}).get("revision", {}).get("decision"),
            "plan_implementado_bajo_evaluacion_experimental": bool((ultimo or {}).get("plan_a_implementar", {}).get(
                "bajo_evaluacion_experimental")),
        },
        "demostrado_por_ejecucion": {
            "4_plan_preserva_invariantes": (plan_ult or {}).get("evaluacion", {}).get("comprobado_por_ejecucion"),
            "6_parche_implementa_el_plan": bool((pa.get("coherencia_con_el_plan") or {}).get("coherente")),
            "parche_aplicado": bool(pa.get("aplicado")),
            "7_parche_supera_el_caso_sintetico": pa.get("discrepancia_sintetica") == "eliminada",
            "8_bateria_independiente_9_9": bool(inf["verificacion"] and inf["verificacion"]["ok"]),
        },
        "plan_causal_correcto": bool(plan_ult and plan_ult["evaluacion"]["plan_causal_correcto"]),
        "desacuerdos_revisora_vs_ejecucion": desacuerdos,
        "planes": len(planes), "planes_rechazados_por_la_revisora": sum(
            (p.get("revision") or {}).get("decision") == "rechazar" for p in planes),
        "planes_repetidos": sum(bool(p.get("repite_el_plan")) for p in planes),
        "parches_repetidos": sum(bool((c.get("parche") or {}).get("repite_el_parche_del_ciclo")
                                      or (c.get("parche") or {}).get("mismo_programa_que_el_ciclo"))
                                 for c in inf["ciclos"]),
        "nota": ("«posteriores deterministas aproximadas»: comparan con la diferencia entre el código defectuoso y la "
                 "referencia y con el caso sintético, DESPUÉS de la corrida; «demostrado por ejecución»: sandbox"),
    }


def _cerrar(inf: dict, s, f: dict) -> dict:
    inf["referencias_posteriores"] = referencias_posteriores(f, getattr(s, "comp_inicial", None))
    inf["medicion"] = medir(inf, f)
    inf["auditoria_de_fugas"] = RE.auditar(s.pedidos, s.spec, s.f["defecto"], s.descripcion)
    inf["llamadas"] = s.llamadas
    inf["consumo"] = {"llamadas": len(s.llamadas), "segundos": round(s.usados(), 1),
                      "por_rol": {r: sum(1 for x in s.llamadas if x["rol"] == r) for r in
                                  ("diagnosticadora", "planificador", "revisora del plan", "programador")}}
    d = inf["medicion"]["demostrado_por_ejecucion"]
    # El criterio principal es el comportamiento comprobado (no la opinión de la revisora).
    inf["exito"] = bool(d["parche_aplicado"] and d["7_parche_supera_el_caso_sintetico"]
                        and d["8_bateria_independiente_9_9"] and inf["auditoria_de_fugas"]["sin_fugas"])
    ultimo = next((c for c in reversed(inf["ciclos"]) if c.get("parche")), None)
    pa = (ultimo or {}).get("parche") or {}
    if inf["exito"]:
        notas = []
        if (ultimo.get("plan_a_implementar") or {}).get("bajo_evaluacion_experimental"):
            notas.append("plan rechazado por la revisora, implementado bajo evaluación experimental")
        if "parche_normalizado" in pa:
            notas.append("con normalización de la sangría")
        inf["clasificacion"] = CLASIFICACION_11 + (f" ({'; '.join(notas)})" if notas else "")
    else:
        inf["clasificacion"] = None
    inf["motivo"] = inf["detenido"] or (f"batería independiente {inf['verificacion']['aprobados']}/"
                                        f"{inf['verificacion']['total']} en el ciclo {inf['verificacion']['ciclo']}"
                                        if inf["verificacion"] else None)
    inf["fin"] = time.time()
    s.guardar("informe.json", inf)
    return inf
