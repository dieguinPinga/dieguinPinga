"""Estudio de mutantes SIN MODELO: ¿cuánto de los desafíos 1 (detectar) y 2 (localizar) resuelve el coordinador solo?

Para cada mutante de un punto de la referencia:
* juez: ¿se diferencia de la referencia (parámetros visibles o de reserva)? Si no, es EQUIVALENTE y no cuenta;
* detección: ¿falla alguna verificación del SISTEMA (S1–S3)? Sin verificadores del modelo: eso es lo que mide;
* localización con la detección del sistema: ¿la instrucción mutada está en el top 1 / top 3?
* localización con detección ORÁCULO (los campos que el juez ve distintos): mide el localizador aislado, como cota
  de lo que lograría si la detección fuera perfecta. Esta segunda medición usa el juez y por eso NUNCA alimenta a
  un brazo: solo describe al localizador.
"""
from __future__ import annotations

from collections import Counter

from . import banco as B
from . import localizador as L
from . import verificaciones as V


def estudiar(ent, *, log=print, limite: int | None = None) -> dict:
    juez = ent.almacen("juez_mutantes")
    ref_id = juez.save("referencia", B.REFERENCIA, B.MANIFIESTO, author="banco")["id"]
    ref = {k: V._run(ent, juez, ref_id, p).get("output") for k, p in
           (("visible", B.PARAMS_VISIBLES), ("reserva", B.PARAMS_RESERVA))}
    mutantes = B.mutantes()[:limite] if limite else B.mutantes()
    filas = []
    for m in mutantes:
        mid = juez.save(f"mut_{m.id}", m.codigo, B.MANIFIESTO, author="banco")["id"]
        difs = {}
        for k, p in (("visible", B.PARAMS_VISIBLES), ("reserva", B.PARAMS_RESERVA)):
            run = V._run(ent, juez, mid, p)
            difs[k] = B.comparar(run.get("output"), ref[k]) if run["status"] == "ok" else \
                {"igual": False, "campos": ["(error)"]}
        equivalente = difs["visible"]["igual"] and difs["reserva"]["igual"]
        fila = {"id": m.id, "descripcion": m.descripcion, "lineas": list(m.lineas), "equivalente": equivalente,
                "campos_juez": sorted(set(difs["visible"]["campos"]) | set(difs["reserva"]["campos"]))}
        if not equivalente:
            ev = V.evaluar(ent, juez, mid, B.PARAMS_VISIBLES, [])
            fs = V.fallas(ev)
            fila["detectado"] = bool(fs)
            fila["verificaciones"] = [f["id"] for f in fs]
            fila["top_sistema"] = L.acierta(L.localizar(m.codigo, fs, k=3), m.lineas) if fs else None
            # descriptivo (no cambia el localizador): ¿la instrucción está en la rebanada, aunque más abajo?
            fila["pos_sistema"] = L.acierta(L.localizar(m.codigo, fs, k=99), m.lineas) if fs else None
            fila["omision"] = m.descripcion.startswith("instrucción eliminada")
            campos = [c for c in fila["campos_juez"] if not c.startswith("(")]
            oraculo = [{"campos": campos}] if campos else [{"campos": [], "lugar": (fs[0].get("lugar") if fs else None)}]
            pos = L.acierta(L.localizar(m.codigo, oraculo, k=3), m.lineas)
            fila["top_oraculo"] = pos
        filas.append(fila)
        log(f"  {m.id} {m.descripcion}: " + ("equivalente" if equivalente else
            f"detectado={fila['detectado']} top_sistema={fila['top_sistema']} top_oraculo={fila['top_oraculo']}"))
    return {"mutantes": filas, "resumen": resumir(filas)}


def resumir(filas: list[dict]) -> dict:
    no_eq = [f for f in filas if not f["equivalente"]]
    det = [f for f in no_eq if f.get("detectado")]

    def frac(n, d):
        return round(n / d, 3) if d else None

    por_tipo = {}
    for tipo in sorted({f["descripcion"].split(" (")[0] for f in no_eq}):
        g = [f for f in no_eq if f["descripcion"].startswith(tipo)]
        por_tipo[tipo] = {"no_equivalentes": len(g), "detectados": sum(1 for f in g if f.get("detectado"))}
    r = {
        "mutantes": len(filas), "equivalentes": len(filas) - len(no_eq), "no_equivalentes": len(no_eq),
        "detectados_por_el_sistema": len(det), "deteccion_sistema": frac(len(det), len(no_eq)),
        "verificaciones_que_detectan": dict(Counter(v for f in det for v in f["verificaciones"])),
        "top1_con_deteccion_sistema": frac(sum(1 for f in det if f.get("top_sistema") == 1), len(det)),
        "top3_con_deteccion_sistema": frac(sum(1 for f in det if f.get("top_sistema")), len(det)),
        "top1_con_deteccion_oraculo": frac(sum(1 for f in no_eq if f.get("top_oraculo") == 1), len(no_eq)),
        "top3_con_deteccion_oraculo": frac(sum(1 for f in no_eq if f.get("top_oraculo")), len(no_eq)),
        "por_tipo": por_tipo,
    }
    fallos = [f for f in det if not f.get("top_sistema")]
    r["fallas_de_localizacion"] = {
        "omision (la instrucción ya no existe)": sum(1 for f in fallos if f.get("omision")),
        "en la rebanada pero fuera del top 3": sum(1 for f in fallos if not f.get("omision") and f.get("pos_sistema")),
        "fuera de la rebanada": sum(1 for f in fallos if not f.get("omision") and not f.get("pos_sistema"))}
    r["R4_activada"] = r["deteccion_sistema"] is not None and r["deteccion_sistema"] < 0.5
    r["R5_activada"] = r["top3_con_deteccion_sistema"] is not None and r["top3_con_deteccion_sistema"] < 0.5
    return r
