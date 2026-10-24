"""Experimento 7.10: PLANIFICACIÓN CAUSAL antes de programar (OFICINA.md, «Experimento 7.10»).

En el 7.9 Gemma identificó la causa y rechazó dos parches defectuosos, pero Qwen agotó sus tres intentos. El 7.10
separa la planificación de la implementación. Dos modelos, cada rol una invocación independiente:
1. DIAGNOSTICADORA (gemma): como en el 7.8/7.9 (causa, mecanismo, comportamiento defectuoso y a preservar).
2. PLANIFICADOR (qwen): antes de escribir código, un plan JSON: estado incorrecto, operación que lo produce,
   comportamiento a cambiar, comportamiento a preservar y líneas a modificar (copiadas del código, dentro de las zonas)
   con su porqué; desde el segundo ciclo, además, qué cambia respecto del intento anterior.
3. REVISORA DEL PLAN (gemma): ¿el plan elimina la causa diagnosticada y preserva el comportamiento? Decisión
   estructurada y crítica causal, sin escribir la solución. Si rechaza, la crítica llega íntegra al planificador, que
   puede revisar el plan (a lo sumo MAX_PLANES_POR_CICLO planes por ciclo). Una revisión inválida no decide nada: se
   registra y se sigue (las comprobaciones deterministas deciden).
4. PROGRAMADOR (qwen): un parche mínimo que implementa el plan aprobado (JSON, zonas autorizadas).
5. COORDINADOR: valida y aplica el parche (normalización de la sangría incluida), caso sintético, ejemplos visibles
   y la batería independiente una sola vez. La aprobación del plan no prueba nada ni evita ninguna comprobación.
Si el parche falla, el ciclo siguiente planifica con la DISCREPANCIA OBSERVABLE (la del 7.8) o el motivo del rechazo.
A lo sumo MAX_CICLOS ciclos y 2400 s en total. Nada de casos ocultos, referencia, defecto ni causa del equipo en los
pedidos (auditoría de fugas en cada uno). Las evaluaciones «correcto» del diagnóstico y del plan son POSTERIORES e
indicativas (no llegan a ningún modelo ni deciden nada).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

from .. import prompts
from ..ollama import OllamaError
from . import contraste as CT
from . import parches as PA
from . import reparacion_coordinada as RC
from . import reparacion_coordinada_8 as RC8
from . import reparacion_coordinada_9 as RC9
from . import reparacion_guiada as RGD
from . import revision_existente as RE
from . import revisor as RV
from . import sonda_comprension as SC
from .misiones_programacion import MISIONES, VALIDADORES

VERSION_10 = ("experimento 7.10: diagnóstico (gemma), plan (qwen), revisión del plan (gemma) y parche (qwen) "
              "coordinados, con normalización de sangría")
CLASIFICACION_10 = "diagnóstico, planificación, revisión e implementación automáticos coordinados"
MAX_CICLOS = 3
MAX_PLANES_POR_CICLO = 2
MAX_CAMPO = 600
MAX_LINEAS_PLAN = 6

SISTEMA_PLANIFICADOR = (
    "Sos el PLANIFICADOR de un equipo de programación. Antes de que nadie escriba código, hacé un PLAN de la "
    "corrección a partir del diagnóstico. No escribas código ni el parche: solo el plan.\n"
    "Respondé SOLO con un objeto JSON con esta forma:\n"
    '{"estado_incorrecto": "<qué estado del programa es incorrecto>", "operacion_que_lo_produce": "<qué operación del '
    'código produce ese estado>", "comportamiento_a_cambiar": "<qué comportamiento tiene que cambiar>", '
    '"comportamiento_a_preservar": ["<qué comportamiento tiene que seguir igual>"], "lineas_a_modificar": [{"linea": '
    '"<línea copiada LITERALMENTE del código, sin el número>", "por_que": "<por qué hay que modificarla>"}], '
    '"cambio_respecto_del_anterior": "<si hubo un intento anterior: qué cambia en este plan y por qué>"}')
PEDIDO_PLAN = "Hacé el plan y respondé solo con el JSON pedido, sin código."

SISTEMA_REVISORA_PLAN = (
    "Sos la REVISORA de planes de un equipo (un rol distinto de quien diagnosticó y de quien planificó). Recibís el "
    "contrato, el código, un diagnóstico de la causa y el PLAN de corrección de otro programador. No tenés resultados "
    "de pruebas: razoná sobre el código.\n"
    "Evaluá si, aplicado tal como está descrito, el plan (1) elimina la CAUSA diagnosticada (no solo un síntoma) y "
    "(2) conserva el comportamiento correcto que hay que preservar. No propongas tu propia solución ni escribas "
    "código: evaluá el plan.\n"
    "Respondé SOLO con un objeto JSON con esta forma:\n"
    '{"decision": "aprobar | rechazar", "elimina_la_causa": true | false, "preserva_comportamiento": true | false, '
    '"justificacion_causal": "<por qué el plan elimina o no la causa, y qué comportamiento conserva o rompe>", '
    '"critica": "<si rechazás: qué falla en el plan, en términos causales>"}\n'
    "Aprobá solo si las dos cosas se cumplen.")
PEDIDO_REVISION_PLAN = "Revisá el plan y respondé solo con el JSON pedido."
PEDIDO_PARCHE = ("Implementá el PLAN APROBADO con un parche mínimo: modificá solo lo que el plan indica y respondé "
                 "solo con el JSON pedido. En «justificacion» explicá cómo el parche sigue el plan. No escribas el "
                 "programa completo.")


class ErrorPlan(Exception):
    pass


def _base(f: dict) -> str:
    m = MISIONES[f["mision"]][0]()
    spec = m["spec"]
    return (f"CONTRATO:\n{m['description']}\n\n{prompts._sig(spec)}\n\n"
            f"Ejemplos visibles:\n{prompts._examples(spec, 3000) or '(sin ejemplos visibles)'}\n\n"
            "CÓDIGO (las zonas que se pueden cambiar y un contexto de solo lectura; los números de línea NO son parte "
            f"del código):\n\n{PA.texto_zonas(f['codigo'], spec['function'], {'zonas': f['zonas']})}\n\n"
            f"DIAGNÓSTICO DE LA CAUSA (de la diagnosticadora, otra IA; no está verificado):\n{f['diagnostico_texto']}")


# Plan ---------------------------------------------------------------------------------------------------------------

def mensajes_plan(f: dict, contexto: str = "") -> list[dict]:
    usuario = _base(f) + (f"\n\n{contexto}" if contexto else "") + f"\n\n{PEDIDO_PLAN}"
    return [{"role": "system", "content": SISTEMA_PLANIFICADOR}, {"role": "user", "content": usuario}]


def interpretar_plan(texto: str, codigo: str, zonas, funcion: str, ciclo: int) -> dict:
    """ValueError si el plan es insuficiente. Las líneas a modificar tienen que existir dentro de las zonas."""
    t = (texto or "").strip()
    if re.search(rf"\bdef\s+{re.escape(funcion)}\s*\(", t):
        raise ValueError("incluyó el programa: el plan no lleva código")
    mt = re.search(r"\{.*\}", t, re.S)
    if not mt:
        raise ValueError("la respuesta no contiene un objeto JSON")
    try:
        d = json.loads(mt.group(0))
    except ValueError as e:
        raise ValueError(f"JSON ilegible: {e}") from None
    if not isinstance(d, dict):
        raise ValueError("la respuesta no es un objeto JSON")
    out = {}
    for c in ("estado_incorrecto", "operacion_que_lo_produce", "comportamiento_a_cambiar"):
        out[c] = RV._limpio(d.get(c), MAX_CAMPO) if isinstance(d.get(c), str) else ""
        if not out[c]:
            raise ValueError(f"falta «{c}»")
    pres = d.get("comportamiento_a_preservar")
    pres = [pres] if isinstance(pres, str) else pres if isinstance(pres, list) else []
    out["comportamiento_a_preservar"] = [RV._limpio(x, MAX_CAMPO) for x in pres if isinstance(x, str) and x.strip()][:6]
    if not out["comportamiento_a_preservar"]:
        raise ValueError("falta «comportamiento_a_preservar»")
    lineas = []
    for x in (d.get("lineas_a_modificar") or [])[:MAX_LINEAS_PLAN]:
        if not isinstance(x, dict) or not isinstance(x.get("linea"), str) or not x["linea"].strip():
            continue
        linea = re.sub(r"^\s*\d+\|", "", x["linea"].strip()).strip()[:RV.MAX_CITA]
        ls = PA.lineas_de_cita(codigo, linea)
        lineas.append({"linea": linea, "lineas": ls, "por_que": RV._limpio(x.get("por_que"), MAX_CAMPO),
                       "en_zona": bool(ls) and all(any(a <= n <= b for a, b in zonas) for n in ls)})
    if not any(x["en_zona"] for x in lineas):
        raise ValueError("«lineas_a_modificar» tiene que nombrar al menos una línea que exista dentro de las zonas")
    out["lineas_a_modificar"] = lineas
    cambio = d.get("cambio_respecto_del_anterior")
    out["cambio_respecto_del_anterior"] = RV._limpio(cambio, MAX_CAMPO) if isinstance(cambio, str) else ""
    if ciclo > 1 and not out["cambio_respecto_del_anterior"]:
        raise ValueError("falta «cambio_respecto_del_anterior» (hubo un intento anterior)")
    return out


def texto_plan(p: dict) -> str:
    l = [f"Estado incorrecto: {p['estado_incorrecto']}",
         f"Operación que lo produce: {p['operacion_que_lo_produce']}",
         f"Comportamiento a cambiar: {p['comportamiento_a_cambiar']}",
         "Comportamiento a preservar:"] + [f"    - {x}" for x in p["comportamiento_a_preservar"]]
    l.append("Líneas a modificar:")
    l += [f"    - «{x['linea']}»" + (f" (línea {', '.join(map(str, x['lineas']))})" if x["en_zona"] else
                                     " (no está en las zonas: no se puede modificar)") + f": {x['por_que']}"
          for x in p["lineas_a_modificar"]]
    if p["cambio_respecto_del_anterior"]:
        l.append(f"Cambio respecto del intento anterior: {p['cambio_respecto_del_anterior']}")
    return "\n".join(l)


def clave_plan(p: dict) -> str:
    norm = {k: (" ".join(v.split()).lower() if isinstance(v, str) else v) for k, v in p.items()
            if k != "cambio_respecto_del_anterior"}
    norm["lineas_a_modificar"] = sorted(" ".join(x["linea"].split()) for x in p["lineas_a_modificar"])
    norm["comportamiento_a_preservar"] = sorted(" ".join(x.split()).lower() for x in p["comportamiento_a_preservar"])
    return hashlib.sha256(json.dumps(norm, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


# Revisión del plan y parche -----------------------------------------------------------------------------------------

def mensajes_revision_plan(f: dict, plan: dict) -> list[dict]:
    usuario = (_base(f) + f"\n\nPLAN DE CORRECCIÓN PROPUESTO POR EL PLANIFICADOR:\n{texto_plan(plan)}\n\n"
               + PEDIDO_REVISION_PLAN)
    return [{"role": "system", "content": SISTEMA_REVISORA_PLAN}, {"role": "user", "content": usuario}]


def texto_critica_plan(k: int, r: dict) -> str:
    return (f"REVISIÓN DE TU PLAN {k} (otra IA, la revisora; NO es un resultado de pruebas): RECHAZADO.\n"
            f"¿Elimina la causa?: {RC9._si_no(r['elimina_la_causa'])} · ¿Preserva el comportamiento?: "
            f"{RC9._si_no(r['preserva_comportamiento'])}\n"
            f"Justificación causal: {r['justificacion_causal']}\n"
            f"Crítica: {r['critica']}")


def mensajes_parche(f: dict, plan: dict, aviso: str = "") -> list[dict]:
    usuario = (_base(f) + f"\n\nPLAN APROBADO (lo que tenés que implementar):\n{texto_plan(plan)}\n\n"
               + (f"AVISO DEL COORDINADOR:\n{aviso}\n\n" if aviso else "") + PEDIDO_PARCHE)
    return [{"role": "system", "content": PA.SISTEMA_PARCHE}, {"role": "user", "content": usuario}]


def coherencia(plan: dict, aplicacion: list[dict]) -> dict:
    """¿El parche toca las líneas del plan? Cada cambio tiene que caer sobre (o junto a) una línea planificada."""
    planificadas = {n for x in plan["lineas_a_modificar"] if x["en_zona"] for n in x["lineas"]}
    por_cambio = []
    for a in aplicacion:
        ini, fin = a["lineas"]
        por_cambio.append(any(ini - 1 <= n <= fin + 1 for n in planificadas))
    return {"lineas_planificadas": sorted(planificadas), "lineas_cambiadas": [a["lineas"] for a in aplicacion],
            "coherente": bool(por_cambio) and all(por_cambio)}


# Recorrido ----------------------------------------------------------------------------------------------------------

def carpeta(carpeta_lab, corrida):
    try:
        return RGD.carpeta_de_reparacion(carpeta_lab, corrida, "experimento-7-10")
    except RGD.ErrorReparacion as e:
        raise RC.ErrorCoordinada(str(e)) from None


class _Sesion:
    """Llamadas, tiempos, registro y auditoría de una corrida."""

    def __init__(self, destino: Path, f: dict, cliente, gate, max_segundos: float, cancel):
        self.destino, self.f, self.cliente, self.gate = Path(destino), f, cliente, gate
        self.max_segundos, self.cancel = max_segundos, cancel
        self.llamadas: list[dict] = []
        self.pedidos: list[list[dict]] = []
        m = MISIONES[f["mision"]][0]()
        self.spec, self.descripcion = m["spec"], m["description"]

    def guardar(self, rel, contenido):
        p = self.destino / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(contenido if isinstance(contenido, str) else
                     json.dumps(contenido, ensure_ascii=False, indent=2, default=str))

    def usados(self) -> float:
        return sum(ll["segundos"] or 0 for ll in self.llamadas)

    def sin_tiempo(self) -> str | None:
        u = self.usados()
        return (f"límite de tiempo de inferencia ({u:.0f} s de {self.max_segundos:.0f} s)"
                if u >= self.max_segundos else None)

    def llamar(self, rol: str, modelo, msgs: list[dict], carpeta_rel: str):
        """Una inferencia (JSON). Devuelve (gen | None, motivo | None). Audita el pedido antes de enviarlo."""
        self.pedidos.append(msgs)
        self.guardar(f"{carpeta_rel}/pedido.json", msgs)
        auditoria = RE.auditar([msgs], self.spec, self.f["defecto"], self.descripcion)
        if not auditoria["sin_fugas"]:
            return None, "auditoría: información prohibida en el pedido; no se envió (" + ", ".join(
                auditoria["hallazgos"]) + ")"
        t0 = time.monotonic()
        try:
            self.gate.acquire(rol, self.cancel)
            try:
                gen = self.cliente.chat(modelo, msgs, self.cancel, fmt="json")
            finally:
                self.gate.release()
        except (OllamaError, OSError) as e:
            self.llamadas.append({"rol": rol, "modelo": modelo.name, "carpeta": carpeta_rel,
                                  "segundos": round(time.monotonic() - t0, 1), "error": str(e)})
            return None, f"sin respuesta: {e}"
        self.llamadas.append({"rol": rol, "modelo": modelo.name, "carpeta": carpeta_rel,
                              "segundos": round(time.monotonic() - t0, 1), "tokens_prompt": gen.prompt_tokens,
                              "tokens_generados": gen.eval_tokens, "fin": gen.done_reason,
                              "cortada": bool(gen.truncated)})
        self.guardar(f"{carpeta_rel}/respuesta.txt", gen.text)
        return gen, None


def correr(destino: Path, f: dict, executor, cliente, gate, diagnosticador, programador, max_segundos: float = 2400.0,
           cancel: threading.Event | None = None) -> dict:
    s = _Sesion(destino, f, cliente, gate, max_segundos, cancel)
    funcion = s.spec["function"]
    fabrica, referencia = MISIONES[f["mision"]]
    contraste = CT.desde_mision(fabrica, referencia, VALIDADORES[f["mision"]])
    codigo = f["codigo"]
    original = RE.programa(codigo)
    ct = RC.discrepancia(executor, f)
    esperado = ct["referencia"]["salida"]
    inf = {"experimento": VERSION_10, "corrida": f["corrida"], "mision": f["mision"], "zonas": f["zonas"],
           "diagnosticador": diagnosticador.name, "programador": programador.name, "max_ciclos": MAX_CICLOS,
           "max_planes_por_ciclo": MAX_PLANES_POR_CICLO, "max_segundos": max_segundos,
           "archivos_leidos": f["archivos"], "diagnosticos": [], "diagnostico_usado": None, "ciclos": [],
           "verificacion": None, "detenido": None, "inicio": time.time()}

    # 1. Diagnóstico (el del 7.8) ------------------------------------------------------------------------------------
    disc = RC.texto_discrepancia(funcion, ct)
    nota, valido = "", None
    for k in range(1, RC.MAX_DIAGNOSTICOS + 1):
        if (motivo := s.sin_tiempo()):
            inf["detenido"] = motivo
            break
        gen, error = s.llamar("diagnosticadora", diagnosticador, RC8.mensajes_diagnostico(f, disc, nota),
                              f"diagnostico-{k}")
        reg = {"intento": k, "valido": False}
        inf["diagnosticos"].append(reg)
        if gen is None:
            reg["motivo"] = error
            inf["detenido"] = f"la diagnosticadora no produjo un diagnóstico: {error}"
            break
        try:
            reg["diagnostico"] = RC8.interpretar(gen.text, codigo, f["zonas"], funcion)
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
        return _cerrar(inf, s, None)
    f = dict(f, diagnostico_texto=RC8.texto_para_el_programador(valido))
    inf["diagnostico_usado"] = {"texto": f["diagnostico_texto"],
                                "huella": hashlib.sha256(f["diagnostico_texto"].encode()).hexdigest()}

    # 2-5. Ciclos de plan → revisión → parche → verificación -----------------------------------------------------------
    contexto, claves_planes, claves_parches, avisado = "", [], [], False
    for c in range(1, MAX_CICLOS + 1):
        ciclo = {"ciclo": c, "planes": [], "plan_aprobado": None, "parche": None}
        inf["ciclos"].append(ciclo)
        aprobado, critica = None, ""
        for k in range(1, MAX_PLANES_POR_CICLO + 1):
            if (motivo := s.sin_tiempo()):
                inf["detenido"] = motivo
                break
            ctx = "\n\n".join(x for x in (contexto, critica) if x)
            gen, error = s.llamar("planificador", programador, mensajes_plan(f, ctx), f"ciclo-{c}/plan-{k}")
            pl = {"plan": k, "valido": False, "contexto_recibido": ctx or None}
            ciclo["planes"].append(pl)
            if gen is None:
                pl["motivo"] = error
                inf["detenido"] = f"el planificador no respondió: {error}"
                break
            try:
                plan = interpretar_plan(gen.text, codigo, f["zonas"], funcion, c)
            except ValueError as e:
                pl["motivo"] = str(e)
                critica = f"Tu plan {k} no se pudo usar ({e}). Respondé con el JSON completo."
                s.guardar(f"ciclo-{c}/plan-{k}/plan.json", pl)
                continue
            pl.update(valido=True, contenido=plan)
            kp = clave_plan(plan)
            pl["repite_el_plan"] = next((x for x, y in claves_planes if y == kp), None)
            claves_planes.append((f"ciclo {c}, plan {k}", kp))
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
                critica = texto_critica_plan(k, rv) + (
                    f"\nTu plan es igual al anterior ({pl['repite_el_plan']}): repetirlo no aporta información nueva."
                    if pl["repite_el_plan"] else "")
                pl["critica_enviada"] = critica
                continue
            aprobado = plan          # aprobado, o revisión inválida: deciden las comprobaciones deterministas
            ciclo["plan_aprobado"] = {"plan": k, "por": "revisora" if rv["decision"] == "aprobar" else
                                      "revisión inválida (sin decisión)"}
            break
        if inf["detenido"]:
            break
        if aprobado is None:
            ciclo["sin_plan_aprobado"] = True
            contexto = ("RESULTADO DEL CICLO ANTERIOR: ningún plan fue aprobado. Última revisión:\n" + critica
                        if critica else "")
            continue
        if (motivo := s.sin_tiempo()):
            inf["detenido"] = motivo
            break
        aviso = ("REPETICIÓN: en un ciclo anterior propusiste un parche igual." if avisado else "")
        gen, error = s.llamar("programador", programador, mensajes_parche(f, aprobado, aviso), f"ciclo-{c}/parche")
        pa = {"aplicado": False}
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
            pa.update(aplicado=True, aplicacion=aplicacion, coherencia_con_el_plan=coherencia(aprobado, aplicacion))
        except PA.ParcheRechazado as e:
            pa["motivo"] = str(e)
        s.guardar(f"ciclo-{c}/parche/parche.json", pa)
        if pa.get("repite_el_parche_del_ciclo"):
            if avisado:
                inf["detenido"] = (f"el parche del ciclo {c} repite uno anterior después del aviso: no se gastan más "
                                   "inferencias sin información nueva")
                break
            avisado = True
        if nuevo is None:
            contexto = (f"RESULTADO DEL CICLO {c}: el parche que implementó el plan aprobado fue RECHAZADO por el "
                        f"coordinador: {pa['motivo']}\nEn tu nuevo plan explicá qué cambia respecto de este intento.")
            continue
        s.guardar(f"ciclo-{c}/parche/solucion.py", nuevo)
        ctp = CT.contrastar(executor, contraste, nuevo, f["caso"], None)
        vis = RGD._visibles(executor, nuevo, s.spec)
        d = {"no_observada": "eliminada", "demostrada": "persiste"}.get(ctp["discrepancia"], "inconclusa")
        pa.update(discrepancia_sintetica=d, visibles={k: vis[k] for k in ("ok", "aprobados", "total")},
                  modifica_el_programa=RE.programa(nuevo) != original)
        if d == "eliminada" and vis["ok"]:
            ver = RE._bateria(executor, nuevo, MISIONES[f["mision"]][0]())
            inf["verificacion"] = dict(ver, ciclo=c)
            pa["bateria"] = {k: ver.get(k) for k in ("ok", "aprobados", "total")}
            s.guardar(f"ciclo-{c}/parche/parche.json", pa)
            break
        s.guardar(f"ciclo-{c}/parche/parche.json", pa)
        cand = ctp["candidato"] or {"ejecucion": ctp["discrepancia"], "salida": None, "detalle": ctp["motivo"]}
        retro = RC8.retroalimentacion(c, f["caso"], funcion, esperado, cand, d, vis, "parche_normalizado" in pa)
        s.guardar(f"ciclo-{c}/parche/retroalimentacion.md", retro)
        contexto = (f"RESULTADO DEL CICLO {c} (tu plan aprobado y su parche):\nPlan:\n{texto_plan(aprobado)}\n"
                    f"Resultado:\n{retro}\nEn tu nuevo plan explicá qué cambia respecto de este intento.")
    if not inf["detenido"] and not inf["verificacion"]:
        inf["detenido"] = f"ningún ciclo corrigió el defecto en {len(inf['ciclos'])} ciclo(s)"
    return _cerrar(inf, s, valido)


def _correcto_plan(plan: dict | None) -> bool:
    """POSTERIOR e indicativa: ¿el plan explica que las anulaciones aplicadas no quedan en `aplicados`?"""
    if not plan:
        return False
    texto = "\n".join([plan["estado_incorrecto"], plan["operacion_que_lo_produce"], plan["comportamiento_a_cambiar"],
                       *(x["linea"] + " " + x["por_que"] for x in plan["lineas_a_modificar"])])
    return bool(SC.evaluar(texto)["identifica_ausencia_en_aplicados"])


def medir(inf: dict) -> dict:
    """La medición que pidió el equipo, por ciclo y en total."""
    filas = []
    for c in inf["ciclos"]:
        for p in c["planes"]:
            if not p.get("valido"):
                continue
            correcto = _correcto_plan(p["contenido"])
            d = (p.get("revision") or {}).get("decision")
            p["evaluacion"] = {"plan_causal_correcto_indicativo": correcto, "decision": d,
                               "revision_correcta": (d == "aprobar") == correcto if d else None,
                               "aprobacion_incorrecta": d == "aprobar" and not correcto,
                               "rechazo_injustificado": d == "rechazar" and correcto}
            filas.append(p["evaluacion"])
        pa = c.get("parche") or {}
        if pa:
            aprobado = c.get("plan_aprobado")
            plan_ok = bool(aprobado) and next((p["evaluacion"]["plan_causal_correcto_indicativo"] for p in c["planes"]
                                               if p["plan"] == aprobado["plan"] and "evaluacion" in p), False)
            pa["fallo_de_implementacion"] = bool(plan_ok and pa.get("discrepancia_sintetica") != "eliminada")
    ultimo = next((c for c in reversed(inf["ciclos"]) if c.get("parche")), None)
    pa = (ultimo or {}).get("parche") or {}
    plan_final = next((p for p in (ultimo or {}).get("planes", []) if ultimo.get("plan_aprobado")
                       and p["plan"] == ultimo["plan_aprobado"]["plan"]), None)
    ev = RC.evaluar_diagnostico(next((d["diagnostico"] for d in inf["diagnosticos"] if d["valido"]), None))
    return {
        "diagnostico_correcto": bool(ev["identifica_la_causa"]),
        "plan_causal_correcto": bool(plan_final and plan_final["evaluacion"]["plan_causal_correcto_indicativo"]),
        "revision_del_plan_correcta": bool(plan_final and plan_final["evaluacion"]["revision_correcta"]),
        "parche_coherente_con_el_plan": bool((pa.get("coherencia_con_el_plan") or {}).get("coherente")),
        "parche_aplicado": bool(pa.get("aplicado")),
        "caso_sintetico_corregido": pa.get("discrepancia_sintetica") == "eliminada",
        "bateria_independiente_9_9": bool(inf["verificacion"] and inf["verificacion"]["ok"]),
        "aprobaciones_incorrectas_de_planes": sum(x["aprobacion_incorrecta"] for x in filas),
        "rechazos_injustificados_de_planes": sum(x["rechazo_injustificado"] for x in filas),
        "fallos_de_implementacion": sum(bool((c.get("parche") or {}).get("fallo_de_implementacion"))
                                        for c in inf["ciclos"]),
        "planes_repetidos": sum(bool(p.get("repite_el_plan")) for c in inf["ciclos"] for p in c["planes"]),
        "parches_repetidos": sum(bool((c.get("parche") or {}).get("repite_el_parche_del_ciclo")) for c in inf["ciclos"]),
        "nota": "«correcto» del diagnóstico y del plan: evaluación posterior e indicativa (requiere lectura humana)",
    }


def _cerrar(inf: dict, s: _Sesion, valido) -> dict:
    inf["medicion"] = medir(inf)
    inf["auditoria_de_fugas"] = RE.auditar(s.pedidos, s.spec, s.f["defecto"], s.descripcion)
    inf["llamadas"] = s.llamadas
    inf["consumo"] = {"llamadas": len(s.llamadas), "segundos": round(s.usados(), 1),
                      "por_rol": {r: sum(1 for x in s.llamadas if x["rol"] == r) for r in
                                  ("diagnosticadora", "planificador", "revisora del plan", "programador")}}
    m = inf["medicion"]
    ultimo = next((c for c in reversed(inf["ciclos"]) if c.get("parche")), None)
    aprobado_por_revisora = bool(ultimo and (ultimo.get("plan_aprobado") or {}).get("por") == "revisora")
    inf["exito"] = bool(m["diagnostico_correcto"] and aprobado_por_revisora and m["parche_aplicado"]
                        and m["caso_sintetico_corregido"] and m["bateria_independiente_9_9"]
                        and inf["auditoria_de_fugas"]["sin_fugas"])
    pa = (ultimo or {}).get("parche") or {}
    inf["clasificacion"] = (CLASIFICACION_10 + (" (con normalización de la sangría)" if "parche_normalizado" in pa
                                                else "")) if inf["exito"] else None
    inf["motivo"] = inf["detenido"] or (f"batería independiente {inf['verificacion']['aprobados']}/"
                                        f"{inf['verificacion']['total']} en el ciclo {inf['verificacion']['ciclo']}"
                                        if inf["verificacion"] else None)
    inf["fin"] = time.time()
    s.guardar("informe.json", inf)
    return inf
