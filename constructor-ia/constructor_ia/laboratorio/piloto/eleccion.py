"""Experimento de elección (PILOTO.md, «Experimento de elección: Etapas 0 y 1»).

Pregunta: ¿la evidencia de ejecución (traza.py) ayuda al modelo a elegir la instrucción que causa las fallas?

* Etapa 0 (sin modelo): construye los casos y mide si la traza DISTINGUE a la candidata sembrada de las otras.
* Etapa 1 (con modelo): para cada caso, rotación del orden de las candidatas y repetición, el modelo recibe el MISMO
  prompt en dos condiciones, control y tratamiento; la única diferencia es la parte «traza» del tratamiento (se
  comprueba en cada ensayo). Una llamada corta: «¿cuál candidata es la causa?». Sin parches.

Aislamiento del juez: los casos (verificaciones que fallan, candidatas del localizador, traza) se construyen solo
con el instrumento defectuoso, una verificación fija igual en las dos condiciones y los datos. La identidad de la
instrucción sembrada y la referencia se usan solo para (a) elegir qué casos entran (la sembrada debe estar entre
las candidatas; mutantes no equivalentes) y (b) puntuar.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import random
import re
import textwrap
import time
from pathlib import Path

from .. import protocolo as PR
from . import banco as B
from . import localizador as L
from . import parches as P
from . import traza as T
from . import v2 as V2
from . import verificaciones as V
from ...instrumentos.medicion import ejecutar

ROTACIONES = 3
REPETICIONES = 2
MAX_CASOS = 8
SEMILLA_ORDEN = 20261022
UMBRAL_DIFERENCIA = 0.20
UMBRAL_P = 0.05
LETRAS = "ABC"
CONDICIONES = ("control", "tratamiento")

# Verificación FIJA para construir los casos (igual en control y tratamiento): reemplaza la detección del modelo, que
# no es lo que se mide acá. Vuelve a medir con SQL propio el total leído y la suma de cada ventana informada.
VERIFICADOR_FIJO = '''
import datetime

def medir(db, p):
    s = p["salida"]
    ancho = int(p["parametros"].get("ancho_min", 15))
    malos, ejemplos = set(), []
    total = int(db.consultar("SELECT COUNT(*) FROM lecturas").filas[0][0])
    if s["cobertura"].get("leidas") != total:
        malos.update(["leidas", "filas_analizadas"])
        ejemplos.append({"salida": s["cobertura"].get("leidas"), "medido": total})
    for w in s["resultado"]["ventanas"]:
        ini = datetime.datetime.strptime(w["inicio"], "%Y-%m-%d %H:%M")
        fin = ini + datetime.timedelta(minutes=ancho)
        r = db.consultar("SELECT COALESCE(SUM(valor), 0) FROM lecturas WHERE valor > 0.5 AND valor < 900 AND "
                         "momento >= '" + ini.strftime("%Y-%m-%d %H:%M:%S") + "' AND momento < '"
                         + fin.strftime("%Y-%m-%d %H:%M:%S") + "'")
        medido = float(r.filas[0][0])
        if abs(medido - w["suma"]) > 0.01:
            malos.update(["inicio", "suma"])
            ejemplos.append({"inicio": w["inicio"], "suma": w["suma"], "medido": medido})
    return {"resultado": {"ok": not malos, "campos": sorted(malos), "contraejemplos": ejemplos[:3]},
            "cobertura": {"filas_analizadas": total}, "limitaciones": []}
'''


def clase_de(descripcion: str) -> str:
    if descripcion.startswith("D2"):
        return "sobrescritura"
    if descripcion.startswith("D1") or descripcion.startswith(("constante", "operador")):
        return "cálculo"
    if descripcion.startswith(("comparación", "«not»")):
        return "condición"
    if descripcion.startswith("instrucción eliminada"):
        return "omisión"
    return "otra"


def _vector(r: dict) -> tuple:
    veces = r["veces"]
    return (0 if veces == 0 else 1 if veces == 1 else 2, r["compuesta"] and veces > 0 and r["verdadera"] == veces,
            r["compuesta"] and veces > 0 and r["verdadera"] == 0, bool(r["pierde"]), r["constante_reemplaza"])


def _anomalo(r: dict) -> bool:
    return r["veces"] == 0 or r["constante_reemplaza"] or (r["compuesta"] and r["veces"] > 0 and
                                                           r["verdadera"] in (0, r["veces"]))


# Etapa 0: casos y calidad de la evidencia ---------------------------------------------------------------------

def construir_casos(ent, *, incluir_mutantes: bool = True, externos: list[tuple] | None = None, solo_externos: bool = False,
                    log=print) -> dict:
    """externos: [(id, descripcion, codigo, (desde, hasta))] escritos por otros (evaluación a ciegas de E0); con
    solo_externos no se agregan D1, D2 ni los mutantes del banco."""
    juez = ent.almacen(f"eleccion_juez_{int(time.time() * 1000)}")
    trabajo = ent.almacen(f"eleccion_casos_{int(time.time() * 1000)}")
    ref = {k: V._run(ent, juez, juez.save("referencia", B.REFERENCIA, B.MANIFIESTO, author="banco")["id"], p)
           .get("output") for k, p in (("visible", B.PARAMS_VISIBLES), ("reserva", B.PARAMS_RESERVA))}
    fuentes = [(d.id, f"{d.id} {d.titulo}", d.codigo, B.lineas_de(d.codigo, d.sembrada), B.REFERENCIA)
               for d in B.DEFECTOS.values()]
    if incluir_mutantes and not solo_externos:
        ref_unparse = ast.unparse(ast.parse(B.REFERENCIA)) + "\n"
        fuentes += [(m.id, m.descripcion, m.codigo, m.lineas, ref_unparse) for m in B.mutantes()]
    if solo_externos:
        fuentes = []
    fuentes += [(i, d, c, tuple(l), B.REFERENCIA) for i, d, c, l in (externos or [])]
    vid = trabajo.save("verificador_fijo", VERIFICADOR_FIJO, {"proposito": "verificador"}, author="laboratorio")["id"]
    casos = []
    for cid, desc, codigo, sembrada, referencia in fuentes:
        caso = {"id": cid, "descripcion": desc, "clase": clase_de(desc), "codigo": codigo,
                "sembrada": list(sembrada), "referencia_lineas": sorted(
                    {x.strip() for x in referencia.splitlines() if x.strip()} -
                    {x.strip() for x in codigo.splitlines() if x.strip()})}
        jid = juez.save(f"c_{cid}", codigo, B.MANIFIESTO, author="banco")["id"]       # solo para la equivalencia
        caso["equivalente"] = all(B.comparar(V._run(ent, juez, jid, p).get("output"), ref[k])["igual"]
                                  for k, p in (("visible", B.PARAMS_VISIBLES), ("reserva", B.PARAMS_RESERVA)))
        if caso["equivalente"]:
            casos.append(caso)
            continue
        iid = trabajo.save(f"c_{cid}", codigo, B.MANIFIESTO, author="banco")["id"]
        ev = V.evaluar(ent, trabajo, iid, B.PARAMS_VISIBLES, [{"id": "VF", "instrumento_id": vid}])
        fallas = V.fallas(ev)
        caso.update({"detectado": bool(fallas), "salida": ev["salida"],
                     "fallas": [{k: f.get(k) for k in ("id", "autor", "campos", "detalle", "lugar")} for f in fallas],
                     "texto_fallas": V.texto_fallas(ev)})
        if fallas:
            cands = L.localizar(codigo, fallas, k=3)
            caso["candidatas"] = [c["unidad"].lineno for c in cands]
            caso["motivos"] = [c["motivo"] for c in cands]
            caso["sembrada_en"] = L.acierta(cands, tuple(sembrada))          # evaluación: no entra a ningún prompt
            unidades = [c["unidad"] for c in cands]
            run = ejecutar(executor=ent.executor, broker=trabajo.broker, almacen=trabajo, cfg=ent.cfg,
                           instrument_id=iid, params=B.PARAMS_VISIBLES, schema=ent.schema,
                           traza=T.pedido(unidades, codigo))
            caso["traza"] = (run.get("verdict") or {}).get("traza")
            rs = [T.rasgos(caso["traza"], u) for u in unidades]
            if caso["sembrada_en"]:
                i = caso["sembrada_en"] - 1
                otros = [v for j, v in enumerate(map(_vector, rs)) if j != i]
                caso["traza_distingue"] = _vector(rs[i]) not in otros
                caso["senal_exclusiva"] = _anomalo(rs[i]) and not any(_anomalo(r) for j, r in enumerate(rs) if j != i)
        casos.append(caso)
        log(f"  {cid} {desc[:50]}: " + ("equivalente" if caso["equivalente"] else
            f"detectado={caso['detectado']} sembrada_en={caso.get('sembrada_en')} "
            f"distingue={caso.get('traza_distingue')} señal={caso.get('senal_exclusiva')}"))
    ent.liberar(juez)
    ent.liberar(trabajo)
    return {"casos": casos, "etapa0": resumir_etapa0(casos), "seleccion": seleccionar(casos)}


def resumir_etapa0(casos: list[dict]) -> dict:
    no_eq = [c for c in casos if not c["equivalente"]]
    det = [c for c in no_eq if c.get("detectado")]
    en_top = [c for c in det if c.get("sembrada_en")]
    por_clase = {}
    for cl in sorted({c["clase"] for c in en_top}):
        g = [c for c in en_top if c["clase"] == cl]
        por_clase[cl] = {"casos": len(g), "traza_distingue": sum(1 for c in g if c.get("traza_distingue")),
                         "senal_exclusiva": sum(1 for c in g if c.get("senal_exclusiva"))}
    return {"no_equivalentes": len(no_eq), "detectados": len(det), "sembrada_entre_candidatas": len(en_top),
            "traza_distingue": sum(1 for c in en_top if c.get("traza_distingue")),
            "senal_exclusiva": sum(1 for c in en_top if c.get("senal_exclusiva")), "por_clase": por_clase}


def seleccionar(casos: list[dict], maximo: int = MAX_CASOS) -> list[str]:
    """Regla fija (preregistrada): D1 y D2 si la sembrada está entre sus candidatas; después mutantes con la
    sembrada entre las candidatas, en orden de id, prefiriendo LÍNEAS SEMBRADAS DISTINTAS (un +1 y un −1 sobre la
    misma constante son casi el mismo caso); si faltan, se completa con los restantes en orden de id."""
    aptos = [c for c in casos if not c["equivalente"] and c.get("sembrada_en")]
    elegidos = [c["id"] for c in aptos if c["id"] in ("D1", "D2")]
    mutantes = sorted((c for c in aptos if c["id"].startswith("M")), key=lambda c: c["id"])
    lineas = set()
    for c in mutantes:
        if len(elegidos) < maximo and c["sembrada"][0] not in lineas:
            elegidos.append(c["id"])
            lineas.add(c["sembrada"][0])
    for c in mutantes:
        if len(elegidos) < maximo and c["id"] not in elegidos:
            elegidos.append(c["id"])
    return elegidos


# Etapa 1: elección pareada --------------------------------------------------------------------------------------

def _rotar(seq, r):
    r %= len(seq)
    return seq[r:] + seq[:r]


def semilla(caso_id: str, rotacion: int, repeticion: int) -> int:
    return int(hashlib.sha256(f"{caso_id}|{rotacion}|{repeticion}".encode()).hexdigest()[:8], 16) % (2 ** 31)


def _unidad_limpia(codigo: str, u: P.Unidad) -> str:
    lines = codigo.splitlines()
    return textwrap.dedent("\n".join(lines[u.lineno - 1:u.end_lineno]))


def _contexto(codigo: str, u: P.Unidad, n: int = 2) -> str:
    lines = codigo.splitlines()
    a, b = max(1, u.lineno - n), min(len(lines), u.end_lineno + n)
    return "\n".join(f"{i:4d}  {lines[i - 1]}" for i in range(a, b + 1))


def partes(caso: dict, orden: list[int], condicion: str) -> list[tuple[str, str]]:
    codigo = caso["codigo"]
    unidades = [P.unidad_en(codigo, ln) for ln in orden]
    bloques = []
    for letra, u in zip(LETRAS, unidades):
        bloques.append(f"{letra}) instrucción de la línea {u.lineno}:\n```python\n{_unidad_limpia(codigo, u)}\n```\n"
                       f"Contexto numerado (no lo copies):\n{_contexto(codigo, u)}")
    out = [("coordinador", f"Un instrumento de medición escrito en Python responde: {B.PREGUNTA}\n"
                           f"Parámetros: {B.PARAMS_VISIBLES}\n"),
           ("evidencia", "Salida actual: " + json.dumps(caso["salida"], ensure_ascii=False)[:1200] + "\n"),
           ("evidencia", "Verificaciones que fallan:\n" + caso["texto_fallas"] + "\n"),
           ("coordinador", "Instrucciones candidatas (una de ellas es la causa de las fallas):\n\n"
                           + "\n\n".join(bloques) + "\n")]
    if condicion == "tratamiento":
        out.append(("traza", T.texto(caso["traza"], [{"unidad": u} for u in unidades], LETRAS)))
    out.append(("coordinador", "¿Cuál de las candidatas es la causa de las fallas? Respondé EXACTAMENTE:\n"
                               "CANDIDATO: <letra>\nMOTIVO: <una frase>"))
    return out


RE_CAND = re.compile(r"CANDIDATO:\s*([A-Ca-c]\b|\d+)")


def interpretar(texto: str | None, orden: list[int]) -> int | None:
    """Posición (0-based) elegida, o None. Acepta la letra o el número de línea de UNA candidata (igual que v2)."""
    m = RE_CAND.search(texto or "")
    if not m:
        return None
    v = m.group(1)
    if v.isalpha():
        i = LETRAS.index(v.upper())
        return i if i < len(orden) else None
    coinciden = [i for i, ln in enumerate(orden) if ln == int(v)]
    return coinciden[0] if len(coinciden) == 1 else None


def ensayos_planificados(casos: dict, seleccion: list[str], repeticiones: int = REPETICIONES) -> list[dict]:
    plan = []
    for cid in seleccion:
        c = next(x for x in casos if x["id"] == cid)
        for r in range(ROTACIONES):
            orden = _rotar(list(c["candidatas"]), r)
            for rep in range(1, repeticiones + 1):
                for cond in CONDICIONES:
                    plan.append({"clave": f"{cid}|{r}|{rep}|{cond}", "caso": cid, "rotacion": r,
                                 "repeticion": rep, "condicion": cond, "orden": orden,
                                 "semilla": semilla(cid, r, rep)})
    random.Random(SEMILLA_ORDEN).shuffle(plan)
    return plan


def _auditar(caso, ps) -> list[str]:
    fugas = []
    for origen, texto in ps:
        if origen in ("coordinador", "traza") and any(x in texto for x in caso["referencia_lineas"]):
            fugas.append(f"una línea de la referencia aparece en una parte «{origen}»")
    return fugas


def correr(cfg, *, crear_modelo, salida: Path, casos_info: dict, repeticiones: int = REPETICIONES,
           limite: int | None = None, guardia=None, log=print) -> dict:
    """Corre (o reanuda) la Etapa 1. limite: cuántos ensayos NUEVOS correr como máximo (prueba de rendimiento)."""
    salida = Path(salida)
    salida.mkdir(parents=True, exist_ok=True, mode=0o700)
    (salida / "casos.json").write_text(json.dumps(casos_info, ensure_ascii=False, indent=1, default=str))
    archivo = salida / "ensayos.jsonl"
    hechos = {json.loads(x)["clave"]: json.loads(x) for x in archivo.read_text().splitlines()} \
        if archivo.exists() else {}
    casos = {c["id"]: c for c in casos_info["casos"]}
    plan = ensayos_planificados(casos_info["casos"], casos_info["seleccion"], repeticiones)
    guardia = guardia or V2.Guardia(minimo_mb=0)
    modelo = V2.ModeloV2(crear_modelo(), 10 ** 6, 10 ** 9, guardia)
    modelo.system = PR.SYSTEM.replace("{resultado_kb}", str(cfg.instrumentos_resultado_kb))
    nuevos, seguidas, estado, interrupciones = 0, 0, "completo", []
    for e in plan:
        if e["clave"] in hechos:
            continue
        if limite is not None and nuevos >= limite:
            estado = "limite"
            break
        caso = casos[e["caso"]]
        control, ps = partes(caso, e["orden"], "control"), partes(caso, e["orden"], e["condicion"])
        sin_traza = [x for x in ps if x[0] != "traza"]
        if sin_traza != control:
            raise RuntimeError("el prompt de tratamiento difiere del de control en algo más que la traza")
        texto = _texto(ps)
        try:
            resp, call = modelo.con_partes(ps).pedir("eleccion", texto, json_out=False, corto=True,
                                                     seed=e["semilla"])
        except (V2.EntornoInterrumpido, V2.MemoriaInsuficiente, V2.ContextoInsuficiente) as ex:
            interrupciones.append({"clave": e["clave"], "tipo": type(ex).__name__, "detalle": str(ex)[:300]})
            with (salida / "interrupciones.jsonl").open("a") as f:
                f.write(json.dumps(interrupciones[-1], ensure_ascii=False) + "\n")
            seguidas += 1
            if not isinstance(ex, V2.EntornoInterrumpido) or seguidas >= 2:
                estado = "detenido_por_entorno"
                break
            continue
        seguidas = 0
        pos = interpretar(resp, e["orden"])
        elegida = e["orden"][pos] if pos is not None else None
        u = P.unidad_en(caso["codigo"], elegida) if elegida else None
        correcto = bool(u and u.lineno <= caso["sembrada"][1] and caso["sembrada"][0] <= u.edit_end)
        reg = {**e, "respuesta": resp, "posicion_elegida": pos, "linea_elegida": elegida, "correcto": correcto,
               "ilegible": pos is None, "fugas": _auditar(caso, ps), "tokens_prompt": call.get("tokens_prompt"),
               "tokens_generados": call.get("tokens_generados"), "reloj_ms": call.get("reloj_ms"),
               "memoria": call.get("memoria"), "partes": ps, "ts": time.time()}
        with archivo.open("a") as f:
            f.write(json.dumps(reg, ensure_ascii=False, default=str) + "\n")
        hechos[e["clave"]] = reg
        nuevos += 1
        log(f"  {len(hechos)}/{len(plan)} {e['clave']}: {'✓' if correcto else '✗'} "
            f"({(call.get('reloj_ms') or 0) / 1000:.1f} s)")
    res = {"estado": estado, "planificados": len(plan), "hechos": len(hechos), "interrupciones": interrupciones,
           "analisis": analizar(list(hechos.values()), casos)}
    (salida / "eleccion.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    (salida / "resumen.md").write_text(markdown(res, casos_info))
    return res


def _texto(ps) -> str:
    return "\n".join(t for _, t in ps)


# Análisis preregistrado ------------------------------------------------------------------------------------------

def mcnemar_exacto(b: int, c: int) -> float:
    """p bilateral exacto de McNemar (binomial sobre los pares discordantes)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def analizar(ensayos: list[dict], casos: dict) -> dict:
    pares: dict = {}
    for e in ensayos:
        pares.setdefault((e["caso"], e["rotacion"], e["repeticion"]), {})[e["condicion"]] = e
    completos = [p for p in pares.values() if len(p) == 2]

    def acc(xs):
        return round(sum(1 for x in xs if x["correcto"]) / len(xs), 3) if xs else None

    ctrl = [p["control"] for p in completos]
    trat = [p["tratamiento"] for p in completos]
    b = sum(1 for p in completos if not p["control"]["correcto"] and p["tratamiento"]["correcto"])
    c = sum(1 for p in completos if p["control"]["correcto"] and not p["tratamiento"]["correcto"])
    pval = mcnemar_exacto(b, c)
    dif = None if not completos else round(acc(trat) - acc(ctrl), 3)
    if dif is None:
        veredicto = "sin datos"
    elif dif >= UMBRAL_DIFERENCIA and pval < UMBRAL_P:
        veredicto = "ayuda"
    elif dif <= -UMBRAL_DIFERENCIA and pval < UMBRAL_P:
        veredicto = "perjudica"
    else:
        veredicto = "inconcluso"
    por_clase = {}
    for cl in sorted({casos[p["control"]["caso"]]["clase"] for p in completos}):
        g = [p for p in completos if casos[p["control"]["caso"]]["clase"] == cl]
        por_clase[cl] = {"pares": len(g), "control": acc([p["control"] for p in g]),
                         "tratamiento": acc([p["tratamiento"] for p in g])}
    posiciones = {cond: [sum(1 for e in ensayos if e["condicion"] == cond and e["posicion_elegida"] == i)
                         for i in range(3)] for cond in CONDICIONES}
    por_rotacion = {cond: [acc([e for e in ensayos if e["condicion"] == cond and e["rotacion"] == r])
                           for r in range(ROTACIONES)] for cond in CONDICIONES}
    largos = {"sembrada": [], "otras": []}
    for e in (x for x in ensayos if x["condicion"] == "tratamiento"):
        caso = casos[e["caso"]]
        for ln in e["orden"]:
            u = P.unidad_en(caso["codigo"], ln)
            t = T.texto(caso["traza"], [{"unidad": u}], "A")
            sembrada = u.lineno <= caso["sembrada"][1] and caso["sembrada"][0] <= u.edit_end
            largos["sembrada" if sembrada else "otras"].append(len(t))
    tiempos = [e["reloj_ms"] / 1000 for e in ensayos if e.get("reloj_ms")]
    return {"pares": len(completos), "acierto_control": acc(ctrl), "acierto_tratamiento": acc(trat),
            "diferencia": dif, "b_solo_tratamiento": b, "c_solo_control": c, "p_mcnemar": round(pval, 4),
            "veredicto": veredicto, "por_clase": por_clase, "posicion_elegida": posiciones,
            "acierto_por_rotacion": por_rotacion,
            "ilegibles": {cond: sum(1 for e in ensayos if e["condicion"] == cond and e["ilegible"])
                          for cond in CONDICIONES},
            "fugas": sum(len(e["fugas"]) for e in ensayos),
            "largo_medio_evidencia": {k: round(sum(v) / len(v)) if v else None for k, v in largos.items()},
            "segundos_por_llamada": round(sum(tiempos) / len(tiempos), 1) if tiempos else None}


def markdown(res: dict, casos_info: dict) -> str:
    a, e0 = res["analisis"], casos_info["etapa0"]
    L = ["# Experimento de elección — ¿la evidencia de ejecución ayuda a elegir la causa?", "",
         "Preregistro: PILOTO.md, «Experimento de elección». Una sola diferencia entre condiciones: la traza.", "",
         f"Estado: {res['estado']} · ensayos {res['hechos']}/{res['planificados']} · pares completos {a['pares']}", "",
         "## Etapa 0 (sin modelo)", "",
         f"- No equivalentes: {e0['no_equivalentes']}; detectados por las verificaciones: {e0['detectados']}; "
         f"con la sembrada entre las 3 candidatas: {e0['sembrada_entre_candidatas']}.",
         f"- La traza distingue a la sembrada (rasgos únicos): {e0['traza_distingue']}; señal anómala exclusiva: "
         f"{e0['senal_exclusiva']}.", f"- Por clase: {e0['por_clase']}.",
         f"- Casos elegidos para la Etapa 1: {', '.join(casos_info['seleccion'])}.", "",
         "## Etapa 1", "",
         f"- Acierto: control {a['acierto_control']} · tratamiento {a['acierto_tratamiento']} · diferencia "
         f"{a['diferencia']}.",
         f"- McNemar exacto: {a['b_solo_tratamiento']} pares acertados solo con traza, {a['c_solo_control']} solo "
         f"sin traza; p = {a['p_mcnemar']}.",
         f"- **Veredicto preregistrado: {a['veredicto']}** (ayuda = diferencia ≥ {UMBRAL_DIFERENCIA:.0%} y "
         f"p < {UMBRAL_P}; si no, inconcluso: no mostrar un efecto no es mostrar que no existe).",
         f"- Por clase: {a['por_clase']}.",
         f"- Posición elegida (1.ª, 2.ª, 3.ª): {a['posicion_elegida']} · acierto por rotación: "
         f"{a['acierto_por_rotacion']}.",
         f"- Respuestas ilegibles: {a['ilegibles']} · fugas: {a['fugas']} · largo medio de la evidencia "
         f"(sembrada / otras): {a['largo_medio_evidencia']} · segundos por llamada: {a['segundos_por_llamada']}.",
         ""]
    if res["interrupciones"]:
        L += ["## Interrupciones del entorno", ""] + [f"- {i['clave']}: {i['tipo']} — {i['detalle']}"
                                                       for i in res["interrupciones"]] + [""]
    return "\n".join(L)
