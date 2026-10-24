"""Experimento 7.13: EVOLUCIÓN de seis candidatos independientes (OFICINA.md, «Experimento 7.13»).

Parte del torneo del 7.12 (mismos candidatos A–F, estrategias y modelos) y convierte cada candidato en una LÍNEA
EVOLUTIVA de hasta MAX_GENERACIONES generaciones. Reutiliza piezas del 7.6–7.12 sin modificarlas.

Cada generación de una línea: (1) formula o revisa su hipótesis (el paso previo de su estrategia, si lo tiene, o la
justificación del parche); (2) genera un parche; (3) lo aplica sobre una COPIA LIMPIA del código original (nunca sobre
un intento anterior); (4) valida zonas, sintaxis y sandbox; (5) ejecuta las PRUEBAS DE DESARROLLO; (6) arma el
feedback; (7) decide continuar o detenerse. Desde la segunda generación el parche tiene que traer
«revision_de_la_hipotesis»: qué hipótesis anterior resultó incorrecta y qué cambia.

Independencia: cada línea ve SOLO su propio historial (sus hipótesis, parches y resultados de desarrollo); nunca nada
de otra línea. Inferencias secuenciales (una GPU).

Desarrollo y evaluación final SEPARADOS:
- PRUEBAS DE DESARROLLO: el caso sintético y los ejemplos visibles del contrato. Su entrada, salida esperada y salida
  observada se usan como feedback.
- BATERÍA FINAL RESERVADA: los 9 casos del 7.12. Nada de ella llega a los agentes. Se ejecuta recién cuando TODAS las
  líneas terminaron y quedaron congeladas, solo sobre la mejor solución de cada línea que supera el desarrollo, una
  vez por programa distinto. Se registran las huellas de los dos conjuntos y el momento de la última inferencia y de
  la primera ejecución de la batería.

Feedback por categoría: parche inválido o fuera de zona; error de sintaxis o ejecución; discrepancia sintética
presente; regresión en ejemplos visibles; supera todas las pruebas de desarrollo (la línea se detiene). Un programa
igual a otro de la MISMA línea no se vuelve a evaluar: se informa; dos repeticiones detienen la línea (sin progreso).

Mejor solución de cada línea (determinista): caso sintético corregido > más ejemplos visibles aprobados > se ejecuta
sin error > parche válido > menos líneas cambiadas (solo desempate) > generación más temprana. Se conserva aunque la
última generación sea peor. Ganadores: como en el 7.12 (caso corregido y 9/9 en la batería), con su desempate.

Presupuesto (explícito y documentado antes de ejecutar): ver PRESUPUESTO. Una línea que lo agota se detiene sin
impedir que las demás sigan. La memoria y el tiempo de cada ejecución los limita el sandbox (config).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time

from . import contraste as CT
from . import parches as PA
from . import reparacion_coordinada as RC
from . import reparacion_coordinada_8 as RC8
from . import reparacion_coordinada_10 as RC10
from . import reparacion_coordinada_11 as RC11
from . import reparacion_guiada as RGD
from . import reparacion_torneo_12 as RT12
from . import revision_existente as RE
from .misiones_programacion import MISIONES, VALIDADORES

VERSION_13 = "experimento 7.13: evolución de seis candidatos independientes (desarrollo separado de la batería final)"
CLASIFICACION_13 = "reparación evolutiva de candidatos independientes, comprobada con la batería reservada"
ESTRATEGIAS = RT12.ESTRATEGIAS
MAX_GENERACIONES = 3
MAX_LLAMADAS = MAX_GENERACIONES * sum(len(e["pasos"]) for e in ESTRATEGIAS)          # 27
MAX_PARCHES = MAX_GENERACIONES * len(ESTRATEGIAS)                                    # 18
MAX_SEGUNDOS = 3600.0
MAX_TOKENS_GENERADOS_POR_LINEA = 12000
MAX_REPETICIONES = 2
CATEGORIAS = ("hipotesis_invalida", "parche_invalido", "error_de_ejecucion", "discrepancia_sintetica",
              "regresion_visibles", "supera_desarrollo", "programa_repetido")


def presupuesto(max_segundos: float = MAX_SEGUNDOS) -> dict:
    return {"candidatos": len(ESTRATEGIAS), "generaciones_por_candidato": MAX_GENERACIONES,
            "llamadas": MAX_LLAMADAS, "parches": MAX_PARCHES, "segundos_totales": max_segundos,
            "segundos_por_linea": round(max_segundos / len(ESTRATEGIAS), 1),
            "tokens_generados_por_linea": MAX_TOKENS_GENERADOS_POR_LINEA,
            "repeticiones_de_programa_por_linea": MAX_REPETICIONES,
            "memoria_y_tiempo_por_ejecucion": "los límites del sandbox de la configuración (bwrap, seccomp, cgroups)"}


def _huella(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def huellas_de_pruebas(f: dict) -> dict:
    spec = MISIONES[f["mision"]][0]()["spec"]
    vis = [c for c in spec["cases"] if c.get("visible", True)]
    res = [c for c in spec["cases"] if not c.get("visible", True)]
    return {"desarrollo": {"caso_sintetico": _huella(json.dumps(f["caso"], sort_keys=True, ensure_ascii=False)),
                           "ejemplos_visibles": _huella(json.dumps(vis, sort_keys=True, ensure_ascii=False)),
                           "cantidad": 1 + len(vis)},
            "bateria_reservada": {"huella": _huella(json.dumps(spec["cases"], sort_keys=True, ensure_ascii=False)),
                                  "casos": len(spec["cases"]), "ocultos": len(res)}}


def puntaje(g: dict) -> tuple:
    """Mayor es mejor. Solo pruebas de desarrollo."""
    d = g.get("desarrollo") or {}
    return (bool(d.get("caso_corregido")), d.get("visibles_aprobados") or 0, bool(d.get("ejecuta")),
            bool(d.get("parche_valido")), -(g.get("lineas_cambiadas") or 0), -g["generacion"])


def _texto_parche(p: dict) -> str:
    return "\n".join(f"  cambio {i}: ORIGINAL:\n{c['original']}\n  REEMPLAZO:\n{c['reemplazo']}"
                     for i, c in enumerate(p.get("cambios", []), 1))


def historial(gens: list[dict]) -> str:
    """El historial PROPIO de una línea: hipótesis, parche y resultado de desarrollo de cada generación."""
    if not gens:
        return ""
    l = ["HISTORIAL DE TUS INTENTOS ANTERIORES (solo los tuyos; cada parche se aplicó sobre el código ORIGINAL):"]
    for g in gens:
        l.append(f"INTENTO {g['generacion']}:")
        if g.get("hipotesis"):
            l.append(f"  Hipótesis: {g['hipotesis']}")
        if g.get("parche"):
            l.append("  Parche:\n" + _texto_parche(g["parche"]))
        l.append(f"  Resultado ({g['categoria']}): {g['feedback']}")
    l.append("En este intento agregá al JSON el campo «revision_de_la_hipotesis»: qué hipótesis anterior resultó "
             "incorrecta y qué cambia en tu nueva propuesta. Un cambio superficial no alcanza.")
    return "\n".join(l)


def _revision_de_la_hipotesis(texto: str) -> str:
    m = re.search(r"\{.*\}", texto or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        d = {}
    v = d.get("revision_de_la_hipotesis") if isinstance(d, dict) else None
    return v.strip()[:600] if isinstance(v, str) else ""


def carpeta(carpeta_lab, corrida):
    try:
        return RGD.carpeta_de_reparacion(carpeta_lab, corrida, "experimento-7-13")
    except RGD.ErrorReparacion as e:
        raise RC.ErrorCoordinada(str(e)) from None


def mensajes_primera_generacion(f: dict, disc: str, estrategia: dict) -> list[dict]:
    """Para el preflight: el pedido de parche de la primera generación (sin historial)."""
    return RT12.mensajes_parche(f, disc, estrategia)


def correr(destino, f: dict, executor, cliente, gate, diagnosticador, programador, max_segundos: float = MAX_SEGUNDOS,
           cancel: threading.Event | None = None) -> dict:
    s = RC10._Sesion(destino, f, cliente, gate, max_segundos, cancel)
    funcion = s.spec["function"]
    fabrica, referencia = MISIONES[f["mision"]]
    contraste = CT.desde_mision(fabrica, referencia, VALIDADORES[f["mision"]])
    ORIGINAL = f["codigo"]
    huella_original = _huella(ORIGINAL)
    ct = RC.discrepancia(executor, f)
    esperado_caso = ct["referencia"]["salida"]
    disc = RC.texto_discrepancia(funcion, ct)
    modelos = {"gemma": diagnosticador, "qwen": programador}
    inf = {"experimento": VERSION_13, "corrida": f["corrida"], "mision": f["mision"], "zonas": f["zonas"],
           "modelos": {"gemma": diagnosticador.name, "qwen": programador.name},
           "presupuesto": presupuesto(max_segundos), "huellas_de_pruebas": huellas_de_pruebas(f),
           "huella_del_codigo_original": huella_original, "archivos_leidos": f["archivos"], "lineas": [],
           "inicio": time.time()}
    s.guardar("presupuesto.json", inf["presupuesto"])
    por_linea = max_segundos / len(ESTRATEGIAS)
    for e in ESTRATEGIAS:
        L = e["letra"]
        linea = {"letra": L, "estrategia": e["nombre"], "instruccion": e["instruccion"],
                 "pasos": [{"etapa": p, "modelo": modelos[m].name} for p, m in e["pasos"]],
                 "generaciones": [], "llamadas": [], "detenida": None, "mejor": None}
        inf["lineas"].append(linea)
        programas, repeticiones = {}, 0
        for gnum in range(1, MAX_GENERACIONES + 1):
            seg = sum(x["segundos"] or 0 for x in linea["llamadas"])
            tok = sum(x.get("tokens_generados") or 0 for x in linea["llamadas"])
            if (motivo := s.sin_tiempo()) or len(s.llamadas) >= MAX_LLAMADAS:
                linea["detenida"] = motivo or f"límite global de {MAX_LLAMADAS} llamadas"
                break
            if seg >= por_linea:
                linea["detenida"] = f"agotó su tiempo ({seg:.0f} s de {por_linea:.0f} s por línea)"
                break
            if tok >= MAX_TOKENS_GENERADOS_POR_LINEA:
                linea["detenida"] = f"agotó sus tokens ({tok} de {MAX_TOKENS_GENERADOS_POR_LINEA})"
                break
            g = _generacion(gnum, e, linea, f, disc, s, modelos, executor, contraste, esperado_caso, ORIGINAL,
                            programas)
            linea["generaciones"].append(g)
            s.guardar(f"candidato-{L}/generacion-{gnum}/generacion.json", g)
            if _huella(ORIGINAL) != huella_original:
                raise RuntimeError("el código original cambió durante la evolución")
            if g["categoria"] == "programa_repetido":
                repeticiones += 1
                if repeticiones >= MAX_REPETICIONES:
                    linea["detenida"] = f"sin progreso: repitió {repeticiones} veces un programa propio"
                    break
            if g["categoria"] == "supera_desarrollo":
                linea["detenida"] = "supera todas las pruebas de desarrollo: la línea se congela"
                break
            if g.get("sin_inferencia"):
                linea["detenida"] = g["feedback"]
                break
        linea["detenida"] = linea["detenida"] or f"completó {MAX_GENERACIONES} generaciones"
        evaluables = [g for g in linea["generaciones"] if g.get("desarrollo") and g["categoria"] != "programa_repetido"]
        if evaluables:
            mejor = max(evaluables, key=puntaje)
            linea["mejor"] = {"generacion": mejor["generacion"], "desarrollo": mejor["desarrollo"],
                              "huella_programa": mejor.get("huella_programa"), "lineas_cambiadas":
                              mejor.get("lineas_cambiadas"), "sangria_normalizada": mejor.get("sangria_normalizada"),
                              "motivo": "mejor puntaje de desarrollo (caso, visibles, ejecución, validez; desempate "
                                        "por menos líneas y generación más temprana)"}
        linea["programas_distintos"] = len(programas)
        s.guardar(f"candidato-{L}/linea.json", {k: v for k, v in linea.items() if k != "generaciones"})
    inf["ultima_inferencia"] = max((x["momento"] for x in s.llamadas), default=None)
    _evaluacion_final(inf, s, executor, fabrica)
    return _cerrar(inf, s)


def _generacion(gnum, e, linea, f, disc, s, modelos, executor, contraste, esperado_caso, ORIGINAL, programas) -> dict:
    L = e["letra"]
    funcion = s.spec["function"]
    g = {"generacion": gnum, "categoria": None, "feedback": None}
    prev = historial(linea["generaciones"])
    previo = ""
    base = f"candidato-{L}/generacion-{gnum}"
    for etapa, quien in e["pasos"]:
        if etapa == "parche":
            msgs = RT12.mensajes_parche(f, disc, e, "\n\n".join(x for x in (previo, prev) if x))
        elif etapa == "diagnostico":
            msgs = RC8.mensajes_diagnostico(f, disc, prev)
        elif etapa == "localizacion":
            msgs = RC11.mensajes_diagnostico(f, disc, prev)
        else:
            msgs = RT12.mensajes_plan(f, disc)
            if prev:
                msgs = [msgs[0], dict(msgs[1], content=msgs[1]["content"].replace(
                    RC10.PEDIDO_PLAN, prev + "\n\n" + RC10.PEDIDO_PLAN))]
        n = len(s.llamadas)
        gen, error = s.llamar(f"candidato {L} · generación {gnum}: {etapa}", modelos[quien], msgs, f"{base}/{etapa}")
        for x in s.llamadas[n:]:
            x["momento"] = time.time()          # «fin» ya es el done_reason de Ollama
        linea["llamadas"] += s.llamadas[n:]
        if gen is None:
            g.update(categoria="hipotesis_invalida" if etapa != "parche" else "parche_invalido",
                     feedback=f"{etapa}: {error}", sin_inferencia=True)
            return g
        if etapa == "parche":
            texto = gen.text
            break
        try:
            if etapa == "diagnostico":
                d = RC8.interpretar(gen.text, ORIGINAL, f["zonas"], funcion)
                g["hipotesis"] = d["causa"]
                previo = f"DIAGNÓSTICO (de tu propio equipo, otra IA; no está verificado):\n{RC8.texto_para_el_programador(d)}"
            elif etapa == "localizacion":
                d = RC11.interpretar_diagnostico(gen.text, ORIGINAL, f["zonas"], funcion)
                g["hipotesis"] = d["causa"]
                previo = f"LOCALIZACIÓN DEL DEFECTO (de tu propio equipo, otra IA; no está verificada):\n" \
                         f"{RC11.texto_diagnostico(d)}"
            else:
                pl = RC11.interpretar_plan(gen.text, ORIGINAL, f["zonas"], funcion, gnum > 1)
                g["hipotesis"] = pl["estado_a_corregir"]
                previo = f"TU PLAN:\n{RC11.texto_plan(pl)}"
        except ValueError as ex:
            g.update(categoria="hipotesis_invalida", feedback=f"tu {etapa} no se pudo usar: {ex}")
            return g
    # Parche (sobre una COPIA LIMPIA del original).
    if gnum > 1:
        g["revision_de_la_hipotesis"] = _revision_de_la_hipotesis(texto)
    try:
        parche = PA.interpretar_parche(texto, funcion)
        g["parche"] = parche
        g["hipotesis"] = g.get("hipotesis") or parche.get("justificacion")
        if gnum > 1 and not g["revision_de_la_hipotesis"]:
            raise PA.ParcheRechazado("falta «revision_de_la_hipotesis»: explicá qué hipótesis anterior resultó "
                                     "incorrecta y qué cambia")
        try:
            nuevo, aplicacion = PA.aplicar(ORIGINAL, parche, f["zonas"], s.spec)
        except PA.ParcheRechazado as ex:
            if "no aparece en el código" not in str(ex):
                raise
            g["rechazo_literal"] = str(ex)
            normalizado, just = RGD.normalizar_sangria(ORIGINAL, parche, f["zonas"])
            g.update(parche_normalizado=normalizado, normalizacion=just)
            nuevo, aplicacion = PA.aplicar(ORIGINAL, normalizado, f["zonas"], s.spec)
    except PA.ParcheRechazado as ex:
        g.update(categoria="parche_invalido", feedback=f"parche inválido o fuera de zona: {ex}",
                 desarrollo={"parche_valido": False})
        return g
    cambio = RE.cambio_de_codigo(ORIGINAL, nuevo)
    prog = RE.programa(nuevo)
    g.update(aplicacion=aplicacion, huella_programa=prog, huella_solucion=_huella(nuevo),
             lineas_cambiadas=cambio["lineas_agregadas"] + cambio["lineas_quitadas"],
             sangria_normalizada="parche_normalizado" in g)
    s.guardar(f"{base}/solucion.py", nuevo)
    if prog in programas:
        g.update(categoria="programa_repetido", repite_la_generacion=programas[prog],
                 feedback=f"tu intento produce exactamente el mismo programa que tu intento {programas[prog]}: no se "
                          "vuelve a evaluar. Proponé un cambio distinto.")
        return g
    programas[prog] = gnum
    # Pruebas de DESARROLLO: caso sintético y ejemplos visibles.
    ctp = CT.contrastar(executor, contraste, nuevo, f["caso"], None)
    cand = ctp["candidato"] or {"ejecucion": ctp["discrepancia"], "salida": None, "detalle": ctp["motivo"]}
    vis = RGD._visibles(executor, nuevo, s.spec)
    ejecuta = cand.get("ejecucion") == "completa"
    corregido = ctp["discrepancia"] == "no_observada"
    g["desarrollo"] = {"parche_valido": True, "ejecuta": ejecuta, "caso_corregido": corregido,
                       "visibles_ok": vis["ok"], "visibles_aprobados": vis["aprobados"], "visibles_total": vis["total"]}
    if not ejecuta:
        g.update(categoria="error_de_ejecucion",
                 feedback=f"error de sintaxis o ejecución en el caso sintético: {cand.get('ejecucion')} — "
                          f"{RE._json(cand.get('salida') or cand.get('detalle') or ctp['motivo'], 400)}")
    elif not corregido:
        d = {"no_observada": "eliminada", "demostrada": "persiste"}.get(ctp["discrepancia"], "inconclusa")
        g.update(categoria="discrepancia_sintetica",
                 feedback=RC8.retroalimentacion(gnum, f["caso"], funcion, esperado_caso, cand, d, vis,
                                                "parche_normalizado" in g))
    elif not vis["ok"]:
        g.update(categoria="regresion_visibles",
                 feedback="el caso sintético da lo que pide el contrato, pero hay una REGRESIÓN en los ejemplos "
                          "visibles:\n" + vis["informe"])
    else:
        g.update(categoria="supera_desarrollo", feedback="supera todas las pruebas de desarrollo disponibles")
    return g


def _evaluacion_final(inf: dict, s, executor, fabrica) -> None:
    """La batería RESERVADA: solo después de congelar todas las líneas, sobre la mejor solución de cada una que
    supera el desarrollo, una vez por programa distinto. Nada de esto llega a ningún agente."""
    inf["primera_ejecucion_de_la_bateria"] = None
    baterias = {}
    for linea in inf["lineas"]:
        m = linea["mejor"]
        if not m or not (m["desarrollo"].get("caso_corregido") and m["desarrollo"].get("visibles_ok")):
            linea["bateria_final"] = None
            linea["motivo_final"] = ("sin una solución válida" if not m else
                                     "su mejor solución no supera el desarrollo: no se evalúa con la batería")
            continue
        g = next(x for x in linea["generaciones"] if x["generacion"] == m["generacion"])
        codigo = (s.destino / f"candidato-{linea['letra']}/generacion-{g['generacion']}/solucion.py").read_text()
        if m["huella_programa"] in baterias:
            ver, de = baterias[m["huella_programa"]]
            linea["bateria_reutilizada_de"] = de
        else:
            inf["primera_ejecucion_de_la_bateria"] = inf["primera_ejecucion_de_la_bateria"] or time.time()
            ver = RE._bateria(executor, codigo, fabrica())
            baterias[m["huella_programa"]] = (ver, linea["letra"])
        linea["bateria_final"] = {k: ver.get(k) for k in ("ok", "aprobados", "total", "ejecucion")}
        linea["motivo_final"] = ("caso corregido y batería final 9/9" if ver["ok"] else
                                 f"batería final {ver.get('aprobados')}/{ver.get('total')}")


def auditoria_entre_lineas(inf: dict, s) -> dict:
    """¿Algún pedido de una línea contiene texto producido por OTRA línea? (respuestas, hipótesis, justificaciones)"""
    propios = {}
    for linea in inf["lineas"]:
        textos = set()
        for g in linea["generaciones"]:
            for t in (g.get("hipotesis"), g.get("revision_de_la_hipotesis"), (g.get("parche") or {}).get("justificacion")):
                if isinstance(t, str) and len(t.strip()) >= 30:
                    textos.add(t.strip())
        propios[linea["letra"]] = textos
    hallazgos = []
    for linea in inf["lineas"]:
        L = linea["letra"]
        for p in sorted(s.destino.glob(f"candidato-{L}/generacion-*/*/pedido.json")):
            contenido = p.read_text()
            for otra, textos in propios.items():
                if otra != L and any(json.dumps(t, ensure_ascii=False)[1:-1] in contenido for t in textos):
                    hallazgos.append(f"{p.relative_to(s.destino)} contiene texto de la línea {otra}")
    return {"hallazgos": hallazgos, "sin_fugas": not hallazgos}


def _cerrar(inf: dict, s) -> dict:
    lineas = inf["lineas"]
    ganadoras = [x for x in lineas if (x.get("bateria_final") or {}).get("ok")]
    orden = sorted(ganadoras, key=lambda x: (x["mejor"]["lineas_cambiadas"], bool(x["mejor"]["sangria_normalizada"]),
                                             x["letra"]))
    inf["ganadores"] = [x["letra"] for x in orden]
    inf["ganador_por_desempate"] = orden[0]["letra"] if orden else None
    inf["criterio_de_desempate"] = RT12.DESEMPATE
    inf["tabla"] = [{"candidato": x["letra"], "estrategia": x["estrategia"],
                     "generaciones": len(x["generaciones"]), "programas_distintos": x["programas_distintos"],
                     "categorias": [g["categoria"] for g in x["generaciones"]],
                     "mejor_desarrollo": (None if not x["mejor"] else
                                          {"generacion": x["mejor"]["generacion"], **x["mejor"]["desarrollo"]}),
                     "bateria_final": x.get("bateria_final"), "detenida": x["detenida"],
                     "resultado": "ganador" if x["letra"] in inf["ganadores"] else x.get("motivo_final")}
                    for x in lineas]
    inf["auditoria_de_fugas"] = RE.auditar(s.pedidos, s.spec, s.f["defecto"], s.descripcion)
    inf["auditoria_entre_lineas"] = auditoria_entre_lineas(inf, s)
    pb, ui = inf.get("primera_ejecucion_de_la_bateria"), inf.get("ultima_inferencia")
    inf["bateria_despues_de_la_ultima_inferencia"] = pb is None or ui is None or pb >= ui
    inf["llamadas"] = s.llamadas
    inf["consumo"] = {"llamadas": len(s.llamadas), "segundos": round(s.usados(), 1),
                      "tokens_generados": sum(x.get("tokens_generados") or 0 for x in s.llamadas)}
    limpio = (inf["auditoria_de_fugas"]["sin_fugas"] and inf["auditoria_entre_lineas"]["sin_fugas"]
              and inf["bateria_despues_de_la_ultima_inferencia"])
    inf["exito"] = bool(ganadoras) and limpio
    inf["clasificacion"] = (CLASIFICACION_13 + f" (ganadores: {', '.join(inf['ganadores'])})") if inf["exito"] else None
    inf["motivo"] = (f"{len(ganadoras)} línea(s) corrigieron el caso y aprobaron la batería final 9/9" if ganadoras else
                     "ninguna línea corrigió el caso y aprobó la batería final 9/9")
    inf["fin"] = time.time()
    s.guardar("informe.json", inf)
    return inf
