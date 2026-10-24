"""Experimento 7.12: TORNEO de seis soluciones independientes (OFICINA.md, «Experimento 7.12»).

Seis candidatos A–F intentan reparar el MISMO defecto con estrategias distintas (instrucciones diferentes, Gemma 4 12B
y Qwen 2.5 Coder 7B), sin un diagnóstico único ni la aprobación de un revisor. Reutiliza piezas del 7.6–7.11 sin
modificarlas (sesión con auditoría, diagnósticos, plan, validación y normalización de parches, contraste, ejemplos
visibles y batería).

Independencia: cada candidato recibe SOLO el contrato, los ejemplos visibles, el código defectuoso (zonas
autorizadas) y la discrepancia sintética, más lo que produjo SU propia cadena (su diagnóstico o su plan). Nunca los
diagnósticos, planes, parches ni resultados de otro candidato. Las inferencias van una por vez (una GPU); la
independencia no depende de la simultaneidad.

Cada parche se aplica sobre una COPIA del código defectuoso ORIGINAL (un texto inmutable; se verifica su huella antes
y después de cada candidato). Torneo, en este orden y sin saltear etapas:
  1. validez del parche y zonas autorizadas (con la normalización segura de la sangría);
  2. compilación y ejecución segura en el sandbox (bwrap, seccomp, límites);
  3. corrección del caso sintético;
  4. ejemplos visibles;
  5. batería independiente de nueve casos (solo si pasó 1–4; una vez por PROGRAMA distinto).
Éxito: al menos un candidato corrige el caso sintético y aprueba 9/9. Ganan TODOS los que lo logran; el orden entre
ellos es un desempate determinista (menos líneas cambiadas, sin normalización, letra) que NO afirma que el parche más
chico sea el más correcto. Duplicados: misma respuesta de parche o MISMO PROGRAMA resultante (árbol sintáctico).
Presupuesto: MAX_LLAMADAS inferencias, MAX_PARCHES parches y 2400 s; lo que no entra queda «no ejecutado».
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

from .. import prompts
from . import contraste as CT
from . import parches as PA
from . import reparacion_coordinada as RC
from . import reparacion_coordinada_8 as RC8
from . import reparacion_coordinada_10 as RC10
from . import reparacion_coordinada_11 as RC11
from . import reparacion_guiada as RGD
from . import revision_existente as RE
from .misiones_programacion import MISIONES, VALIDADORES

VERSION_12 = "experimento 7.12: torneo de seis soluciones independientes (gemma y qwen, estrategias distintas)"
CLASIFICACION_12 = "reparación por torneo de candidatos independientes, comprobada por ejecución"
DESEMPATE = ("menos líneas cambiadas; luego sin normalización de la sangría; luego la letra. Es un orden "
             "determinista, no una afirmación de que el parche más chico sea el más correcto")

# Las estrategias: instrucciones distintas, ninguna con la solución. «gemma»/«qwen» se resuelven a los modelos dados.
ESTRATEGIAS = [
    {"letra": "A", "nombre": "qwen parchea directamente, buscando el cambio mínimo",
     "pasos": [("parche", "qwen")],
     "instruccion": "Buscá el cambio más chico que haga que el caso dé lo que pide el contrato sin romper los ejemplos."},
    {"letra": "B", "nombre": "qwen parchea directamente, siguiendo la traza del caso",
     "pasos": [("parche", "qwen")],
     "instruccion": "Antes de proponer el parche, seguí la entrada del caso línea por línea y explicá en "
                    "«justificacion» en qué paso el estado del programa se aparta de lo que pide el contrato."},
    {"letra": "C", "nombre": "gemma diagnostica (causa y comportamiento a preservar) y qwen parchea",
     "pasos": [("diagnostico", "gemma"), ("parche", "qwen")],
     "instruccion": "Corregí la causa del diagnóstico sin romper los comportamientos que hay que preservar."},
    {"letra": "D", "nombre": "gemma localiza origen y manifestación y qwen parchea",
     "pasos": [("localizacion", "gemma"), ("parche", "qwen")],
     "instruccion": "Intervení en el origen del defecto, no en el punto donde se manifiesta."},
    {"letra": "E", "nombre": "gemma parchea directamente",
     "pasos": [("parche", "gemma")],
     "instruccion": "Revisá qué estado deja cada rama en las estructuras de datos y corregí el que viola el contrato."},
    {"letra": "F", "nombre": "qwen planifica y después parchea su propio plan",
     "pasos": [("plan", "qwen"), ("parche", "qwen")],
     "instruccion": "Implementá tu plan con el parche mínimo."},
]
MAX_LLAMADAS = sum(len(e["pasos"]) for e in ESTRATEGIAS)      # 9: un intento por etapa, sin reintentos
MAX_PARCHES = len(ESTRATEGIAS)
PEDIDO_PARCHE_12 = "Proponé el parche y respondé solo con el JSON pedido. No escribas el programa completo."
ETAPAS = ("1_parche_valido_y_en_zona", "2_compila_y_ejecuta_en_sandbox", "3_caso_sintetico_corregido",
          "4_ejemplos_visibles", "5_bateria_9_9")


def _base(f: dict, disc: str) -> str:
    m = MISIONES[f["mision"]][0]()
    spec = m["spec"]
    return (f"CONTRATO:\n{m['description']}\n\n{prompts._sig(spec)}\n\n"
            f"Ejemplos visibles:\n{prompts._examples(spec, 3000) or '(sin ejemplos visibles)'}\n\n"
            "CÓDIGO (las zonas que se pueden cambiar y un contexto de solo lectura; los números de línea NO son parte "
            f"del código):\n\n{PA.texto_zonas(f['codigo'], spec['function'], {'zonas': f['zonas']})}\n\n{disc}")


def mensajes_parche(f: dict, disc: str, estrategia: dict, previo: str = "") -> list[dict]:
    usuario = (_base(f, disc) + (f"\n\n{previo}" if previo else "")
               + f"\n\nESTRATEGIA: {estrategia['instruccion']}\n\n{PEDIDO_PARCHE_12}")
    return [{"role": "system", "content": PA.SISTEMA_PARCHE}, {"role": "user", "content": usuario}]


def mensajes_plan(f: dict, disc: str) -> list[dict]:
    return [{"role": "system", "content": RC11.SISTEMA_PLANIFICADOR_11},
            {"role": "user", "content": _base(f, disc) + f"\n\n{RC10.PEDIDO_PLAN}"}]


def carpeta(carpeta_lab, corrida):
    try:
        return RGD.carpeta_de_reparacion(carpeta_lab, corrida, "experimento-7-12")
    except RGD.ErrorReparacion as e:
        raise RC.ErrorCoordinada(str(e)) from None


def _huella(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def correr(destino, f: dict, executor, cliente, gate, diagnosticador, programador, max_segundos: float = 2400.0,
           cancel: threading.Event | None = None) -> dict:
    s = RC10._Sesion(destino, f, cliente, gate, max_segundos, cancel)
    funcion = s.spec["function"]
    fabrica, referencia = MISIONES[f["mision"]]
    contraste = CT.desde_mision(fabrica, referencia, VALIDADORES[f["mision"]])
    ORIGINAL = f["codigo"]                        # nunca se modifica: cada candidato parte de este texto
    huella_original = _huella(ORIGINAL)
    ct = RC.discrepancia(executor, f)
    esperado_caso = ct["referencia"]["salida"]
    salida_defectuosa = ct["candidato"]["salida"]
    disc = RC.texto_discrepancia(funcion, ct)
    modelos = {"gemma": diagnosticador, "qwen": programador}
    inf = {"experimento": VERSION_12, "corrida": f["corrida"], "mision": f["mision"], "zonas": f["zonas"],
           "modelos": {"gemma": diagnosticador.name, "qwen": programador.name},
           "limites": {"llamadas": MAX_LLAMADAS, "parches": MAX_PARCHES, "segundos": max_segundos},
           "archivos_leidos": f["archivos"], "huella_del_codigo_original": huella_original,
           "candidatos": [], "inicio": time.time()}
    claves_parche, programas, baterias = {}, {}, {}
    parches_usados = 0
    for e in ESTRATEGIAS:
        L = e["letra"]
        cand = {"letra": L, "estrategia": e["nombre"], "instruccion": e["instruccion"],
                "pasos": [{"etapa": p, "modelo": modelos[m].name} for p, m in e["pasos"]],
                "huella_base": huella_original, "etapas": {k: None for k in ETAPAS}, "motivo": None,
                "duplicado_de": None, "llamadas": []}
        inf["candidatos"].append(cand)
        t_cand = time.monotonic()
        previo, parche_txt = "", None
        for etapa, quien in e["pasos"]:
            if len(s.llamadas) >= MAX_LLAMADAS or (motivo := s.sin_tiempo()):
                cand["motivo"] = "no ejecutado: " + (motivo if len(s.llamadas) < MAX_LLAMADAS else
                                                     f"límite de {MAX_LLAMADAS} llamadas")
                break
            if etapa == "parche":
                if parches_usados >= MAX_PARCHES:
                    cand["motivo"] = f"no ejecutado: límite de {MAX_PARCHES} parches"
                    break
                parches_usados += 1
                msgs = mensajes_parche(f, disc, e, previo)
            elif etapa == "diagnostico":
                msgs = RC8.mensajes_diagnostico(f, disc)
            elif etapa == "localizacion":
                msgs = RC11.mensajes_diagnostico(f, disc)
            else:
                msgs = mensajes_plan(f, disc)
            n = len(s.llamadas)
            gen, error = s.llamar(f"candidato {L}: {etapa}", modelos[quien], msgs, f"candidato-{L}/{etapa}")
            cand["llamadas"] += s.llamadas[n:]
            if gen is None:
                cand["motivo"] = f"{etapa}: {error}"
                break
            if etapa == "parche":
                parche_txt = gen.text
                continue
            try:
                if etapa == "diagnostico":
                    d = RC8.interpretar(gen.text, ORIGINAL, f["zonas"], funcion)
                    cand["diagnostico"] = d
                    previo = f"DIAGNÓSTICO (de tu propio equipo, otra IA; no está verificado):\n" \
                             f"{RC8.texto_para_el_programador(d)}"
                elif etapa == "localizacion":
                    d = RC11.interpretar_diagnostico(gen.text, ORIGINAL, f["zonas"], funcion)
                    cand["diagnostico"] = d
                    previo = f"LOCALIZACIÓN DEL DEFECTO (de tu propio equipo, otra IA; no está verificada):\n" \
                             f"{RC11.texto_diagnostico(d)}"
                else:
                    pl = RC11.interpretar_plan(gen.text, ORIGINAL, f["zonas"], funcion, False)
                    cand["plan"] = pl
                    previo = f"TU PLAN:\n{RC11.texto_plan(pl)}"
            except ValueError as ex:
                cand["motivo"] = f"{etapa} inválido: {ex}"
                break
        if parche_txt is not None:
            _evaluar(cand, parche_txt, ORIGINAL, f, s, executor, contraste, esperado_caso, salida_defectuosa,
                     claves_parche, programas, baterias, fabrica)
        if _huella(ORIGINAL) != huella_original:          # defensa: el original es inmutable
            raise RuntimeError("el código original cambió durante el torneo")
        cand["huella_original_despues"] = _huella(ORIGINAL)
        cand["segundos"] = round(time.monotonic() - t_cand, 1)
        s.guardar(f"candidato-{L}/candidato.json", cand)
    return _cerrar(inf, s)


def _evaluar(cand, texto, ORIGINAL, f, s, executor, contraste, esperado_caso, salida_defectuosa, claves_parche,
             programas, baterias, fabrica):
    L = cand["letra"]
    funcion = s.spec["function"]
    et = cand["etapas"]
    # 1. Validez y zonas (sobre una COPIA: aplicar devuelve un texto nuevo; ORIGINAL no se toca).
    try:
        parche = PA.interpretar_parche(texto, funcion)
        cand["parche"] = parche
        k = PA.clave(parche)
        if k in claves_parche:
            cand["duplicado_de"] = claves_parche[k]
            cand["tipo_de_duplicado"] = "la misma respuesta de parche"
        claves_parche.setdefault(k, L)
        try:
            nuevo, aplicacion = PA.aplicar(ORIGINAL, parche, f["zonas"], s.spec)
        except PA.ParcheRechazado as ex:
            if "no aparece en el código" not in str(ex):
                raise
            cand["rechazo_literal"] = str(ex)
            normalizado, just = RGD.normalizar_sangria(ORIGINAL, parche, f["zonas"])
            cand.update(parche_normalizado=normalizado, normalizacion=just)
            nuevo, aplicacion = PA.aplicar(ORIGINAL, normalizado, f["zonas"], s.spec)
    except PA.ParcheRechazado as ex:
        et["1_parche_valido_y_en_zona"] = False
        cand["motivo"] = f"rechazado en la etapa 1: {ex}"
        return
    et["1_parche_valido_y_en_zona"] = True
    cambio = RE.cambio_de_codigo(ORIGINAL, nuevo)
    prog = RE.programa(nuevo)
    cand.update(aplicacion=aplicacion, lineas_cambiadas=cambio["lineas_agregadas"] + cambio["lineas_quitadas"],
                huella_programa=prog, huella_solucion=_huella(nuevo), modifica_el_programa=prog != RE.programa(ORIGINAL))
    s.guardar(f"candidato-{L}/solucion.py", nuevo)
    if prog in programas and not cand["duplicado_de"]:
        cand["duplicado_de"] = programas[prog]
        cand["tipo_de_duplicado"] = "el mismo programa resultante (otro texto)"
    programas.setdefault(prog, L)
    # 2. Compilación (ya validada) y ejecución segura en el sandbox.
    ctp = CT.contrastar(executor, contraste, nuevo, f["caso"], None)
    c = ctp["candidato"] or {}
    ejecuta = c.get("ejecucion") in ("completa", "excepcion")
    et["2_compila_y_ejecuta_en_sandbox"] = ejecuta
    if not ejecuta:
        cand["motivo"] = f"rechazado en la etapa 2: {c.get('ejecucion') or ctp['discrepancia']} ({ctp['motivo']})"
        return
    cand["salida_en_el_caso"] = c.get("salida")
    cand["misma_salida_que_el_defectuoso"] = c.get("salida") == salida_defectuosa
    # 3. Caso sintético.
    et["3_caso_sintetico_corregido"] = ctp["discrepancia"] == "no_observada"
    if not et["3_caso_sintetico_corregido"]:
        cand["motivo"] = "rechazado en la etapa 3: la discrepancia del caso sintético persiste"
        cand["diferencia_en_el_caso"] = RE.comparar_salidas(esperado_caso, c.get("salida")
                                                            if isinstance(c.get("salida"), dict) else None)
        return
    # 4. Ejemplos visibles.
    vis = RGD._visibles(executor, nuevo, s.spec)
    cand["visibles"] = {k: vis[k] for k in ("ok", "aprobados", "total")}
    et["4_ejemplos_visibles"] = vis["ok"]
    if not vis["ok"]:
        cand["motivo"] = "rechazado en la etapa 4: no cumple los ejemplos visibles"
        return
    # 5. Batería independiente (una vez por programa distinto).
    if prog in baterias:
        ver, cand["bateria_reutilizada_de"] = baterias[prog]
    else:
        ver = RE._bateria(executor, nuevo, fabrica())
        baterias[prog] = (ver, L)
    cand["bateria"] = {k: ver.get(k) for k in ("ok", "aprobados", "total", "ejecucion")}
    et["5_bateria_9_9"] = bool(ver["ok"])
    cand["motivo"] = ("superó las cinco etapas" if ver["ok"] else
                      f"rechazado en la etapa 5: batería {ver.get('aprobados')}/{ver.get('total')}")


def tabla(candidatos: list[dict]) -> list[dict]:
    return [{"letra": c["letra"], "estrategia": c["estrategia"], **{k: c["etapas"][k] for k in ETAPAS},
             "duplicado_de": c["duplicado_de"], "lineas_cambiadas": c.get("lineas_cambiadas"),
             "sangria_normalizada": "parche_normalizado" in c, "huella_programa": (c.get("huella_programa") or "")[:12]
             or None, "llamadas": len(c["llamadas"]),
             "segundos_de_inferencia": round(sum(x["segundos"] or 0 for x in c["llamadas"]), 1), "motivo": c["motivo"]}
            for c in candidatos]


def _cerrar(inf: dict, s) -> dict:
    cands = inf["candidatos"]
    ganadores = [c for c in cands if c["etapas"]["3_caso_sintetico_corregido"] and c["etapas"]["5_bateria_9_9"]]
    orden = sorted(ganadores, key=lambda c: (c["lineas_cambiadas"], "parche_normalizado" in c, c["letra"]))
    inf["tabla"] = tabla(cands)
    inf["ganadores"] = [c["letra"] for c in orden]
    inf["ganador_por_desempate"] = orden[0]["letra"] if orden else None
    inf["criterio_de_desempate"] = DESEMPATE
    inf["programas_distintos"] = len({c["huella_programa"] for c in cands if c.get("huella_programa")})
    inf["duplicados"] = [{"candidato": c["letra"], "de": c["duplicado_de"], "tipo": c.get("tipo_de_duplicado")}
                         for c in cands if c["duplicado_de"]]
    inf["auditoria_de_fugas"] = RE.auditar(s.pedidos, s.spec, s.f["defecto"], s.descripcion)
    inf["llamadas"] = s.llamadas
    inf["consumo"] = {"llamadas": len(s.llamadas), "segundos": round(s.usados(), 1),
                      "parches": sum(1 for c in cands for p in c["llamadas"] if p["rol"].endswith(": parche"))}
    inf["exito"] = bool(ganadores) and inf["auditoria_de_fugas"]["sin_fugas"]
    inf["clasificacion"] = (CLASIFICACION_12 + f" (ganadores: {', '.join(inf['ganadores'])})") if inf["exito"] else None
    inf["motivo"] = (f"{len(ganadores)} candidato(s) corrigieron el caso y aprobaron 9/9" if ganadores else
                     "ningún candidato corrigió el caso sintético y aprobó la batería 9/9")
    inf["fin"] = time.time()
    s.guardar("informe.json", inf)
    return inf
