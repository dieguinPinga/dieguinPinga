"""Experimento 7.4: sonda de COMPRENSIÓN del programador (OFICINA.md, «Experimento 7.4»). Diagnóstico, no reparación.

Pregunta: con las dos zonas autorizadas y el diagnóstico del revisor, ¿el programador (qwen2.5-coder:7b) entiende
la CAUSA del defecto? Es una prueba independiente: no toca el coordinador, la lógica de producción ni la corrida
original, y no pide código ni parches.

1. FUENTE (solo lectura) — una corrida terminada de `oficina revisar-existente` (por ejemplo 7.3c):
   - el código inicial (`inicial/solucion.py`);
   - el diagnóstico de la revisión cuya discrepancia quedó DEMOSTRADA (`revision-N/revision.json`), con sus campos
     tal como los escribió el revisor (regla, citas comprobadas, secuencia causal, rama inaccesible, propiedad a
     cambiar, hechos) y SIN las instrucciones de reparación del 7.1 («QUÉ HACER…», «ver la evidencia»);
   - las zonas que el coordinador autorizó en el primer intento (`reparacion-1/reparacion.json`), mostradas con el
     mismo formato (parches.texto_zonas).
   No se le da la evidencia del caso sintético (salida de la referencia), ni datos de la batería, ni el defecto.
2. PEDIDO — dos preguntas fijas, en prosa: qué ocurre cuando una anulación referencia a otra anulación, y por qué no
   se alcanza la rama `ref["tipo"] == "anulacion"`. Sin código ni parches. La rúbrica de evaluación NO va en el pedido.
3. UNA inferencia con la infraestructura remota existente (ClienteRemoto con lista blanca, compuerta por destino) y
   los parámetros del programador de siempre (num_ctx 8192, num_predict 2048, temperatura 0.2).
4. REGISTRO en <carpeta>/experimento-7-4/sonda-<fecha>/: origen.json (fuentes y huellas), pedido.json,
   respuesta.txt (literal), metadatos.json (modelo, opciones, tokens, tiempos, fin) y evaluacion.json.
5. EVALUACIÓN, separada y automática (indicativa: requiere lectura humana), con `evaluar`:
   - `identifica_ausencia_en_aplicados`: alguna oración dice que las anulaciones aplicadas NO se registran (no se
     guardan, no se agregan…) en `aplicados`, o atribuye su falta a la condición `tipo != "anulacion"`;
   - `observa_que_la_referida_no_esta`: alguna oración dice que la anulación referida no está en `aplicados` (sin
     necesariamente explicar por qué);
   - `explica_ref_none`, `cita_la_condicion`, `contiene_codigo`, `propone_cambios`, y cuánto de la evidencia es
     paráfrasis del diagnóstico del revisor (que también recibe).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import threading
import time
from pathlib import Path

from .. import prompts
from ..ollama import OllamaError
from . import parches as PA
from . import revision_existente as RE
from . import revisor as RV
from .misiones_programacion import MISIONES

VERSION_SONDA = "experimento 7.4: sonda de comprensión causal del programador (sin reparación)"

SISTEMA_SONDA = (
    "Sos un programador del equipo y vas a explicar el comportamiento de un código existente. En esta tarea NO "
    "escribís código, NO proponés cambios ni parches: solo explicás, en prosa y paso a paso, qué hace el código que "
    "se te muestra.")

PREGUNTAS = (
    "1. ¿Qué ocurre, paso a paso en este código, cuando un movimiento de tipo 'anulacion' referencia (con 'anula') a "
    "otro movimiento que también es una anulación y que ya fue aplicada? Seguí el valor de cada variable que "
    "intervenga.\n"
    "2. ¿Por qué, en ese caso, no se alcanza la rama `ref[\"tipo\"] == \"anulacion\"`?\n"
    "Respondé en prosa, en español. No escribas código ni propongas cambios.")


class ErrorSonda(Exception):
    pass


def _huella(texto: str) -> str:
    return hashlib.sha256(texto.encode()).hexdigest()


def _leer_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise ErrorSonda(f"no se pudo leer {p}: {e}") from None


def fuentes(corrida: Path) -> dict:
    """Lee (solo lectura) lo necesario de una corrida de `revisar-existente`. ErrorSonda si falta algo."""
    corrida = Path(corrida)
    ini = corrida / "inicial" / "solucion.py"
    if not ini.is_file():
        raise ErrorSonda(f"{corrida} no es una corrida de revisar-existente (falta inicial/solucion.py)")
    codigo = ini.read_text()
    prep = _leer_json(corrida / "inicial" / "preparacion.json")
    mision = prep.get("mision")
    if mision not in MISIONES:
        raise ErrorSonda(f"misión desconocida en la corrida: {mision!r}")
    revisiones = sorted(corrida.glob("revision-*/revision.json"), key=lambda p: int(p.parent.name.split("-")[1]))
    demostrada = None
    for p in revisiones:
        r = _leer_json(p)
        if r.get("estado") == "hecha" and (r.get("contraste") or {}).get("discrepancia") == "demostrada":
            demostrada = (p, r)
            break
    if demostrada is None:
        raise ErrorSonda("la corrida no tiene una revisión con la discrepancia demostrada")
    rep_p = corrida / "reparacion-1" / "reparacion.json"
    rep = _leer_json(rep_p)
    zonas = [tuple(z) for z in ((rep.get("localizacion") or {}).get("zonas") or [])]
    if not zonas:
        raise ErrorSonda("el primer intento de la corrida no registró zonas autorizadas (¿no es una corrida 7.3?)")
    if rep.get("base") not in (0, None):
        raise ErrorSonda("el primer intento no partió del código inicial")
    n = len(codigo.splitlines())
    if any(not (1 <= a <= b <= n) for a, b in zonas):
        raise ErrorSonda(f"zonas fuera del código inicial: {zonas}")
    return {"corrida": str(corrida), "mision": mision, "codigo": codigo, "zonas": zonas,
            "revision": demostrada[1]["revision"], "diagnostico": demostrada[1]["diagnostico"],
            "defecto": prep.get("defecto"),
            "archivos": {str(p.relative_to(corrida)): _huella(p.read_text())
                         for p in (ini, corrida / "inicial" / "preparacion.json", demostrada[0], rep_p)}}


def texto_diagnostico(diag: dict) -> str:
    """Los campos del diagnóstico del revisor, sin instrucciones de reparación."""
    p = diag["principal"]
    l = [RV.INICIO, f"Regla del contrato en juego: {p['regla_del_contrato'] or '—'}"]
    citas = [c["texto"] for c in p["citas"] if c["verificada"]]
    if citas:
        l.append("Líneas citadas (están en el código): " + "; ".join(f"«{c}»" for c in citas))
    l.append("Secuencia causal (hipótesis del revisor):")
    l += [f"    {i}. {paso}" for i, paso in enumerate(p["secuencia_causal"], 1)]
    if p["rama_inaccesible"]:
        l.append(f"Rama que no se alcanza: {p['rama_inaccesible']}")
    l.append(f"Propiedad del código señalada: {p['propiedad_a_cambiar']}")
    if diag.get("hechos"):
        l.append("Hechos señalados: " + " · ".join(diag["hechos"]))
    return prompts.clip("\n".join(l), RE.MAX_CARACTERES_DIAGNOSTICO) + "\n" + RV.FIN


def mensajes(f: dict) -> list[dict]:
    fabrica, _ = MISIONES[f["mision"]]
    m = fabrica()
    spec, funcion = m["spec"], m["spec"]["function"]
    diag = texto_diagnostico(f["diagnostico"])
    usuario = (f"CONTRATO:\n{m['description']}\n\n{prompts._sig(spec)}\n\n"
               "CÓDIGO (solo las zonas que el coordinador autorizó y un contexto de solo lectura; los números de línea "
               f"NO son parte del código):\n\n{PA.texto_zonas(f['codigo'], funcion, {'zonas': f['zonas']})}\n\n"
               f"{diag}\n\nPREGUNTAS:\n{PREGUNTAS}")
    return [{"role": "system", "content": SISTEMA_SONDA}, {"role": "user", "content": usuario}]


# Evaluación (separada del pedido) -----------------------------------------------------------------------------------

_ANUL = r"anulaci[oó]n(es)?"
_NEG = r"(\bno\b|\bnunca\b|\bjam[aá]s\b|\bning[uú]n[ao]?\b|\bexclu\w*|\bomit\w*|\bfalta\w*|\bausen\w*|\bsin\b|" \
       r"\bs[oó]lo\b|\bsolamente\b|\b[uú]nicamente\b|\bexcepto\b|\bsalvo\b)"
_REGISTRO = r"(guard|registr|agreg|añad|anad|almacen|incluy|inclu[iy]|insert|a[ñn]ad|anot|asign|escrib|conserv|ingres)"
_CONDICION = r"tipo\s*!=\s*[\"']anulacion[\"']|distint[oa] de (una )?[\"'«]?anulaci[oó]n|no (es|sea) (una )?[\"'«]?anulaci[oó]n"
_CAMBIOS = r"\b(deber[ií]a|hay que|habr[ií]a que|se puede corregir|para corregir|cambiar|modificar|agregar|reemplazar)\b"


def _oraciones(texto: str) -> list[str]:
    t = re.sub(r"```.*?(```|$)", " ", texto or "", flags=re.S)
    return [o.strip() for o in re.split(r"(?<=[.!?])\s+|\n+", t) if o.strip()]


def evaluar(respuesta: str, diagnostico_texto: str = "") -> dict:
    """Evaluación automática e INDICATIVA (la decisión final es de una lectura humana)."""
    ors = _oraciones(respuesta)
    con = lambda o, *pats: all(re.search(p, o, re.I) for p in pats)  # noqa: E731
    causa = [o for o in ors if "aplicados" in o and re.search(_ANUL, o, re.I) and (
        con(o, _NEG, _REGISTRO) or re.search(_CONDICION, o, re.I))]
    observa = [o for o in ors if "aplicados" in o and re.search(_ANUL, o, re.I) and re.search(_NEG, o, re.I)]
    ref_none = [o for o in ors if re.search(r"\bref\b", o) and re.search(r"\bNone\b|anulacion_inexistente", o)]
    diag_ors = _oraciones(diagnostico_texto)

    def parafrasis(o):
        return max((difflib.SequenceMatcher(None, o.lower(), d.lower()).ratio() for d in diag_ors), default=0.0)
    evidencia = [{"oracion": o, "parecido_con_el_revisor": round(parafrasis(o), 2)} for o in causa]
    return {
        "identifica_ausencia_en_aplicados": bool(causa),
        "observa_que_la_referida_no_esta": bool(observa),
        "explica_ref_none": bool(ref_none),
        "cita_la_condicion": bool(re.search(_CONDICION, respuesta or "", re.I)),
        "contiene_codigo": "```" in (respuesta or "") or bool(re.search(r"^\s*(def |if .*:\s*$|for .*:\s*$)",
                                                                       respuesta or "", re.M)),
        "propone_cambios": bool(re.search(_CAMBIOS, respuesta or "", re.I)),
        "evidencia": evidencia,
        "evidencia_observacion": observa[:5],
        "evidencia_ref_none": ref_none[:5],
        "solo_parafrasis_del_revisor": bool(evidencia) and all(e["parecido_con_el_revisor"] >= 0.8 for e in evidencia),
        "nota": "evaluación automática e indicativa; requiere lectura humana de respuesta.txt",
    }


# Ejecución ----------------------------------------------------------------------------------------------------------

def carpeta_de_sonda(carpeta: Path, corrida: Path) -> Path:
    carpeta, corrida = Path(carpeta).resolve(), Path(corrida).resolve()
    if carpeta == corrida or corrida in carpeta.parents:
        raise ErrorSonda("la sonda no escribe dentro de la corrida original: usá otra carpeta")
    for previo in ("taller.db", "taller"):
        if (carpeta / previo).exists():
            raise ErrorSonda(f"la carpeta {carpeta} tiene «{previo}» de un experimento anterior; usá una propia")
    base = carpeta / "experimento-7-4"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    nombre = time.strftime("sonda-%Y%m%d-%H%M%S")
    destino, i = base / nombre, 1
    while destino.exists():
        i += 1
        destino = base / f"{nombre}-{i}"
    destino.mkdir(mode=0o700)
    return destino


def correr(destino: Path, f: dict, cliente, gate, modelo, cancel: threading.Event | None = None) -> dict:
    """UNA inferencia; guarda pedido, respuesta literal, metadatos y evaluación. No modifica la corrida original."""
    destino = Path(destino)

    def guardar(nombre, contenido):
        (destino / nombre).write_text(contenido if isinstance(contenido, str) else
                                      json.dumps(contenido, ensure_ascii=False, indent=2, default=str))
    msgs = mensajes(f)
    fabrica, _ = MISIONES[f["mision"]]
    m = fabrica()
    auditoria = RE.auditar([msgs], m["spec"], f["defecto"], m["description"])
    guardar("origen.json", {"sonda": VERSION_SONDA, "corrida": f["corrida"], "mision": f["mision"],
                            "revision_usada": f["revision"], "zonas": f["zonas"], "archivos_leidos": f["archivos"],
                            "auditoria_de_fugas": auditoria})
    guardar("pedido.json", msgs)
    if not auditoria["sin_fugas"]:
        raise ErrorSonda("la auditoría encontró información prohibida en el pedido: " + ", ".join(auditoria["hallazgos"]))
    t0 = time.monotonic()
    meta = {"modelo": modelo.name, "opciones": {"num_ctx": modelo.num_ctx, "num_predict": modelo.num_predict,
                                                 "temperature": modelo.temperature}, "inicio": time.time()}
    try:
        gate.acquire("sonda 7.4", cancel)
        try:
            gen = cliente.chat(modelo, msgs, cancel)
        finally:
            gate.release()
    except (OllamaError, OSError) as e:
        meta.update(error=str(e), segundos=round(time.monotonic() - t0, 1))
        guardar("metadatos.json", meta)
        return {"ok": False, "destino": str(destino), "metadatos": meta, "evaluacion": None}
    meta.update(segundos=round(time.monotonic() - t0, 1), tokens_prompt=gen.prompt_tokens,
                tokens_generados=gen.eval_tokens, fin=gen.done_reason, cortada=bool(gen.truncated),
                tiempos=gen.timings())
    guardar("respuesta.txt", gen.text)
    guardar("metadatos.json", meta)
    ev = dict(evaluar(gen.text, texto_diagnostico(f["diagnostico"])), respuesta_cortada=bool(gen.truncated))
    guardar("evaluacion.json", ev)
    return {"ok": True, "destino": str(destino), "metadatos": meta, "evaluacion": ev}
