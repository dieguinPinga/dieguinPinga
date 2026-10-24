"""Experimento 7.7: diagnóstico y reparación COORDINADOS entre dos IA (OFICINA.md, «Experimento 7.7»).

Igual que el 7.6, pero SIN la causa escrita por el equipo: la causa la diagnostica otra IA.
1. DIAGNOSTICADORA (gemma4:12b): recibe el contrato, los ejemplos visibles, las zonas de la corrida 7.3 de origen y
   la DISCREPANCIA OBSERVABLE del caso sintético (la entrada, lo que pide el contrato y lo que devuelve el código).
   Nada más: ni la causa conocida (reparacion_guiada.CAUSAS), ni el diagnóstico anterior del revisor, ni el defecto,
   ni la referencia, ni casos ocultos. Responde un JSON (causa, líneas citadas, mecanismo, relación con la
   discrepancia). El coordinador lo valida: campos completos, sin código, y al menos una línea citada que exista
   LITERALMENTE dentro de las zonas. Si no es válido, se registra el fallo y se le pide otra vez (a lo sumo
   MAX_DIAGNOSTICOS); si sigue sin serlo, el experimento se detiene SIN sustituirlo por la causa conocida.
2. PROGRAMADOR (qwen2.5-coder:7b): el ciclo del 7.6 tal cual (reparacion_guiada.correr con la normalización de la
   sangría), con el diagnóstico de la diagnosticadora en lugar de la causa del equipo, rotulado como lo que es.
3. COORDINADOR determinista: validación, normalización de la sangría, aplicación, caso sintético, ejemplos visibles
   y la batería independiente (9 casos) una sola vez; nada de eso vuelve a los modelos.

La evaluación de si el diagnóstico identifica la causa es POSTERIOR y separada (sonda_comprension.evaluar sobre el
texto del diagnóstico): no decide nada durante la corrida ni llega a ningún modelo.
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
from . import reparacion_guiada as RGD
from . import revision_existente as RE
from . import revisor as RV
from . import sonda_comprension as SC
from .misiones_programacion import MISIONES, VALIDADORES

VERSION_COORDINADA = "experimento 7.7: diagnóstico (gemma) y reparación (qwen) coordinados, con normalización de sangría"
MAX_DIAGNOSTICOS = 2
CLASIFICACION = "diagnóstico y reparación automáticos coordinados"

SISTEMA_DIAGNOSTICO = (
    "Sos la DIAGNOSTICADORA de un equipo. Un código no cumple su contrato en un caso concreto que el coordinador "
    "ejecutó en un entorno aislado. Tu tarea es encontrar la CAUSA en el código, no corregirlo.\n"
    "Seguí la entrada del caso paso a paso por las líneas que se te muestran y explicá qué condición o qué estado "
    "produce la salida incorrecta.\n"
    "Respondé SOLO con un objeto JSON con esta forma:\n"
    '{"causa": "<la causa concreta, en una o dos oraciones>", "lineas_citadas": ["<línea copiada LITERALMENTE del '
    'código, sin el número>"], "mecanismo": ["<paso 1>", "<paso 2>", "..."], "relacion_con_la_discrepancia": "<por '
    'qué esa causa produce exactamente la diferencia observada>"}\n'
    "No escribas código corregido, ni bloques de código, ni el programa.")
PEDIDO_DIAGNOSTICO = "Diagnosticá la causa y respondé solo con el JSON pedido."
TITULO_DIAGNOSTICO = ("DIAGNÓSTICO DE LA DIAGNOSTICADORA (otra IA; el coordinador comprobó que las líneas citadas "
                      "existen en el código, NO que la causa sea correcta)")
MAX_CAMPO = 600


class ErrorCoordinada(Exception):
    pass


def preparar(corrida: Path) -> dict:
    """Fuentes de solo lectura de la corrida 7.3 y el caso sintético demostrado. Sin causa: la dará la diagnosticadora."""
    try:
        f = SC.fuentes(Path(corrida))
    except SC.ErrorSonda as e:
        raise ErrorCoordinada(str(e)) from None
    rev = json.loads((Path(corrida) / f"revision-{f['revision']}" / "revision.json").read_text())
    f["caso"] = rev["contraste"]["caso"]
    f.pop("diagnostico", None)          # el diagnóstico ANTERIOR del revisor no se usa en el 7.7
    return f


def discrepancia(executor, f: dict) -> dict:
    """La discrepancia observable del caso sintético sobre el código defectuoso (contraste en el sandbox)."""
    fabrica, referencia = MISIONES[f["mision"]]
    ct = CT.contrastar(executor, CT.desde_mision(fabrica, referencia, VALIDADORES[f["mision"]]), f["codigo"],
                       f["caso"], None)
    if ct["discrepancia"] != "demostrada":
        raise ErrorCoordinada(f"el caso sintético no muestra una discrepancia en el código ({ct['discrepancia']})")
    return ct


def texto_discrepancia(funcion: str, ct: dict) -> str:
    esperado, obtenido = ct["referencia"]["salida"], ct["candidato"]["salida"]
    comp = RE.comparar_salidas(esperado, obtenido if isinstance(obtenido, dict) else None)
    l = ["DISCREPANCIA OBSERVADA (caso sintético ejecutado por el coordinador; NO es una prueba oculta):",
         f"Caso: {funcion}({RE._llamada(ct['caso'])})",
         f"Lo que pide el contrato: {RE._json(esperado)}",
         f"Lo que devuelve el código: {RE._json(obtenido)}"]
    if comp["comparable"]:
        l.append("Claves distintas: " + ", ".join(f"'{k}'" for k in comp["distintas"]))
        for k in comp["distintas"]:
            d = comp["detalle"].get(k) or {}
            if "faltan" in d:
                l.append(f"  - '{k}': falta {RE._json(d['faltan'], 300)} · sobra {RE._json(d['sobran'], 300)}")
    return "\n".join(l)


def mensajes_diagnostico(f: dict, disc: str, nota: str = "") -> list[dict]:
    fabrica, _ = MISIONES[f["mision"]]
    m = fabrica()
    spec = m["spec"]
    usuario = (f"CONTRATO:\n{m['description']}\n\n{prompts._sig(spec)}\n\n"
               f"Ejemplos visibles:\n{prompts._examples(spec, 3000) or '(sin ejemplos visibles)'}\n\n"
               "CÓDIGO (las zonas relevantes y un contexto de solo lectura; los números de línea NO son parte del "
               f"código):\n\n{PA.texto_zonas(f['codigo'], spec['function'], {'zonas': f['zonas']})}\n\n{disc}\n\n"
               + (f"NOTA DEL COORDINADOR:\n{nota}\n\n" if nota else "")
               + PEDIDO_DIAGNOSTICO)
    return [{"role": "system", "content": SISTEMA_DIAGNOSTICO}, {"role": "user", "content": usuario}]


def interpretar_diagnostico(texto: str, codigo: str, zonas, funcion: str) -> dict:
    """Valida el diagnóstico. ValueError (con el motivo) si es insuficiente."""
    t = (texto or "").strip()
    if re.search(rf"\bdef\s+{re.escape(funcion)}\s*\(", t):
        raise ValueError("incluyó el programa: se pide solo el diagnóstico")
    mt = re.search(r"\{.*\}", t, re.S)
    if not mt:
        raise ValueError("la respuesta no contiene un objeto JSON")
    try:
        d = json.loads(mt.group(0))
    except ValueError as e:
        raise ValueError(f"JSON ilegible: {e}") from None
    if not isinstance(d, dict):
        raise ValueError("la respuesta no es un objeto JSON")
    causa = RV._limpio(d.get("causa"), MAX_CAMPO) if isinstance(d.get("causa"), str) else ""
    relacion = (RV._limpio(d.get("relacion_con_la_discrepancia"), MAX_CAMPO)
                if isinstance(d.get("relacion_con_la_discrepancia"), str) else "")
    mecanismo = [RV._limpio(x, MAX_CAMPO) for x in (d.get("mecanismo") or []) if isinstance(x, str) and x.strip()][:8]
    citas = [x for x in (d.get("lineas_citadas") or []) if isinstance(x, str) and x.strip()][:4]
    if not causa:
        raise ValueError("falta «causa»")
    if len(mecanismo) < 2:
        raise ValueError("«mecanismo» necesita al menos 2 pasos")
    if not relacion:
        raise ValueError("falta «relacion_con_la_discrepancia»")
    lineas = []
    for c in citas:
        c = re.sub(r"^\s*\d+\|", "", c.strip()).strip()[:RV.MAX_CITA]
        ls = PA.lineas_de_cita(codigo, c)
        lineas.append({"texto": c, "lineas": ls, "en_zona": bool(ls) and all(any(a <= x <= b for a, b in zonas)
                                                                              for x in ls)})
    if not any(x["en_zona"] for x in lineas):
        raise ValueError("ninguna línea citada aparece literalmente dentro de las zonas del código")
    return {"causa": causa, "mecanismo": mecanismo, "relacion_con_la_discrepancia": relacion, "lineas_citadas": lineas}


def texto_para_el_programador(d: dict) -> str:
    l = [f"Causa: {d['causa']}",
         "Líneas involucradas: " + "; ".join(f"«{x['texto']}»" for x in d["lineas_citadas"] if x["en_zona"]),
         "Mecanismo:"]
    l += [f"    {i}. {p}" for i, p in enumerate(d["mecanismo"], 1)]
    l.append(f"Relación con la discrepancia observada: {d['relacion_con_la_discrepancia']}")
    return "\n".join(l)


def nota_de_diagnostico_invalido(motivo: str) -> str:
    return (f"Tu diagnóstico anterior no se pudo usar ({motivo}). Respondé con el JSON completo y copiá las líneas "
            "citadas exactamente como aparecen en el código.")


def evaluar_diagnostico(d: dict | None) -> dict:
    """POSTERIOR y separada (no decide nada durante la corrida ni llega a ningún modelo)."""
    if d is None:
        return {"identifica_la_causa": False, "detalle": None}
    texto = "\n".join([d["causa"], *d["mecanismo"], d["relacion_con_la_discrepancia"],
                       *(x["texto"] for x in d["lineas_citadas"])])
    ev = SC.evaluar(texto)
    citas = [x["texto"] for x in d["lineas_citadas"] if x["en_zona"]]
    cita_condicion = any('tipo != "anulacion"' in c or "tipo != 'anulacion'" in c for c in citas)
    return {"identifica_la_causa": bool(ev["identifica_ausencia_en_aplicados"]),
            "cita_la_condicion": cita_condicion, "detalle": ev,
            "nota": "evaluación automática e indicativa sobre el diagnóstico; requiere lectura humana"}


def carpeta(carpeta_lab: Path, corrida: Path) -> Path:
    try:
        return RGD.carpeta_de_reparacion(carpeta_lab, corrida, "experimento-7-7")
    except RGD.ErrorReparacion as e:
        raise ErrorCoordinada(str(e)) from None


def correr(destino: Path, f: dict, executor, cliente, gate, diagnosticador, programador, max_segundos: float = 2400.0,
           cancel: threading.Event | None = None, ajustes: dict | None = None) -> dict:
    """ajustes (opcional; lo usa el 7.8): reemplaza piezas del recorrido sin cambiarlo — «version», «clasificacion»,
    «mensajes_diagnostico», «interpretar», «texto», «pedido» (para el programador) y «retroalimentacion». Sin
    ajustes, es exactamente el 7.7."""
    destino = Path(destino)
    aj = ajustes or {}
    _mensajes = aj.get("mensajes_diagnostico", mensajes_diagnostico)
    _interpretar = aj.get("interpretar", interpretar_diagnostico)
    _texto = aj.get("texto", texto_para_el_programador)

    def guardar(rel, contenido):
        p = destino / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(contenido if isinstance(contenido, str) else
                     json.dumps(contenido, ensure_ascii=False, indent=2, default=str))
    fabrica, _ = MISIONES[f["mision"]]
    m = fabrica()
    spec, funcion = m["spec"], m["spec"]["function"]
    ct = discrepancia(executor, f)
    disc = texto_discrepancia(funcion, ct)
    inf = {"experimento": aj.get("version", VERSION_COORDINADA), "corrida": f["corrida"], "mision": f["mision"], "zonas": f["zonas"],
           "diagnosticador": diagnosticador.name, "programador": programador.name, "archivos_leidos": f["archivos"],
           "caso_sintetico": f["caso"], "diagnosticos": [], "diagnostico_usado": None, "reparacion": None,
           "detenido": None, "inicio": time.time()}
    pedidos, nota, valido, segundos = [], "", None, 0.0
    for k in range(1, MAX_DIAGNOSTICOS + 1):
        if segundos >= max_segundos:
            inf["detenido"] = f"límite de tiempo de inferencia ({segundos:.0f} s de {max_segundos:.0f} s)"
            break
        msgs = _mensajes(f, disc, nota)
        pedidos.append(msgs)
        guardar(f"diagnostico-{k}/pedido.json", msgs)
        auditoria = RE.auditar(pedidos, spec, f["defecto"], m["description"])
        if not auditoria["sin_fugas"]:
            inf["detenido"] = "la auditoría encontró información prohibida en el pedido de diagnóstico: " + ", ".join(
                auditoria["hallazgos"])
            break
        reg = {"intento": k, "valido": False}
        inf["diagnosticos"].append(reg)
        t0 = time.monotonic()
        try:
            gate.acquire("diagnosticadora 7.7", cancel)
            try:
                gen = cliente.chat(diagnosticador, msgs, cancel, fmt="json")
            finally:
                gate.release()
        except (OllamaError, OSError) as e:
            reg.update(segundos=round(time.monotonic() - t0, 1), motivo=f"sin respuesta: {e}")
            segundos += reg["segundos"]
            guardar(f"diagnostico-{k}/diagnostico.json", reg)
            inf["detenido"] = f"la diagnosticadora no respondió: {e}"
            break
        reg.update(segundos=round(time.monotonic() - t0, 1), tokens_prompt=gen.prompt_tokens,
                   tokens_generados=gen.eval_tokens, fin=gen.done_reason, cortada=bool(gen.truncated),
                   tiempos=gen.timings())
        segundos += reg["segundos"]
        guardar(f"diagnostico-{k}/respuesta.txt", gen.text)
        try:
            reg["diagnostico"] = _interpretar(gen.text, f["codigo"], f["zonas"], funcion)
            reg["valido"] = True
        except ValueError as e:
            reg["motivo"] = str(e) + (" (la respuesta se cortó por el límite de tokens)" if gen.truncated else "")
        guardar(f"diagnostico-{k}/diagnostico.json", reg)
        if reg["valido"]:
            valido = reg["diagnostico"]
            break
        nota = nota_de_diagnostico_invalido(reg["motivo"])
    if valido is None:
        # Sin un diagnóstico válido NO se sigue: nunca se sustituye por la causa conocida.
        inf["detenido"] = inf["detenido"] or (f"la diagnosticadora no produjo un diagnóstico válido en "
                                              f"{len(inf['diagnosticos'])} intento(s)")
        return _cerrar(inf, destino, guardar, None, aj)
    texto = _texto(valido)
    inf["diagnostico_usado"] = {"texto": texto, "huella": hashlib.sha256(texto.encode()).hexdigest()}
    f2 = dict(f, causa=texto, titulo_causa=TITULO_DIAGNOSTICO)
    if "pedido" in aj:
        f2["pedido"] = aj["pedido"]
    rep = RGD.correr(destino / "reparacion", f2, executor, cliente, gate, programador,
                     max_segundos=max(0.0, max_segundos - segundos), cancel=cancel, normalizar=True,
                     retroalimentacion=aj.get("retroalimentacion"), revision=aj.get("revision"))
    inf["reparacion"] = {k: rep[k] for k in ("intentos", "llamadas", "verificacion", "criterios", "exito", "motivo",
                                             "clasificacion", "auditoria_de_fugas")}
    # ¿El programador recibió ESE diagnóstico? (comprobado sobre lo que efectivamente se envió)
    enviados = [json.loads(p.read_text())[1]["content"] for p in sorted((destino / "reparacion").glob(
        "intento-*/pedido.json"))]
    inf["diagnostico_en_cada_pedido_del_programador"] = bool(enviados) and all(texto in e for e in enviados)
    return _cerrar(inf, destino, guardar, valido, aj)


def _cerrar(inf: dict, destino: Path, guardar, valido, aj: dict | None = None) -> dict:
    rep = inf.get("reparacion") or {}
    ev = evaluar_diagnostico(valido)
    inf["evaluacion_del_diagnostico"] = ev
    verificado = next((i for i in rep.get("intentos", []) if rep.get("verificacion") and
                       i["intento"] == rep["verificacion"]["intento"]), None)
    inf["criterios"] = {
        "1_diagnosticadora_identifica_la_causa": bool(ev["identifica_la_causa"]),
        "2_programador_recibe_ese_diagnostico_y_genera_un_parche": bool(
            inf.get("diagnostico_en_cada_pedido_del_programador") and rep.get("intentos")),
        "3_coordinador_aplica_el_parche": bool(verificado and verificado["estado"] == "aplicado"),
        "4_bateria_independiente_completa": bool(rep.get("verificacion") and rep["verificacion"]["ok"]),
    }
    inf["exito"] = all(inf["criterios"].values()) and bool(rep.get("exito"))
    inf["clasificacion"] = ((aj or {}).get("clasificacion", CLASIFICACION) + (" (con normalización de la sangría)" if verificado and
                                             verificado.get("sangria_normalizada") else "")) if inf["exito"] else None
    inf["motivo"] = inf["detenido"] or rep.get("motivo")
    inf["fin"] = time.time()
    guardar("informe.json", inf)
    return inf
