"""Experimento 6: hipótesis del revisor CONTRASTADAS con un caso sintético (OFICINA.md, «Hipótesis verificables»).

El revisor puede proponer, junto con su hipótesis causal, un caso sintético MÍNIMO construido solo desde el contrato
público. El coordinador:
1. valida la estructura del caso contra el contrato (validador de la misión) y su tamaño;
2. comprueba que no coincida con la entrada de ningún caso oculto (si coincide, no revela nada: inconclusa);
3. lo ejecuta en el sandbox (Bubblewrap + seccomp + límites) con la implementación de REFERENCIA autorizada; si la
   referencia no lo procesa, el caso queda fuera del contrato (inconclusa);
4. lo ejecuta en el sandbox con el código del programador y compara las SALIDAS (no el veredicto del arnés):
   - distinta salida (o una excepción del candidato) → DISCREPANCIA DEMOSTRADA: el candidato se comporta mal en ese
     caso. La explicación causal del revisor queda «compatible, no demostrada»;
   - misma salida → DISCREPANCIA NO OBSERVADA: el candidato ya cumple en ese caso; la hipótesis no tiene respaldo ahí;
   - cualquier otro problema (salida demasiado grande, plazo, sandbox) → INCONCLUSA.
Campos separados: validez del caso · ejecución y salida (referencia, candidato) · discrepancia · hipótesis causal.
Los nueve casos originales siguen siendo la única evaluación: el caso sintético solo informa al programador.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

MAX_JSON_CASO = 4000


@dataclass
class Contraste:
    funcion: str
    referencia: str                       # código de la implementación de referencia autorizada
    validar: object                       # validar(args, kwargs) -> None | motivo
    entradas_privadas: set = field(default_factory=set)   # entradas canónicas de los casos ocultos (no se muestran)


def canonico(args, kwargs) -> str:
    return json.dumps({"args": args, "kwargs": kwargs or {}}, sort_keys=True, ensure_ascii=False)


def desde_mision(fabrica, referencia, validar) -> Contraste:
    spec = fabrica()["spec"]
    privadas = {canonico(c.get("args", []), c.get("kwargs", {})) for c in spec["cases"] if not c.get("visible", True)}
    return Contraste(spec["function"], referencia, validar, privadas)


def leer_caso(d) -> tuple[dict | None, str | None]:
    """Del JSON del revisor: {"args": [...], "kwargs": {...}, "que_deberia_pasar": "..."}."""
    if d is None:
        return None, "el revisor no propuso un caso sintético"
    if not isinstance(d, dict) or not isinstance(d.get("args", []), list) or not isinstance(d.get("kwargs", {}), dict):
        return None, "el caso sintético tiene que tener «args» (lista) y, opcional, «kwargs» (diccionario)"
    caso = {"args": d.get("args", []), "kwargs": d.get("kwargs", {}) or {},
            "que_deberia_pasar": str(d.get("que_deberia_pasar") or "")[:400]}
    if len(canonico(caso["args"], caso["kwargs"])) > MAX_JSON_CASO:
        return None, f"el caso sintético supera {MAX_JSON_CASO} caracteres: tiene que ser mínimo"
    return caso, None


def ejecutar(executor, codigo, funcion, caso) -> dict:
    """Ejecuta UNA llamada en el sandbox y devuelve solo la EJECUCIÓN y la SALIDA, sin comparar con nada.

    El arnés siempre compara contra una expectativa: se le pasa un centinela que ninguna salida JSON puede igualar
    y se ignora su veredicto («ok»/«fallo»), que acá no significa nada. ejecucion: completa | excepcion | timeout |
    error_de_carga | <estado del sandbox>.
    """
    spec = {"mode": "funcion", "function": funcion,
            "cases": [{"args": caso["args"], "kwargs": caso["kwargs"], "expect": {"__sin_expectativa__": True},
                       "visible": True}]}
    res = executor.run(codigo, spec, "")
    if res.status != "ok":
        return {"ejecucion": res.status, "salida": None, "detalle": res.reason}
    r = res.result or {}
    if r.get("load_error"):
        return {"ejecucion": "error_de_carga", "salida": None, "detalle": r["load_error"][-300:]}
    c = (r.get("cases") or [{}])[0]
    got = c.get("got")
    if c.get("status") in ("ok", "fallo"):            # devolvió un valor (el veredicto del arnés se ignora)
        if not isinstance(got, str) or got.endswith("…"):
            return {"ejecucion": "completa", "salida": None, "salida_recortada": True,
                    "detalle": "salida demasiado grande para compararla"}
        return {"ejecucion": "completa", "salida": json.loads(got), "detalle": None}
    if c.get("status") == "error" and isinstance(got, str) and got.startswith("excepción"):
        return {"ejecucion": "excepcion", "salida": got, "detalle": c.get("detail")}
    return {"ejecucion": c.get("status") or "desconocida", "salida": None, "detalle": c.get("detail")}


def contrastar(executor, contraste: Contraste, codigo: str, caso: dict | None, motivo_caso: str | None) -> dict:
    """Cuatro cosas SEPARADAS: validez del caso; ejecución y salida de la referencia y del candidato; discrepancia
    entre ambos; y validez de la hipótesis causal. Una discrepancia demuestra que el candidato se comporta mal en ese
    caso, pero NO demuestra la explicación causal del revisor: esa queda, a lo sumo, «compatible». Nunca lanza."""
    out = {"caso": caso, "validez": {"valido": False, "motivo": motivo_caso}, "referencia": None, "candidato": None,
           "discrepancia": "inconclusa", "comportamiento_candidato": "indeterminado",
           "hipotesis_causal": "sin_evaluar", "motivo": motivo_caso}
    if caso is None:
        return out
    motivo = contraste.validar(caso["args"], caso["kwargs"])
    if motivo:
        out["validez"] = {"valido": False, "motivo": f"no respeta el contrato: {motivo}"}
        out["motivo"] = out["validez"]["motivo"]
        return out
    if canonico(caso["args"], caso["kwargs"]) in contraste.entradas_privadas:
        out["validez"] = {"valido": True, "motivo": "coincide con un caso privado"}
        out["motivo"] = "el caso coincide con la entrada de un caso privado: no se ejecuta ni se revela nada"
        return out
    ref = ejecutar(executor, contraste.referencia, contraste.funcion, caso)
    out["referencia"] = ref
    if ref["ejecucion"] != "completa":
        out["validez"] = {"valido": False, "motivo": "la referencia no procesa el caso: queda fuera del contrato"}
        out["motivo"] = out["validez"]["motivo"] + f" ({ref['ejecucion']}: {ref.get('detalle')})"
        return out
    if ref.get("salida_recortada"):
        out["validez"] = {"valido": True, "motivo": "estructura del contrato y procesado por la referencia"}
        out["motivo"] = "la salida de la referencia es demasiado grande para compararla: el caso tiene que ser mínimo"
        return out
    out["validez"] = {"valido": True, "motivo": "estructura del contrato y procesado por la referencia"}
    cand = ejecutar(executor, codigo, contraste.funcion, caso)
    out["candidato"] = cand
    if cand["ejecucion"] in ("excepcion", "error_de_carga"):
        out.update(discrepancia="demostrada", comportamiento_candidato="incorrecto_en_el_caso",
                   motivo=f"el candidato termina con {cand['ejecucion']} donde el contrato pide un resultado")
    elif cand["ejecucion"] != "completa" or cand.get("salida_recortada"):
        out["motivo"] = f"el candidato no dio una salida comparable ({cand['ejecucion']}: {cand.get('detalle')})"
    elif cand["salida"] == ref["salida"]:
        out.update(discrepancia="no_observada", comportamiento_candidato="correcto_en_el_caso",
                   motivo="el candidato devuelve lo mismo que pide el contrato en este caso")
    else:
        out.update(discrepancia="demostrada", comportamiento_candidato="incorrecto_en_el_caso",
                   motivo="el candidato devuelve algo distinto de lo que pide el contrato en este caso")
    out["hipotesis_causal"] = {"demostrada": "compatible_no_demostrada",
                               "no_observada": "sin_respaldo_en_el_caso"}.get(out["discrepancia"], "sin_evaluar")
    return out


def diferencias(esperado, obtenido) -> list[str]:
    if isinstance(esperado, dict) and isinstance(obtenido, dict):
        return [k for k in esperado if k not in obtenido or obtenido[k] != esperado[k]]
    return []


def texto_para_programador(c: dict, funcion: str, propiedad: str, limite: int = 1800) -> str:
    """Solo con una discrepancia DEMOSTRADA: el caso (del revisor, público), lo que pide el contrato y lo que devolvió
    el candidato. Separa el comportamiento demostrado de la explicación causal, que sigue siendo una hipótesis."""
    caso = c["caso"]
    llamada = ", ".join([json.dumps(a, ensure_ascii=False) for a in caso["args"]]
                        + [f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in caso["kwargs"].items()])
    esperado = c["referencia"]["salida"]
    obtenido = c["candidato"]["salida"]
    dif = diferencias(esperado, obtenido)
    lineas = ["════ EVIDENCIA VERIFICADA POR EL COORDINADOR (caso sintético del revisor; NO es una prueba oculta) ════",
              f"Caso construido por el revisor a partir del contrato: {funcion}({llamada})",
              "Según el contrato (implementación de referencia, en el sandbox) debe devolver: "
              + json.dumps(esperado, ensure_ascii=False),
              "Tu código devuelve: " + (obtenido if isinstance(obtenido, str) else json.dumps(obtenido, ensure_ascii=False))
              + (f"   (claves distintas: {', '.join(repr(k) for k in dif)})" if dif else ""),
              "DEMOSTRADO: en esta entrada tu código no cumple el contrato.",
              f"NO DEMOSTRADO: la causa. El revisor cree que la propiedad responsable es: {propiedad}. Es compatible con "
              "la discrepancia, pero verificá vos la causa en tu código.",
              "Corregí tu código para que este caso dé lo que pide el contrato, sin romper lo que ya funciona.",
              "════ FIN DE LA EVIDENCIA ════"]
    texto = "\n".join(lineas)
    if len(texto) > limite:          # salida grande: solo lo esencial
        lineas[2] = "Según el contrato (implementación de referencia) la salida es distinta de la tuya."
        texto = "\n".join(lineas)[:limite]
    return texto
