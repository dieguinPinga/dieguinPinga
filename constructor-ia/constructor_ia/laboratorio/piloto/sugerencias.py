"""Experimento de sugerencias (PILOTO.md, «Experimento de sugerencias»): ¿la sugerencia de la PRIMERA llamada ayuda
o perjudica el diagnóstico de Qwen?

Dos condiciones, con TODO lo demás idéntico (ciclo v1.3.4, su formato, verificaciones y juez):
- «localizador»: la sugerencia de v1.3.3 (la primera hipótesis, marcada con «>>» y «← sugerida», sin origen);
- «evidencia»: la de v1.3.4 (sin regla congelada que responda, ninguna línea sugerida ni marcada).

El plan es FIJO (preregistrado): defectos, orden alternado de las condiciones, una llamada por defecto y condición y
la misma semilla en cada pareja. Antes de llamar, el coordinador arma los dos prompts de cada pareja y comprueba que
difieran SOLO en lo que corresponde a la sugerencia; si no, no se llama a nadie.
"""
from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from pathlib import Path

from . import ciclo as C
from . import e0 as E0

VERSION = "experimento de sugerencias v1 (ciclo v1.3.4)"
LLAMADAS_POR_CONDICION = 1          # fijo: no se cambia durante el experimento
# Orden alternado: en X05 y X11 va primero «localizador»; en X08 y X12, «evidencia». X07 es el control adicional
# (solo «localizador», con la sugerencia 17 de v1.3.3; su «evidencia» es la corrida real de v1.3.4).
TANDAS = {"1": [("X05", "localizador"), ("X05", "evidencia"), ("X08", "evidencia"), ("X08", "localizador")],
          "2": [("X11", "localizador"), ("X11", "evidencia"), ("X12", "evidencia"), ("X12", "localizador")],
          "control": [("X07", "localizador")]}
BANCO = Path(__file__).resolve().parent / "banco_desarrollo"


class PlanInvalido(RuntimeError):
    """El plan no se puede ejecutar tal como se preregistró: no se llama al modelo."""


def normalizar(texto: str) -> str:
    """Quita del prompt SOLO lo que corresponde a la sugerencia; el resto tiene que quedar idéntico."""
    t = re.sub(r"(?m)^>>", "  ", texto)
    t = t.replace("  ← sugerida por el coordinador", "").replace(" y la marca «>>»", "")
    t = t.replace("Instrucciones candidatas, sin orden de preferencia (", "Instrucciones candidatas (")
    t = re.sub(r"El coordinador sugiere investigar la línea \d+, pero es solo una sugerencia, TODAVÍA NO COMPROBADA: ",
               "", t)
    return t.replace("El coordinador NO sugiere ninguna línea: no tiene evidencia suficiente para preferir una "
                     "candidata (el orden de la lista no indica preferencia). ", "")


def _texto(ps) -> str:
    return "".join(t for _, t in ps)


def comparar_prompts(problema: dict, obs: dict) -> dict:
    """Los dos prompts de la primera llamada y exactamente en qué difieren."""
    ps = {c: C.partes_iniciales(problema, obs, c) for c in C.POLITICAS}
    a, b = _texto(ps["localizador"]).splitlines(), _texto(ps["evidencia"]).splitlines()
    dif = [x for x in difflib.unified_diff(a, b, "localizador", "evidencia", lineterm="", n=0)
           if not x.startswith(("---", "+++", "@@"))]
    pasos = {c: C.primer_paso(obs, c) for c in C.POLITICAS}
    return {"sugerida": {c: pasos[c]["foco"] for c in C.POLITICAS},
            "solo_difieren_en_la_sugerencia": normalizar(_texto(ps["localizador"])) == normalizar(_texto(ps["evidencia"])),
            "misma_evidencia": [p for p in ps["localizador"] if p[0] == "evidencia"]
                               == [p for p in ps["evidencia"] if p[0] == "evidencia"],
            "diferencias": dif, "partes": ps}


def validar_plan(plan, observaciones: dict, comparaciones: dict) -> None:
    for i, (did, cond) in enumerate(plan):
        obs, cmp_ = observaciones[did], comparaciones[did]
        if obs.get("estado") != "investigable":
            raise PlanInvalido(f"{did}: estado «{obs.get('estado')}», no investigable")
        if cmp_["sugerida"]["evidencia"] == cmp_["sugerida"]["localizador"]:
            raise PlanInvalido(f"{did}: las dos condiciones sugieren lo mismo ({cmp_['sugerida']}): la pareja no "
                               "compara nada")
        if not (cmp_["solo_difieren_en_la_sugerencia"] and cmp_["misma_evidencia"]):
            raise PlanInvalido(f"{did}: los prompts difieren en algo más que la sugerencia: {cmp_['diferencias']}")
    if len(set(plan)) != len(plan):
        raise PlanInvalido("el plan repite una pareja defecto/condición")


def clasificar(j: dict, cond: str) -> dict:
    """Una fila por llamada: qué diagnosticó, si era correcto, y qué hicieron el coordinador y el juez."""
    it = (j.get("intentos") or [None])[0]
    obj, causa_entre = set(j.get("objetivo") or []), j.get("causa_entre_candidatas")
    if it is None:
        cat = "sin_llamada"
    elif it["rechazo"] == "ambigua":
        cat = "ambiguo"
    elif it["interpretacion"] == "ninguna":
        cat = "abstencion"
    elif it["diagnostico"] is None:
        cat = "sin_diagnostico"
    else:
        cat = "correcto" if it["diagnostico"] in obj else "incorrecto"
    return {"id": j["id"], "condicion": cond, "objetivo": sorted(obj), "causa_entre_candidatas": causa_entre,
            "sugerida": it and it["sugerida"], "diagnostico": it and it["diagnostico"], "categoria": cat,
            # Con la causa fuera de las candidatas, lo correcto es abstenerse (DIAGNOSTICO: NINGUNA).
            "respuesta_correcta": cat == "correcto" or (cat == "abstencion" and not causa_entre),
            "anclado_a_la_sugerencia": bool(it and it["sugerida"] is not None and it["diagnostico"] == it["sugerida"]),
            "rechazo": it and it["rechazo"], "aceptada_por_el_coordinador": bool(j.get("aceptada_por_el_coordinador")),
            "resultado_juez": j.get("resultado"),
            "confirmacion_falsa": j.get("resultado") == "CONFIRMACIÓN FALSA", "fugas": len((it or {}).get("fugas", []))}


def resumir(filas: list[dict]) -> dict:
    out = {}
    for c in C.POLITICAS:
        f = [x for x in filas if x["condicion"] == c]
        out[c] = {"llamadas": sum(1 for x in f if x["categoria"] != "sin_llamada"),
                  "categorias": dict(Counter(x["categoria"] for x in f)),
                  "respuestas_correctas": sum(x["respuesta_correcta"] for x in f),
                  "ancladas_a_la_sugerencia": sum(x["anclado_a_la_sugerencia"] for x in f),
                  "aceptadas_por_el_coordinador": sum(x["aceptada_por_el_coordinador"] for x in f),
                  "confirmaciones_verdaderas": sum(x["resultado_juez"] == "confirmación verdadera" for x in f),
                  "confirmaciones_falsas": sum(x["confirmacion_falsa"] for x in f),
                  "fugas": sum(x["fugas"] for x in f)}
    return out


def correr_tanda(cfg, ent, *, tanda: str, salida: Path, crear_modelo=None, guardia=None, defectos=None,
                 datos_juez: dict = C.DATOS_JUEZ, datos_variados: dict = C.DATOS_VARIADOS, log=print) -> dict:
    """Una tanda del plan. Sin modelo: solo observa y compara los prompts (no llama a nadie ni juzga).
    La carpeta es de UNA tanda; volver a correrla reanuda sin repetir llamadas."""
    if tanda not in TANDAS:
        raise PlanInvalido(f"tanda desconocida: {tanda} (hay {sorted(TANDAS)})")
    if E0.huella_reglas() != C.HUELLA_REGLAS:
        raise RuntimeError(f"las reglas de E0 cambiaron ({E0.huella_reglas()} ≠ {C.HUELLA_REGLAS})")
    plan = TANDAS[tanda]
    salida = Path(salida)
    salida.mkdir(parents=True, exist_ok=True, mode=0o700)
    vpath = salida / "version.json"
    yo = {"version": VERSION, "ciclo": C.VERSION, "tanda": tanda, "plan": plan,
          "llamadas_por_condicion": LLAMADAS_POR_CONDICION}
    if vpath.exists():
        previa = json.loads(vpath.read_text())
        if previa != json.loads(json.dumps(yo)):
            raise PlanInvalido(f"la carpeta es de otro plan o versión ({previa}): no se reanuda; usar otra carpeta")
    vpath.write_text(json.dumps(yo, ensure_ascii=False))
    if defectos is None:
        from . import defectos as DF
        defectos, _ = DF.cargar(BANCO)
    por_id = {d["id"]: d for d in defectos}
    faltan = sorted({did for did, _ in plan} - set(por_id))
    if faltan:
        raise PlanInvalido(f"faltan defectos en el banco: {faltan}")

    bordes = C.agregar_bordes(ent)
    ent_variado, datos_variados_info = C.crear_datos_variados(ent, datos_variados)
    co = C.Coordinador(ent, ent_variado)
    try:
        ids = list(dict.fromkeys(did for did, _ in plan))
        problemas = {did: {"id": did, "codigo": por_id[did]["codigo"]} for did in ids}   # solo id y código
        observaciones = {did: C.observar(co, problemas[did]) for did in ids}
        comparaciones = {did: comparar_prompts(problemas[did], observaciones[did]) for did in ids}
        validar_plan(plan, observaciones, comparaciones)
        parejas = {did: {"semilla": C.semilla(did, 0), "sugerida": c["sugerida"],
                         "solo_difieren_en_la_sugerencia": c["solo_difieren_en_la_sugerencia"],
                         "misma_evidencia": c["misma_evidencia"], "diferencias": c["diferencias"],
                         "orden": [cond for d2, cond in plan if d2 == did]} for did, c in comparaciones.items()}
        (salida / "parejas.json").write_text(json.dumps(parejas, ensure_ascii=False, indent=1))
        for did, p in parejas.items():
            log(f"  {did}: semilla {p['semilla']} · sugerida {p['sugerida']} · orden {p['orden']} · "
                f"{len(p['diferencias'])} líneas distintas, solo de la sugerencia")
        res = {"version": VERSION, "ciclo": C.VERSION, "huella_reglas": E0.huella_reglas(), "tanda": tanda,
               "plan": plan, "llamadas_por_condicion": LLAMADAS_POR_CONDICION, "bordes": bordes, "parejas": parejas}
        if crear_modelo is None:
            res.update({"corrida": "solo_prompts", "filas": []})
            (salida / "comparacion.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
            return res

        from . import v2 as V2
        from .. import protocolo as PR
        modelo = V2.ModeloV2(crear_modelo(), 10 ** 6, 10 ** 9, guardia or V2.Guardia(minimo_mb=0))
        modelo.system = PR.SYSTEM.replace("{resultado_kb}", str(cfg.instrumentos_resultado_kb))
        hechas = {x["clave"]: x for x in C._jsonl(salida / "llamadas.jsonl")}
        terminados = {f"{x['condicion']}|{x['id']}": x for x in C._jsonl(salida / "resultados.jsonl")}
        estado = {"nuevas": 0, "corrida": "completa", "interrupciones": []}
        # El límite es el plan: una llamada por pareja defecto/condición, nunca más.
        pedir = C.pedidor(modelo, salida, estado, hechas, len(plan) - len(terminados))
        resultados = dict(terminados)
        try:
            for did, cond in plan:
                if f"{cond}|{did}" in terminados:
                    continue
                log(f"  {cond} · {did}")
                r = C.investigar(co, problemas[did], observaciones[did], pedir, hechas,
                                 llamadas=LLAMADAS_POR_CONDICION, politica=cond, etiqueta=f"{cond}|", log=log)
                if r["estado"] == "incompleto":
                    estado["corrida"] = "limite"
                    break
                r = {**r, "condicion": cond}
                resultados[f"{cond}|{did}"] = r
                C._agregar(salida / "resultados.jsonl", {k: v for k, v in r.items() if k != "intentos"} |
                           {"intentos": [{k: v for k, v in it.items() if k != "partes"} for it in r["intentos"]]})
        except C.Detener as ex:
            estado["corrida"] = f"detenido_por_entorno: {ex}"

        # Cada prompt enviado tiene que ser el que se comparó antes de llamar.
        for clave, ll in hechas.items():
            cond, did = clave.split("|")[:2]
            ll_ok = [tuple(p) for p in ll["partes"]] == [tuple(p) for p in comparaciones[did]["partes"][cond]]
            parejas[did].setdefault("prompt_enviado_igual_al_comparado", {})[cond] = ll_ok
        (salida / "parejas.json").write_text(json.dumps(parejas, ensure_ascii=False, indent=1))

        # El juez entra recién ahora, con sus datos independientes.
        ent_juez, datos = C.crear_datos_juez(ent, datos_juez)
        juez = C.Juez(ent, ent_juez)
        juez.reservado = {"base del juez": ent_juez.info["base"], "lecturas del juez": datos["lecturas"],
                          "válidas del juez": datos["validas"], "inválidas del juez": datos["invalidas"]}
        filas, juicio = [], []
        try:
            for did, cond in plan:
                r = resultados.get(f"{cond}|{did}")
                if r is None:
                    continue
                for it in r["intentos"]:
                    it.setdefault("partes", (hechas.get(it["clave"]) or {}).get("partes", []))
                j = C.juzgar(juez, por_id[did], observaciones[did], r)
                juicio.append({**j, "condicion": cond})
                filas.append({**clasificar(j, cond), "semilla": parejas[did]["semilla"],
                              **{k: (hechas.get((j.get("intentos") or [{}])[0].get("clave")) or {}).get(k)
                                 for k in ("tokens_generados", "reloj_ms")}})
        finally:
            juez.liberar()
            ent_juez.cerrar()
        res.update({"corrida": estado["corrida"], "llamadas_nuevas": estado["nuevas"], "llamadas_totales": len(hechas),
                    "interrupciones": estado["interrupciones"],
                    "datos_independientes_del_juez": {**datos_juez, **datos},
                    "datos_variados_del_coordinador": {**datos_variados, **datos_variados_info},
                    "filas": filas, "resumen": resumir(filas), "juicio": juicio})
        (salida / "comparacion.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
        (salida / "resumen.md").write_text(markdown([res]))
        return res
    finally:
        co.liberar()
        ent_variado.cerrar()


def markdown(tandas: list[dict]) -> str:
    filas = [f for t in tandas for f in t.get("filas", [])]
    out = [f"# {VERSION}", "",
           "Una llamada por defecto y condición, misma semilla en cada pareja, orden alternado. La métrica principal "
           "son las confirmaciones falsas. Con tan pocas parejas, el resultado es DESCRIPTIVO.", "",
           "| tanda | defecto | condición | semilla | objetivo | sugerida | diagnóstico | categoría | correcta | "
           "anclada | aceptada | juez |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for t in tandas:
        for f in t.get("filas", []):
            out.append(f"| {t['tanda']} | {f['id']} | {f['condicion']} | {f['semilla']} | {f['objetivo']} | "
                       f"{f['sugerida'] if f['sugerida'] is not None else '—'} | {f['diagnostico'] or '—'} | "
                       f"{f['categoria']} | {'sí' if f['respuesta_correcta'] else 'no'} | "
                       f"{'sí' if f['anclado_a_la_sugerencia'] else 'no'} | "
                       f"{'sí' if f['aceptada_por_el_coordinador'] else 'no'} | {f['resultado_juez'] or '—'} |")
    out += ["", "## Por condición", ""]
    for c, r in resumir(filas).items():
        out.append(f"- **{c}**: {r['llamadas']} llamadas · {r['categorias']} · respuestas correctas "
                   f"{r['respuestas_correctas']} · ancladas {r['ancladas_a_la_sugerencia']} · aceptadas "
                   f"{r['aceptadas_por_el_coordinador']} · verdaderas {r['confirmaciones_verdaderas']} · "
                   f"**falsas {r['confirmaciones_falsas']}** · fugas {r['fugas']}")
    out += ["", "## Diferencias entre los prompts de cada pareja (antes de llamar)", ""]
    for t in tandas:
        for did, p in t["parejas"].items():
            out.append(f"- {did} (tanda {t['tanda']}): sugerida {p['sugerida']}, orden {p['orden']}, solo difieren "
                       f"en la sugerencia: {p['solo_difieren_en_la_sugerencia']}, prompt enviado = comparado: "
                       f"{p.get('prompt_enviado_igual_al_comparado', '—')}")
            out += [f"  - `{x[:150]}`" for x in p["diferencias"]]
    return "\n".join(out) + "\n"


def resumen_general(carpeta: Path) -> str | None:
    """Junta las tandas terminadas de un experimento (solo lectura)."""
    tandas = [json.loads(p.read_text()) for p in sorted(Path(carpeta).glob("tanda-*/comparacion.json"))]
    tandas = [t for t in tandas if t.get("corrida") != "solo_prompts"]
    return markdown(tandas) if tandas else None
