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
    protocolo = (res or {}).get("protocolo", "v1")
    if res and res.get("ensayo"):
        L = ["# ENSAYO TÉCNICO — EXCLUIDO DEL ANÁLISIS PREREGISTRADO", "",
             "Solo comprueba que Qwen real, el sandbox y el MariaDB privado funcionan juntos (D2, 1 repetición, "
             "presupuesto reducido). Sus resultados no se leen como evidencia a favor ni en contra.", ""]
    else:
        L = []
    L += [f"# Piloto de microtareas — resultados (protocolo {protocolo})", "",
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
              f"- Estado de la corrida: {integ.get('estado', 'completo')}; interrupciones del entorno: "
              f"{integ.get('interrupciones_entorno', 0)}.",
              f"- Apto para el experimento completo: {'sí' if integ['apto_para_experimento_completo'] else 'NO'}.",
              f"- Historial recortado por contexto (menos información que la política común): "
              f"{', '.join(integ['historial_recortado']) if integ.get('historial_recortado') else 'ninguno'}.",
              "", "## Autoría de las fallas (modelo, contrato, coordinador, límite, entorno)", "",
              *[f"- {k}: {v}" for k, v in sorted(lec.get("autoria", {}).items())],
              "", "## Desafío 1 — detección compartida", ""]
        for k, d in sorted(res["detecciones"].items()):
            sis = [n for n, c in d["sistema"].items() if not c["ok"]]
            ver = ", ".join(f"{v['id']}: " + ("inválido" if not v["valido"] else
                                              ("detecta " + str(v["campos"]) if v["detecta"] else "no detecta"))
                            for v in d["verificadores"]) or "ninguno"
            L.append(f"- {k}: sistema {'detecta (' + ', '.join(sis) + ')' if sis else 'no detecta'}; "
                     f"verificadores del modelo: {ver}.")
        fa = lec.get("falsas_alarmas") or {}
        if fa:
            L += ["", "## Falsas alarmas (verificadores del modelo corridos sobre la REFERENCIA, después)", ""]
            L += [f"- {k} {vid}: {'FALSA ALARMA' if i['falsa_alarma'] else ('no evaluable' if i['no_evaluable'] else 'correcto')}"
                  for k, vs in sorted(fa.items()) for vid, i in sorted(vs.items())]
        if lec.get("disputas"):
            L += ["", "## Disputas (el modelo decidió si estaba mal el instrumento o el verificador)", ""]
            L += [f"- {d['episodio']} · {d['verificador']}: decidió «{d['decision']}»; según el juez "
                  + ("era falsa alarma" if d["falsa_alarma_segun_juez"] else "el verificador tenía razón"
                     if d["falsa_alarma_segun_juez"] is False else "no evaluable")
                  + (" → decisión correcta" if d["decision_correcta"] else " → decisión incorrecta"
                     if d["decision_correcta"] is False else "") for d in lec["disputas"]]
        if res.get("interrupciones"):
            L += ["", "## Interrupciones del entorno (no cuentan como fallas del modelo)", ""]
            L += [f"- {i['defecto']} r{i['repeticion']} {i['brazo']}: {i['tipo']} — {i['detalle'][:160]}"
                  for i in res["interrupciones"]]
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


def revisar(carpeta: Path, max_respuesta: int = 400) -> str:
    """Revisión de SOLO LECTURA de una carpeta del piloto (o del ensayo): integridad, cada llamada al modelo con su
    tiempo, el origen de las partes de su prompt y el comienzo de la respuesta cruda, y la autoría de cada paso."""
    carpeta = Path(carpeta)
    L = [f"Carpeta: {carpeta}"]
    ensayo = (carpeta / "ENSAYO_TECNICO").exists()
    L.append("Tipo: " + ("ENSAYO TÉCNICO (excluido del análisis)" if ensayo else "corrida preregistrada"))
    marca = carpeta / "PROTOCOLO"
    L.append("Protocolo: " + (marca.read_text().strip() if marca.exists() else "v1"))
    L.append("MariaDB privado borrado: " + ("sí" if not (carpeta / "mariadb").exists() else "NO (quedó la carpeta)"))
    L.append("Clave desechable borrada: " + ("sí" if not (carpeta / "clave-desechable").exists() else "NO"))
    eps = [json.loads(x) for x in (carpeta / "episodios.jsonl").read_text().splitlines() if x.strip()] \
        if (carpeta / "episodios.jsonl").exists() else []
    prompts = [json.loads(x) for x in (carpeta / "prompts.jsonl").read_text().splitlines() if x.strip()] \
        if (carpeta / "prompts.jsonl").exists() else []
    L.append(f"Episodios: {len(eps)}; llamadas registradas: {len(prompts)}")
    L.append(f"Fugas: {sum(len(e['fugas']) for e in eps)}; presupuesto excedido: "
             f"{sum(1 for e in eps if not e['presupuesto_respetado'])}")
    det = carpeta / "deteccion.json"
    if det.exists():
        L.append("\n== Detección compartida")
        for k, d in json.loads(det.read_text()).items():
            for c in d.get("llamadas", []):
                L.append(f"  {k} llamada {c.get('tipo')}: {c.get('resultado')}, {c.get('tokens_generados')} tokens, "
                         f"{(c.get('reloj_ms') or 0) / 1000:.1f} s")
            for v in d["verificadores"]:
                L.append(f"  {k} {v['id']}: " + ("INVÁLIDO: " + str((v.get('error') or {}).get('error'))[:200]
                                                if not v["valido"] else
                                                f"válido, {'detecta ' + str(v['campos']) if v['detecta'] else 'no detecta'}"))
    for e in eps:
        j = e["juez"]
        L.append(f"\n== {e['defecto']} r{e['repeticion']} brazo {e['brazo']}: "
                 f"{'CORREGIDO' if j['corregido'] else 'no corregido'} · fin {e['motivo_fin']} · "
                 f"{len(e['llamadas'])} llamadas · {e['segundos_modelo']} s de modelo")
        for c in e["llamadas"]:
            mem = c.get("memoria") or {}
            L.append(f"   llamada {c.get('tipo')}: {c.get('resultado')}, {c.get('tokens_prompt')} → "
                     f"{c.get('tokens_generados')} tokens, {(c.get('reloj_ms') or 0) / 1000:.1f} s"
                     + (f", ~{c['tokens_estimados']} tokens estimados" if c.get("tokens_estimados") else "")
                     + (f", memoria disponible {mem['mem_disponible_mb']} MiB" if mem.get("mem_disponible_mb") else "")
                     + (f", error: {c['error']}" if c.get("error") else ""))
        for i, p in enumerate(e["pasos"], 1):
            if isinstance(p, dict) and "candidatas" in p:
                extra = [f"{k}: {p[k]}" for k in ("candidato", "linea", "interpretacion", "contrato",
                                                    "historial_incluido", "repite_intento") if p.get(k) is not None]
                L.append(f"   paso {i}: candidatas {p['candidatas']} → {p.get('resultado')}"
                         f" (autoría de la falla: {p.get('atribucion') or 'ninguna'}; {p.get('razon') or p.get('detalle')})"
                         + (f" [{'; '.join(extra)}]" if extra else ""))
            elif isinstance(p, dict):
                L.append(f"   intento v{p.get('version')}: {p.get('firma') or 'sin falla'} ({p.get('resultado')})")
        for d in e.get("disputas") or []:
            L.append(f"   disputa {d['verificador']}: decidió «{d['decision']}» — {d.get('respuesta', '')[:200]}")
        if e.get("motivo_invalido"):
            L.append(f"   EPISODIO INVÁLIDO: {e['motivo_invalido']}")
        for p in [x for x in prompts if (x["defecto"], x["repeticion"], x["brazo"]) ==
                  (e["defecto"], e["repeticion"], e["brazo"])]:
            origenes = sorted({o for o, _ in p["partes"]})
            resp = (p.get("respuesta") or "").strip().replace("\n", "\n      ")
            L.append(f"   prompt {p['n']} ({p.get('tipo')}), partes: {', '.join(origenes)}\n      respuesta: "
                     + (resp[:max_respuesta] + ("…" if len(resp) > max_respuesta else "") if resp else "(vacía)"))
    inter = carpeta / "interrupciones.jsonl"
    if inter.exists():
        L.append("\n== Interrupciones del entorno (no son fallas del modelo)")
        for x in inter.read_text().splitlines():
            i = json.loads(x)
            L.append(f"  {i['defecto']} r{i['repeticion']} {i['brazo']}: {i['tipo']} — {i['detalle'][:200]}"
                     f" · memoria: {i.get('memoria')}")
    if (carpeta / "resumen.md").exists():
        L += ["", "== resumen.md", (carpeta / "resumen.md").read_text()]
    return "\n".join(L)
