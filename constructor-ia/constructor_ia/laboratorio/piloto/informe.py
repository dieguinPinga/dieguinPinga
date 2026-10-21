"""Informe del piloto. Por preregistro, empieza con «Contra la hipótesis»."""
from __future__ import annotations

import json
from pathlib import Path


SENALES = {"R1": "B* ≤ C en todos los defectos: en el piloto, el tamaño del cambio no ayudó",
           "R2": "B < B* en todos los defectos: la localización automática es el cuello de botella",
           "R3": "D1 (silencioso) no fue detectado por ninguna verificación: desafío 1 sin resolver",
           "R4": "detección del sistema < 50 % de los mutantes: las invariantes genéricas no alcanzan",
           "R5": "top 3 del localizador < 50 % de los mutantes detectados: el localizador estático no alcanza"}


def markdown(res: dict | None, mut: dict | None) -> str:
    if res and res.get("ensayo"):
        L = ["# ENSAYO TÉCNICO — EXCLUIDO DEL ANÁLISIS PREREGISTRADO", "",
             "Solo comprueba que Qwen real, el sandbox y el MariaDB privado funcionan juntos (D2, 1 repetición, "
             "presupuesto reducido). Sus resultados no se leen como evidencia a favor ni en contra.", ""]
    else:
        L = []
    L += ["# Piloto de microtareas — resultados", "",
         "Preregistro: `PILOTO.md` (commit anterior al código). Los episodios con fugas se excluyen de la lectura.", ""]
    contra = list((res or {}).get("lectura", {}).get("contra_la_hipotesis", []))
    senales = dict((res or {}).get("lectura", {}).get("senales", {}))
    if mut:
        senales["R4"] = mut["resumen"]["R4_activada"]
        senales["R5"] = mut["resumen"]["R5_activada"]
    contra += [f"Señal {k} activada: {SENALES[k]}" for k, v in sorted(senales.items()) if v]
    L += ["## Contra la hipótesis", ""]
    L += [f"- {c}" for c in contra] if contra else [
        "- (ninguno). Con 2 defectos esto NO confirma la hipótesis: solo dice que el piloto no la refutó."]
    L.append("")
    if mut:
        r = mut["resumen"]
        L += ["## Estudio de mutantes (sin modelo): desafíos 1 y 2 del coordinador solo", "",
              f"- Mutantes: {r['mutantes']} ({r['equivalentes']} equivalentes, que no cuentan); no equivalentes: "
              f"{r['no_equivalentes']}.",
              f"- Detectados por las verificaciones del sistema: {r['detectados_por_el_sistema']} "
              f"({r['deteccion_sistema']:.0%}); por verificación: {r['verificaciones_que_detectan']}.",
              f"- Localización con la detección del sistema: top 1 {r['top1_con_deteccion_sistema']:.0%}, "
              f"top 3 {r['top3_con_deteccion_sistema']:.0%}.",
              f"- Localización con detección oráculo (cota del localizador aislado): top 1 "
              f"{r['top1_con_deteccion_oraculo']:.0%}, top 3 {r['top3_con_deteccion_oraculo']:.0%}.",
              f"- Por qué falla la localización: {r.get('fallas_de_localizacion')}.", "",
              "| Tipo de mutación | No equivalentes | Detectados por el sistema |", "|---|---|---|"]
        L += [f"| {t} | {v['no_equivalentes']} | {v['detectados']} |" for t, v in r["por_tipo"].items()]
        L.append("")
    if res:
        lec = res["lectura"]
        integ = lec["integridad"]
        L += ["## Integridad del circuito", "",
              f"- Fugas del oráculo: {integ['fugas']}; episodios inválidos: {integ['episodios_invalidos']}; "
              f"presupuesto excedido: {integ['presupuesto_excedido']}; fallas atribuidas al coordinador: "
              f"{integ['atribucion_coordinador']}.",
              f"- Apto para el experimento completo: {'sí' if integ['apto_para_experimento_completo'] else 'NO'}.",
              "", "## Desafío 1 — detección compartida", ""]
        for k, d in sorted(res["detecciones"].items()):
            sis = [n for n, c in d["sistema"].items() if not c["ok"]]
            ver = ", ".join(f"{v['id']}: " + ("inválido" if not v["valido"] else
                                              ("detecta " + str(v["campos"]) if v["detecta"] else "no detecta"))
                            for v in d["verificadores"]) or "ninguno"
            L.append(f"- {k}: sistema {'detecta (' + ', '.join(sis) + ')' if sis else 'no detecta'}; "
                     f"verificadores del modelo: {ver}.")
        lp = lec["localizacion_primer_paso"]
        L += ["", f"## Desafío 2 — localización automática en los episodios (primer paso, B y D): "
                  f"{lp['aciertos']}/{lp['total']}", "",
              "## Desafío 3 — corrección (juez: parámetros visibles y de reserva)", "",
              "| Defecto | Rep. | Brazo | Juez | Fin | Llamadas | Segundos de modelo |", "|---|---|---|---|---|---|---|"]
        for e in res["episodios"]:
            j = e["juez"]
            estado = "corregido" if j["corregido"] else ("solo visibles" if j["solo_visible"] else "no")
            if j["regresiones"]:
                estado += f" (regresión: {', '.join(j['regresiones'])})"
            L.append(f"| {e['defecto']} | {e['repeticion']} | {e['brazo']} | {estado}{'' if e['valido'] else ' INVÁLIDO'}"
                     f" | {e['motivo_fin']} | {len(e['llamadas'])} | {e['segundos_modelo']} |")
        L += ["", "## Comparaciones preregistradas", "", "| Comparación | Defecto | Corregidos | Llamadas | Categoría |",
              "|---|---|---|---|---|"]
        L += [f"| {c['comparacion']} | {c['defecto']} | {c['corregidos'][0]} vs {c['corregidos'][1]} | "
              f"{c['llamadas'][0]} vs {c['llamadas'][1]} | {c['categoria']} |" for c in lec["comparaciones"]]
        L.append("")
    L += ["_Piloto: 2 defectos. Sirve para la integridad del circuito y la dirección de cada comparación, no para "
          "conclusiones estadísticas._", ""]
    return "\n".join(L)


def escribir(salida: Path, res: dict | None, mut: dict | None) -> Path:
    salida = Path(salida)
    if res is not None:
        (salida / "piloto.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    if mut is not None:
        (salida / "mutantes.json").write_text(json.dumps(mut, ensure_ascii=False, indent=1))
    p = salida / "resumen.md"
    p.write_text(markdown(res, mut))
    return p
