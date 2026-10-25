"""OH-DUO-02: detector de estancamiento de agentes programadores, a partir de los eventos de OpenHands.

Solo diagnóstico: lee registros existentes, no los modifica, no detiene ni corrige agentes. No usa pruebas
ocultas ni soluciones de referencia, ni el nombre del modelo: las reglas miran acciones, observaciones y el
estado del código que se deduce de ellas.

Fuentes, en orden de preferencia, para cada ejecución:
  1. eventos persistidos por el SDK (estado/conversacion/<id>/events/event-NNNNN-<id>.json): completos;
  2. resumen de agente.py (estado/eventos.json, o logs/estado__eventos.json en OH-DUO-01): observaciones
     recortadas a 4000 caracteres y sin el código del error de conversación; fidelidad reducida.

Uso:
  python3 estancamiento.py --corrida ~/oh-duo-01/corridas/ensayo-p1 [--umbrales umbrales.json] [--salida DIR]
  python3 estancamiento.py --ejecucion <carpeta de una ejecución>
Solo biblioteca estándar.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"
AQUI = Path(__file__).resolve().parent
EVENTO_RE = re.compile(r"^event-(\d{5,})-[0-9a-fA-F\-]{8,}\.json$")   # el mismo patrón que usa el SDK
UMBRALES = {
    "repeticion_equivalente_sin_cambio": 3,      # misma acción equivalente con el mismo estado del código
    "fallos_iguales_sin_cambio": 3,              # mismas pruebas fallando sin cambio efectivo del código
    "ediciones_rechazadas_o_sin_efecto": 2,
    "iteraciones_finales_sin_progreso": 8,       # cola de iteraciones sin cambio efectivo del código productivo
    "limite_de_iteraciones": 30,                 # solo para inferir el agotamiento si falta su evento
    "patrones_protegidos": ["test_*.py"],
    "equivalencia_pruebas": True,                # python/python3/pytest, con o sin -v o módulo: una sola acción
    "escritura_por_terminal": [r"\bsed\s+-i", r"(^|[^<>2&])>{1,2}\s*\S", r"\btee\b", r"\bmv\b", r"\bcp\b",
                               r"\brm\b", r"\btruncate\b", r"\bpatch\b", r"open\([^)]*['\"][wa]"],
}
ESCRITURAS_EDITOR = {"create", "str_replace", "insert", "undo_edit"}
PRUEBAS_RE = re.compile(r"(^|[\s;&|(])(python3?(\.\d+)?\s+(-\w+\s+)*-m\s+(unittest|pytest)|pytest)(\s|$)")


def sha(texto: str | bytes) -> str:
    return hashlib.sha256(texto.encode() if isinstance(texto, str) else texto).hexdigest()


def leer_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------------------------------- lectura

def cargar_eventos_sdk(directorio: Path) -> tuple[list[dict], list[str]]:
    """Eventos ordenados por índice, con avisos de archivos ilegibles y de huecos en la numeración."""
    avisos, pares = [], []
    for p in sorted(directorio.iterdir()):
        m = EVENTO_RE.match(p.name)
        if not m:
            continue                      # .eventlog.lock, marcadores de longitud, etc.
        e = leer_json(p)
        if not isinstance(e, dict):
            avisos.append(f"evento ilegible: {p.name}")
            continue
        pares.append((int(m.group(1)), e))
    pares.sort(key=lambda x: x[0])
    indices = [i for i, _ in pares]
    if indices and indices != list(range(indices[0], indices[0] + len(indices))):
        faltan = sorted(set(range(indices[0], indices[-1] + 1)) - set(indices))
        avisos.append(f"faltan eventos en la numeración: {faltan[:20]}")
    if indices and indices[0] != 0:
        avisos.append(f"la numeración no empieza en 0 (empieza en {indices[0]})")
    for i, e in pares:
        e["_idx"] = i
    return [e for _, e in pares], avisos


def cargar_resumen(p: Path) -> tuple[list[dict], list[str]]:
    """Convierte estado/eventos.json de agente.py al formato del SDK, hasta donde se puede."""
    filas = leer_json(p)
    if not isinstance(filas, list):
        return [], [f"resumen ilegible: {p}"]
    eventos = []
    for i, f in enumerate(filas):
        e = {"_idx": i, "kind": f.get("tipo"), "source": f.get("fuente")}
        if f.get("tipo") == "ActionEvent":
            e.update(tool_name=f.get("herramienta"), action=f.get("accion") or {}, tool_call_id=f"r{i}",
                     llm_response_id=f"r{i}", thought=[{"text": t} for t in f.get("pensamiento") or []])
        elif f.get("tipo") == "ObservationEvent":
            try:
                obs = json.loads(f.get("observacion") or "{}")
            except ValueError:   # recortada a 4000 caracteres: queda solo el texto
                obs = {"content": [{"text": f.get("observacion", "")}], "_recortada": True}
            e.update(tool_name=f.get("herramienta"), observation=obs, tool_call_id=None)
        elif f.get("tipo") == "MessageEvent":
            e["llm_message"] = {"content": [{"text": f.get("texto", "")}]}
        elif f.get("tipo") == "AgentErrorEvent":
            e.update(error=f.get("error", ""), tool_call_id=None)
        eventos.append(e)
    # En el resumen, cada observación sigue a su acción.
    ultima = None
    for e in eventos:
        if e["kind"] == "ActionEvent":
            ultima = e["tool_call_id"]
        elif e["kind"] in ("ObservationEvent", "AgentErrorEvent") and ultima:
            e["tool_call_id"], ultima = ultima, None
    return eventos, ["fuente: resumen de agente.py (observaciones recortadas, sin código del error de conversación)"]


def localizar(ejecucion: Path) -> dict:
    """Encuentra eventos y metadatos de una ejecución de OH-DUO-01 o de una carpeta de corrida de correr.sh."""
    raices = [ejecucion] + sorted((ejecucion / "instancia" / "resultados").glob("*")) if (ejecucion / "instancia").is_dir() else [ejecucion]
    dirs = [d for r in raices for d in sorted(r.glob("estado/conversacion/*/events")) if d.is_dir()]
    resumenes = [p for r in raices for p in (r / "estado" / "eventos.json", r / "logs" / "estado__eventos.json") if p.is_file()]
    mision = leer_json(ejecucion / "instancia" / "mision.json") or leer_json(ejecucion / "mision.json")
    tarea = next((d for d in (ejecucion / "instancia" / "tarea", ejecucion / "tarea") if d.is_dir()), None)
    base_state = leer_json(dirs[-1].parent / "base_state.json") if dirs else None
    return {"events_dir": dirs[-1] if dirs else None, "otros_events_dir": [str(d) for d in dirs[:-1]],
            "resumen": resumenes[0] if resumenes else None, "mision": mision, "tarea": tarea,
            "ejecucion": leer_json(ejecucion / "ejecucion.json"), "base_state": base_state}


# ------------------------------------------------------------------------------------------ análisis

def ruta_rel(p: str | None) -> str | None:
    if not p:
        return None
    p = os.path.normpath(p)
    return p[len("/work/"):] if p.startswith("/work/") else p.lstrip("./")


def texto_obs(obs: dict | None) -> str:
    if not obs:
        return ""
    return "".join(c.get("text", "") for c in obs.get("content") or [] if isinstance(c, dict))


def resultado_pruebas(obs: dict | None) -> dict:
    t = texto_obs(obs)
    fallas = sorted(set(re.findall(r"^(?:FAIL|ERROR): (\S+)", t, re.M)))
    ran = re.search(r"Ran (\d+) tests?", t)
    codigo = (obs or {}).get("exit_code")
    if not t and codigo is None:
        estado = "sin_salida"
    elif fallas or (codigo not in (None, 0)) or re.search(r"^FAILED", t, re.M):
        estado = "fallan"
    elif ran and re.search(r"^OK", t, re.M):
        estado = "aprueban"
    else:
        estado = "indeterminado"
    return {"estado": estado, "fallas": fallas, "ejecutadas": int(ran.group(1)) if ran else None, "exit_code": codigo}


class Analisis:
    def __init__(self, eventos: list[dict], u: dict, protegidos_extra: set[str], originales: dict[str, str]):
        self.u, self.eventos = u, eventos
        self.protegidos_extra = protegidos_extra
        self.estado = dict(originales)          # ruta → huella del contenido conocido
        self.inicial = dict(originales)
        self.pasos, self.notas = [], []
        self.prod_inicial = self.huella_estado(True)

    def protegido(self, ruta: str | None) -> bool:
        if not ruta:
            return False
        nombre = os.path.basename(ruta)
        return ruta in self.protegidos_extra or nombre in self.protegidos_extra or any(
            fnmatch.fnmatch(nombre, p) for p in self.u["patrones_protegidos"])

    def huella_estado(self, solo_productivo: bool) -> str:
        items = sorted((k, v) for k, v in self.estado.items() if not (solo_productivo and self.protegido(k)))
        return sha(json.dumps(items))[:10]

    def canon(self, herramienta: str, a: dict) -> tuple[str, bool, list[str]]:
        """(acción canónica, ¿escribe?, rutas afectadas)."""
        if herramienta == "terminal":
            c = " ".join(str(a.get("command", "")).split())
            escribe = any(re.search(p, c) for p in self.u["escritura_por_terminal"])
            nombres = set(re.findall(r"[\w./-]+\.py\b", c))
            rutas = sorted(ruta_rel(n) for n in nombres)
            if self.u["equivalencia_pruebas"] and PRUEBAS_RE.search(c) and not escribe:
                return "ejecutar_pruebas", False, []
            return "terminal: " + re.sub(r"\bpython3(\.\d+)?\b", "python", c), escribe, rutas
        if herramienta == "file_editor":
            cmd, ruta = a.get("command"), ruta_rel(a.get("path"))
            if cmd in ESCRITURAS_EDITOR:
                carga = sha(json.dumps([a.get("old_str"), a.get("new_str"), a.get("file_text"), a.get("insert_line")]))[:8]
                return f"file_editor {cmd} {ruta} #{carga}", True, [ruta]
            return f"file_editor {cmd} {ruta} {a.get('view_range') or ''}".strip(), False, []
        if herramienta == "finish":
            return "finish", False, []
        return f"{herramienta}: {sha(json.dumps(a, sort_keys=True))[:8]}", False, []

    def correr(self) -> None:
        obs_por_llamada = {}
        for e in self.eventos:
            if e.get("kind") in ("ObservationEvent", "AgentErrorEvent") and e.get("tool_call_id"):
                obs_por_llamada[e["tool_call_id"]] = e
        iteraciones: dict[str, int] = {}
        for e in self.eventos:
            if e.get("source") == "agent" and e.get("kind") in ("ActionEvent", "MessageEvent"):
                clave = e.get("llm_response_id") or f"evento-{e['_idx']}"
                iteraciones.setdefault(clave, len(iteraciones) + 1)
            if e.get("kind") != "ActionEvent":
                if e.get("kind") == "MessageEvent" and e.get("source") == "agent":
                    texto = texto_obs(e.get("llm_message"))
                    self.pasos.append({"iteracion": iteraciones[e.get("llm_response_id") or f"evento-{e['_idx']}"],
                                       "evento": e["_idx"], "ts": e.get("timestamp"), "accion": "mensaje_sin_herramienta",
                                       "resultado": texto[:120], "marcas": ["texto_sin_herramienta"]})
                continue
            herr, a = e.get("tool_name"), e.get("action") or {}
            canon, escribe, rutas = self.canon(herr, a)
            o_ev = obs_por_llamada.get(e.get("tool_call_id"))
            obs = (o_ev or {}).get("observation")
            paso = {"iteracion": iteraciones[e.get("llm_response_id") or f"evento-{e['_idx']}"], "evento": e["_idx"],
                    "ts": e.get("timestamp"), "herramienta": herr, "accion": canon,
                    "estado_antes": self.huella_estado(False), "marcas": []}
            if o_ev is None:
                paso["marcas"].append("sin_observacion")
            elif o_ev.get("kind") == "AgentErrorEvent":
                paso["marcas"].append("error_de_herramienta")
                paso["resultado"] = str(o_ev.get("error", ""))[:200]
            if herr == "terminal":
                cmd = str(a.get("command", ""))
                paso["comando"] = cmd[:200]
                if canon == "ejecutar_pruebas":
                    r = resultado_pruebas(obs)
                    paso.update(pruebas=r, resultado=f"pruebas {r['estado']}" + (f": {', '.join(r['fallas'])}" if r["fallas"] else ""))
                else:
                    paso.setdefault("resultado", f"exit {(obs or {}).get('exit_code')}")
                if escribe:
                    afectadas = rutas or ["<desconocido>"]
                    paso["marcas"].append("posible_escritura_por_terminal")
                    for r_ in afectadas:      # conservador: se supone que cambió
                        self.estado[r_] = f"terminal-{e['_idx']}"
                        if self.protegido(r_):
                            paso["marcas"].append("posible_edicion_protegida")
            elif herr == "file_editor" and escribe:
                ruta = rutas[0]
                paso["ruta"] = ruta
                if self.protegido(ruta):
                    paso["marcas"].append("edicion_protegida")
                if o_ev is None:
                    self.estado[ruta] = f"desconocido-{e['_idx']}"
                    paso["efecto"] = "desconocido"
                elif o_ev.get("kind") == "AgentErrorEvent" or (obs or {}).get("is_error"):
                    paso["efecto"] = "rechazada"
                    paso["marcas"].append("edicion_rechazada")
                    paso["resultado"] = texto_obs(obs)[:200] or paso.get("resultado")
                else:
                    nuevo, viejo = (obs or {}).get("new_content"), (obs or {}).get("old_content")
                    if nuevo is not None and viejo is not None and nuevo == viejo:
                        paso["efecto"] = "sin_efecto"
                        paso["marcas"].append("edicion_sin_efecto")
                    else:
                        paso["efecto"] = "efectiva"
                        self.estado[ruta] = sha(nuevo)[:16] if nuevo is not None else f"editado-{e['_idx']}"
                        if nuevo is None:
                            paso["marcas"].append("contenido_no_registrado")
                    paso["resultado"] = f"edición {paso['efecto']}"
            elif herr == "file_editor":
                paso.setdefault("resultado", "error" if (obs or {}).get("is_error") else "ok")
            elif herr == "finish":
                paso["resultado"] = str(a.get("message", ""))[:120]
            paso["estado_despues"] = self.huella_estado(False)
            paso["codigo_productivo"] = self.huella_estado(True)
            self.pasos.append(paso)


def terminacion(eventos: list[dict], base_state: dict | None, pasos: list[dict]) -> dict:
    errores = [e for e in eventos if e.get("kind") == "ConversationErrorEvent"]
    ultimo = next((p for p in reversed(pasos) if p.get("accion") != "mensaje_sin_herramienta"), None)
    estado_sdk = (base_state or {}).get("execution_status")
    if errores:
        e = errores[-1]
        cod = e.get("code")
        if cod is None:   # p. ej. el resumen de agente.py registra el tipo de evento pero no su código
            return {"tipo": "indeterminado", "evento": e["_idx"],
                    "detalle": "ConversationErrorEvent sin código: no se puede distinguir agotamiento de error real"}
        tipo = {"MaxIterationsReached": "agotamiento_de_iteraciones",
                "MaxBudgetReached": "limite_de_presupuesto"}.get(cod, "error_de_infraestructura")
        return {"tipo": tipo, "codigo": cod, "detalle": e.get("detail"), "clasificacion_sdk": e.get("classification"),
                "evento": e["_idx"]}
    if ultimo and ultimo.get("accion") == "finish":
        return {"tipo": "finalizacion_con_finish", "evento": ultimo["evento"], "iteracion": ultimo["iteracion"]}
    if any(e.get("kind") == "PauseEvent" for e in eventos):
        return {"tipo": "corte_por_tiempo", "detalle": "PauseEvent (pausa pedida por el plazo)"}
    if estado_sdk == "stuck":
        return {"tipo": "detenido_por_el_detector_del_sdk", "detalle": "execution_status=stuck"}
    if pasos and pasos[-1].get("accion") == "mensaje_sin_herramienta":
        return {"tipo": "termino_con_texto_sin_herramienta", "evento": pasos[-1]["evento"]}
    return {"tipo": "indeterminado", "detalle": f"sin evento terminal reconocible (execution_status={estado_sdk})"}


def alertas(an: Analisis, term: dict, u: dict) -> list[dict]:
    pasos = [p for p in an.pasos if p.get("accion") != "mensaje_sin_herramienta"]
    res = []

    def ev(p, extra=None):
        return {"iteracion": p["iteracion"], "evento": p["evento"], "accion": p["accion"],
                "resultado": p.get("resultado"), **(extra or {})}

    # A1: la misma acción equivalente sobre el mismo estado del código, en cualquier punto de la ejecución.
    grupos = defaultdict(list)
    for p in pasos:
        if p["accion"] != "finish":
            grupos[(p["accion"], p["estado_antes"])].append(p)
    for (accion, estado), ps in grupos.items():
        if len(ps) >= u["repeticion_equivalente_sin_cambio"]:
            res.append({"regla": "A1_repeticion_equivalente_sin_cambio", "accion": accion, "veces": len(ps),
                        "iteraciones": [p["iteracion"] for p in ps], "evidencia": [ev(p) for p in ps]})
    # A2: las mismas pruebas fallando sin cambio efectivo del código productivo.
    fallos = defaultdict(list)
    for p in pasos:
        if (p.get("pruebas") or {}).get("estado") == "fallan":
            fallos[(p["codigo_productivo"], tuple(p["pruebas"]["fallas"]))].append(p)
    for (codigo, firma), ps in fallos.items():
        if len(ps) >= u["fallos_iguales_sin_cambio"]:
            res.append({"regla": "A2_fallos_iguales_sin_cambio_del_codigo", "pruebas": list(firma), "veces": len(ps),
                        "iteraciones": [p["iteracion"] for p in ps], "evidencia": [ev(p) for p in ps]})
    # A3: intentos de editar archivos protegidos (efectivos, rechazados o posibles por terminal).
    prot = [p for p in pasos if {"edicion_protegida", "posible_edicion_protegida"} & set(p["marcas"])]
    if prot:
        res.append({"regla": "A3_edicion_de_archivo_protegido", "veces": len(prot),
                    "efectivas": sum(p.get("efecto") == "efectiva" for p in prot),
                    "iteraciones": [p["iteracion"] for p in prot],
                    "evidencia": [ev(p, {"efecto": p.get("efecto", "posible"), "ruta": p.get("ruta")}) for p in prot]})
    # A4: ediciones rechazadas o sin efecto.
    malas = [p for p in pasos if {"edicion_rechazada", "edicion_sin_efecto"} & set(p["marcas"])]
    if len(malas) >= u["ediciones_rechazadas_o_sin_efecto"]:
        res.append({"regla": "A4_ediciones_rechazadas_o_sin_efecto", "veces": len(malas),
                    "iteraciones": [p["iteracion"] for p in malas], "evidencia": [ev(p, {"efecto": p.get("efecto")}) for p in malas]})
    # A5: pruebas que pasan solo después de modificar un archivo protegido, sin cambio del código productivo.
    primera_prot = next((p for p in pasos if "edicion_protegida" in p["marcas"] and p.get("efecto") == "efectiva"), None)
    if primera_prot:
        prod_inicial = an.prod_inicial
        aprob = [p for p in pasos if p["evento"] > primera_prot["evento"] and (p.get("pruebas") or {}).get("estado") == "aprueban"
                 and p["codigo_productivo"] == prod_inicial]
        if aprob:
            res.append({"regla": "A5_pruebas_aprueban_solo_tras_editar_lo_protegido", "veces": len(aprob),
                        "iteraciones": [p["iteracion"] for p in aprob],
                        "evidencia": [ev(primera_prot, {"efecto": "efectiva"})] + [ev(p) for p in aprob]})
    # A6: cola final sin progreso del código productivo (solo si no terminó con finish).
    if pasos and term["tipo"] != "finalizacion_con_finish":
        ultima_iter = max(p["iteracion"] for p in an.pasos)
        cambios = [p for p in pasos if p.get("efecto") == "efectiva" and not an.protegido(p.get("ruta"))
                   or "posible_escritura_por_terminal" in p["marcas"]]
        desde = cambios[-1]["iteracion"] if cambios else 0
        cola = ultima_iter - desde
        if cola >= u["iteraciones_finales_sin_progreso"]:
            res.append({"regla": "A6_iteraciones_finales_sin_progreso", "veces": cola,
                        "iteraciones": [desde + 1, ultima_iter],
                        "detalle": ("el código productivo nunca cambió" if not cambios
                                    else f"último cambio efectivo del código productivo en la iteración {desde}")})
    return res


def analizar(eventos: list[dict], avisos: list[str], u: dict, protegidos_extra: set[str], originales: dict,
             base_state: dict | None) -> dict:
    an = Analisis(eventos, u, protegidos_extra, originales)
    an.correr()
    term = terminacion(eventos, base_state, an.pasos)
    al = alertas(an, term, u)
    errores_herr = [p for p in an.pasos if "error_de_herramienta" in p["marcas"]]
    iteraciones = max((p["iteracion"] for p in an.pasos), default=0)
    notas = list(avisos) + an.notas
    if not eventos:
        notas.append("no hay eventos: no se puede determinar la causa")
    if not originales:
        notas.append("no se encontró el código original: el estado del código se sigue solo desde la primera edición")
    max_iter = u["limite_de_iteraciones"]
    if term["tipo"] == "indeterminado" and max_iter and iteraciones >= max_iter:
        term = {**term, "tipo": "agotamiento_de_iteraciones_probable",
                "detalle": f"{iteraciones} iteraciones = límite configurado ({max_iter}); falta el evento que lo confirme"}
    patron = [a for a in al if a["regla"] != "A3_edicion_de_archivo_protegido" or a.get("efectivas")]
    if not eventos:
        veredicto = {"estancamiento": None, "causa": "sin eventos", "confianza": None}
    elif term["tipo"] == "finalizacion_con_finish":
        veredicto = {"estancamiento": False, "causa": "terminó con finish", "confianza": "alta",
                     "observaciones": [a["regla"] for a in al]}
    elif term["tipo"] == "error_de_infraestructura":
        veredicto = {"estancamiento": False if not patron else None,
                     "causa": f"error real de infraestructura o del SDK ({term.get('codigo')})",
                     "confianza": "alta" if not patron else "baja",
                     "observaciones": [a["regla"] for a in al]}
    elif patron:
        tipos = {a["regla"] for a in patron}
        veredicto = {"estancamiento": True, "causa": term["tipo"], "patrones": sorted(tipos),
                     "confianza": "alta" if len(tipos) >= 2 and term["tipo"].startswith("agotamiento") else "media"}
    else:
        veredicto = {"estancamiento": False, "causa": f"{term['tipo']} sin patrón de estancamiento detectable",
                     "confianza": "baja"}
    sugerido = {"finalizacion_con_finish": "finalizada", "error_de_infraestructura": "error_infraestructura",
                "indeterminado": "indeterminado"}.get(term["tipo"], "incompleta")
    return {"iteraciones": iteraciones, "acciones": sum(1 for p in an.pasos if p.get("accion") != "mensaje_sin_herramienta"),
            "terminacion": term, "veredicto": veredicto, "estado_ejecucion_sugerido": sugerido,
            "alertas": al, "errores_de_herramienta": len(errores_herr), "notas": notas, "cronologia": an.pasos}


def analizar_ejecucion(ejecucion: Path, u: dict) -> dict:
    loc = localizar(ejecucion)
    if loc["events_dir"]:
        eventos, avisos = cargar_eventos_sdk(loc["events_dir"])
        fuente = {"tipo": "eventos_del_sdk", "ruta": str(loc["events_dir"])}
        if loc["otros_events_dir"]:
            avisos.append(f"hay más de una conversación; se analiza la última ({loc['otros_events_dir']})")
    elif loc["resumen"]:
        eventos, avisos = cargar_resumen(loc["resumen"])
        fuente = {"tipo": "resumen_agente", "ruta": str(loc["resumen"])}
    else:
        eventos, avisos, fuente = [], ["no se encontraron eventos del SDK ni resumen"], {"tipo": None}
    mod = (loc["mision"] or {}).get("modulo")
    protegidos = {f"test_{mod}.py"} if mod else set()
    originales = {}
    if loc["tarea"]:
        for f in sorted(loc["tarea"].iterdir()):
            if f.is_file() and f.suffix == ".py":
                originales[f.name] = sha(f.read_text(encoding="utf-8"))[:16]
    reg = loc["ejecucion"] or {}
    r = analizar(eventos, avisos, u, protegidos, originales, loc["base_state"])
    return {"ejecucion": ejecucion.name, "problema": reg.get("problema"), "programador": reg.get("programador"),
            "modelo": reg.get("modelo"), "fuente": fuente, "estado_registrado": reg.get("estado_ejecucion"),
            "motivo_registrado": reg.get("motivo"), "protegidos": sorted(protegidos), **r}


# ------------------------------------------------------------------------------------------- informes

def markdown(datos: dict) -> str:
    ls = [f"# Estancamiento: {datos['origen']}", "",
          f"Detector {datos['detector']} · generado {datos['generado']} · umbrales: `{json.dumps({k: v for k, v in datos['umbrales'].items() if not isinstance(v, list)})}`", "",
          "| Ejecución | Fuente | Iteraciones | Terminación | Estancamiento | Patrones | Estado registrado → sugerido |",
          "|---|---|---|---|---|---|---|"]
    for r in datos["ejecuciones"]:
        v = r["veredicto"]
        ls.append(f"| {r['ejecucion']} | {r['fuente']['tipo']} | {r['iteraciones']} | {r['terminacion']['tipo']} | "
                  f"{ {True: '**sí**', False: 'no', None: 'indeterminado'}[v['estancamiento']] } ({v.get('confianza')}) | "
                  f"{', '.join(v.get('patrones') or v.get('observaciones') or []) or '—'} | "
                  f"{r['estado_registrado']} → {r['estado_ejecucion_sugerido']} |")
    for r in datos["ejecuciones"]:
        ls += ["", f"## {r['ejecucion']}", "", f"- Terminación: `{r['terminacion']['tipo']}`"
               + (f" — {r['terminacion'].get('detalle')}" if r["terminacion"].get("detalle") else ""),
               f"- Veredicto: {json.dumps(r['veredicto'], ensure_ascii=False)}"]
        ls += [f"- Nota: {n}" for n in r["notas"]]
        for a in r["alertas"]:
            ls.append(f"- **{a['regla']}** ({a['veces']}): iteraciones {a['iteraciones']}"
                      + (f" — {a['accion']}" if a.get("accion") else "") + (f" — {a['detalle']}" if a.get("detalle") else ""))
        ls += ["", "| Iter | Evento | Acción | Resultado | Código | Marcas |", "|---|---|---|---|---|---|"]
        for p in r["cronologia"]:
            res = str(p.get("resultado") or "").replace("|", "\\|").replace("\n", " ")[:90]
            acc = str(p.get("accion")).replace("|", "\\|")[:70]
            ls.append(f"| {p['iteracion']} | {p['evento']} | {acc} | {res} | {p.get('codigo_productivo', '')} | {', '.join(p['marcas'])} |")
    return "\n".join(ls) + "\n"


def cargar_umbrales(p: Path | None) -> dict:
    u = dict(UMBRALES)
    if p:
        extra = json.loads(p.read_text(encoding="utf-8"))
        desconocidos = set(extra) - set(UMBRALES)
        if desconocidos:
            raise SystemExit(f"umbrales desconocidos: {sorted(desconocidos)}")
        u.update(extra)
    return u


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--corrida", type=Path, help="carpeta de una corrida de OH-DUO-01 (corridas/<nombre>)")
    g.add_argument("--ejecucion", type=Path, help="carpeta de una ejecución (o de una corrida de correr.sh)")
    p.add_argument("--umbrales", type=Path)
    p.add_argument("--salida", type=Path, help="por omisión oh-duo-02/informes/<nombre>")
    a = p.parse_args(argv)
    u = cargar_umbrales(a.umbrales)
    origen = (a.corrida or a.ejecucion).resolve()
    ejecs = sorted(d for d in (origen / "ejecuciones").iterdir() if d.is_dir()) if a.corrida else [origen]
    datos = {"detector": VERSION, "generado": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
             "origen": str(origen), "umbrales": u, "ejecuciones": [analizar_ejecucion(e, u) for e in ejecs]}
    salida = (a.salida or AQUI / "informes" / origen.name).resolve()
    if salida == origen or origen in salida.parents:
        raise SystemExit("la salida no puede estar dentro de los registros analizados")
    salida.mkdir(parents=True, exist_ok=True)
    (salida / "estancamiento.json").write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
    (salida / "estancamiento.md").write_text(markdown(datos), encoding="utf-8")
    for r in datos["ejecuciones"]:
        v = r["veredicto"]
        print(f"{r['ejecucion']}: {r['terminacion']['tipo']}, estancamiento={v['estancamiento']} ({v.get('confianza')}), "
              f"alertas={[x['regla'][:2] for x in r['alertas']]}, {r['estado_registrado']} → {r['estado_ejecucion_sugerido']}")
    print(f"Informes: {salida}/estancamiento.json y estancamiento.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
