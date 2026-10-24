"""Experimento 6: hipótesis del revisor CONTRASTADAS con un caso sintético (OFICINA.md, «Hipótesis verificables»).

El revisor puede proponer, junto con su hipótesis causal, un caso sintético MÍNIMO construido solo desde el contrato
público. El coordinador:
1. valida la estructura del caso contra el contrato (validador de la misión) y su tamaño;
2. comprueba que no coincida con la entrada de ningún caso oculto (si coincide, no revela nada: inconclusa);
3. lo ejecuta en el sandbox (Bubblewrap + seccomp + límites) con la implementación de REFERENCIA autorizada; si la
   referencia no lo procesa, el caso queda fuera del contrato (inconclusa);
4. lo ejecuta en el sandbox con el código del programador y compara:
   - distinta salida (o una excepción) → hipótesis CONFIRMADA: hay una discrepancia demostrada;
   - misma salida → REFUTADA: en ese caso el código ya cumple; la hipótesis queda sin confirmar;
   - cualquier otro problema (salida demasiado grande, plazo, sandbox) → INCONCLUSA.
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


def _ejecutar(executor, codigo, funcion, caso, esperado):
    spec = {"mode": "funcion", "function": funcion,
            "cases": [{"args": caso["args"], "kwargs": caso["kwargs"], "expect": esperado, "visible": True}]}
    res = executor.run(codigo, spec, "")
    if res.status != "ok":
        return {"estado": res.status, "detalle": res.reason}, None
    r = res.result or {}
    if r.get("load_error"):
        return {"estado": "error_de_carga", "detalle": r["load_error"][-300:]}, None
    c = (r.get("cases") or [{}])[0]
    return {"estado": c.get("status"), "ok": c.get("ok"), "got": c.get("got"), "detalle": c.get("detail")}, c


def contrastar(executor, contraste: Contraste, codigo: str, caso: dict | None, motivo_caso: str | None) -> dict:
    """Clasifica la hipótesis: confirmada, refutada o inconclusa. Nunca lanza."""
    out = {"caso": caso, "validez": {"valido": False, "motivo": motivo_caso}, "referencia": None, "programa": None,
           "resultado": "inconclusa", "motivo": motivo_caso}
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
    # 1) referencia autorizada, en el sandbox: su salida es lo que el contrato pide para ese caso.
    ref, c = _ejecutar(executor, contraste.referencia, contraste.funcion, caso, None)
    out["referencia"] = ref
    if c is None or ref["estado"] not in ("ok", "fallo"):
        out["validez"] = {"valido": False, "motivo": "la referencia no procesa el caso: queda fuera del contrato"}
        out["motivo"] = out["validez"]["motivo"] + f" ({ref['estado']}: {ref.get('detalle')})"
        return out
    got = ref.get("got")
    if not isinstance(got, str) or got.endswith("…"):
        out["motivo"] = "la salida de la referencia es demasiado grande para compararla: el caso tiene que ser mínimo"
        return out
    esperado = json.loads(got)
    out["referencia"]["salida"] = esperado
    out["validez"] = {"valido": True, "motivo": "estructura del contrato y procesado por la referencia"}
    # 2) el programa del programador, en el sandbox, contra esa salida.
    prog, c = _ejecutar(executor, codigo, contraste.funcion, caso, esperado)
    out["programa"] = prog
    if c is None:
        out["motivo"] = f"el programa no se pudo ejecutar en el sandbox ({prog['estado']}): {prog.get('detalle')}"
        if prog["estado"] == "error_de_carga":
            out.update(resultado="confirmada", motivo="el programa no carga: " + str(prog.get("detalle"))[:200])
        return out
    if prog.get("ok"):
        out.update(resultado="refutada", motivo="el programa devuelve lo mismo que pide el contrato en este caso")
    elif prog["estado"] in ("fallo", "error"):
        out.update(resultado="confirmada", motivo="el programa no devuelve lo que pide el contrato en este caso")
        out["programa"]["salida"] = _leer(prog.get("got"))
    else:
        out["motivo"] = f"el programa terminó «{prog['estado']}»: {prog.get('detalle')}"
    return out


def _leer(got):
    try:
        return json.loads(got) if isinstance(got, str) and not got.endswith("…") else got
    except ValueError:
        return got


def diferencias(esperado, obtenido) -> list[str]:
    if isinstance(esperado, dict) and isinstance(obtenido, dict):
        return [k for k in esperado if k not in obtenido or obtenido[k] != esperado[k]]
    return []


def texto_para_programador(c: dict, funcion: str, propiedad: str, limite: int = 1800) -> str:
    """Solo para una hipótesis CONFIRMADA: el caso (del revisor, público), lo que pide el contrato y lo que devolvió."""
    caso = c["caso"]
    llamada = ", ".join([json.dumps(a, ensure_ascii=False) for a in caso["args"]]
                        + [f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in caso["kwargs"].items()])
    esperado = c["referencia"]["salida"]
    obtenido = (c["programa"] or {}).get("salida", (c["programa"] or {}).get("got"))
    dif = diferencias(esperado, obtenido)
    lineas = ["════ EVIDENCIA VERIFICADA POR EL COORDINADOR (caso sintético del revisor; NO es una prueba oculta) ════",
              f"Caso construido por el revisor a partir del contrato: {funcion}({llamada})",
              "Según el contrato (implementación de referencia, en el sandbox) debe devolver: "
              + json.dumps(esperado, ensure_ascii=False),
              "Tu código devuelve: " + (json.dumps(obtenido, ensure_ascii=False) if not isinstance(obtenido, str)
                                        else obtenido)
              + (f"   (claves distintas: {', '.join(repr(k) for k in dif)})" if dif else ""),
              f"PROPIEDAD INCUMPLIDA (hipótesis del revisor, CONFIRMADA por este caso): {propiedad}",
              "Corregí tu código para que este caso dé lo que pide el contrato, sin romper lo que ya funciona.",
              "════ FIN DE LA EVIDENCIA ════"]
    texto = "\n".join(lineas)
    if len(texto) > limite:          # salida grande: solo lo esencial
        lineas[2] = "Según el contrato (implementación de referencia) la salida es distinta de la tuya."
        texto = "\n".join(lineas)[:limite]
    return texto
