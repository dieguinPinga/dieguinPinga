"""Análisis forense de una Etapa 1 TERMINADA del experimento de elección (PILOTO.md, «Análisis forense»).

SOLO LECTURA: lee casos.json y ensayos.jsonl de la carpeta, no llama a ningún modelo, no ejecuta código y no escribe
en la carpeta. No cambia el veredicto preregistrado: describe patrones para formular hipótesis del próximo diseño.

Genérico (no depende de D1 ni de D2). Por cada respuesta mira:

* coherencia entre el MOTIVO y el CANDIDATO: qué candidatas nombra el motivo (por número de línea) y si la elegida
  está entre ellas;
* términos que el motivo toma SOLO de las verificaciones (no aparecen en el código ni en la salida), SOLO de la
  traza o SOLO de la salida: de dónde sacó el modelo su explicación;
* en el tratamiento, la etiqueta que la traza le puso a la elegida y a la sembrada («conserva el valor», «ya no
  tiene el valor», «constante», «no se ejecutó») y si la elegida tenía el bloque de traza más largo o el último;
* posición y letra elegidas por caso, rotación y condición.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

LETRAS = "ABC"
RE_CAND = re.compile(r"CANDIDATO:\s*([A-Ca-c]\b|\d+)")
RE_PALABRA = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ_][A-Za-zÁÉÍÓÚÜÑáéíóúüñ_0-9]{3,}")
RE_LINEA = re.compile(r"\bl[íi]neas?\s*(\d{1,4})\b|\b(\d{1,4})\b")


def _parte(ensayo: dict, origen: str, empieza: str = "") -> str:
    return "\n".join(t for o, t in ensayo.get("partes") or [] if o == origen and t.startswith(empieza))


def motivo(respuesta: str | None) -> str:
    t = respuesta or ""
    i = t.find("MOTIVO:")
    return t[i + len("MOTIVO:"):].strip() if i >= 0 else RE_CAND.sub("", t).strip()


def lineas_nombradas(texto: str, candidatas: list[int]) -> list[int]:
    """Candidatas que el texto nombra por su número de línea (en orden de aparición, sin repetir)."""
    out = []
    for m in RE_LINEA.finditer(texto):
        n = int(m.group(1) or m.group(2))
        if n in candidatas and n not in out:
            out.append(n)
    return out


def _palabras(texto: str) -> set[str]:
    return {w.lower() for w in RE_PALABRA.findall(texto)}


def bloques_traza(traza: str) -> dict[str, str]:
    """Texto de la traza de cada letra («A) línea 22: …» hasta la letra siguiente)."""
    out, actual = {}, None
    for ln in traza.splitlines():
        m = re.match(r"([A-C])\) línea \d+:", ln)
        if m:
            actual = m.group(1)
            out[actual] = ln
        elif actual:
            out[actual] += "\n" + ln
    return out


def etiquetas(bloque: str) -> list[str]:
    out = []
    if re.search(r"se ejecutó 0 veces", bloque):
        out.append("no se ejecutó")
    if "conserva el valor" in bloque:
        out.append("conserva")
    if "ya no tiene el valor" in bloque:
        out.append("se pierde")
    if "valor constante" in bloque:
        out.append("constante que reemplaza")
    if re.search(r"verdadera en 0 de", bloque):
        out.append("condición nunca verdadera")
    return out or ["sin etiqueta"]


def describir(e: dict, caso: dict) -> dict:
    """Rasgos forenses de UNA respuesta guardada."""
    orden = list(e["orden"])
    resp = e.get("respuesta") or ""
    mot = motivo(resp)
    m = RE_CAND.search(resp)
    letra_dicha = m.group(1).upper() if m and m.group(1).isalpha() else None
    elegida = e.get("linea_elegida")
    nombradas = lineas_nombradas(mot, orden)
    if not nombradas:
        coherencia = "sin referencia"
    elif elegida in nombradas:
        coherencia = "coherente" if len(nombradas) == 1 else "nombra varias, incluida la elegida"
    else:
        coherencia = "incoherente"
    # ¿la sembrada es una de las nombradas? (la sembrada se usa SOLO para puntuar)
    a, b = caso["sembrada"]
    sembradas = [ln for ln in orden if a <= ln <= b] + ([elegida] if e.get("correcto") else [])
    salida = _parte(e, "evidencia", "Salida actual")
    verif = _parte(e, "evidencia", "Verificaciones")
    traza = _parte(e, "traza")
    resto = "\n".join(t for o, t in e.get("partes") or [] if o == "coordinador")
    pal = _palabras(mot)
    p_salida, p_verif, p_traza = _palabras(salida), _palabras(verif), _palabras(traza)
    p_codigo = _palabras(resto + "\n" + caso.get("codigo", ""))
    out = {"clave": e["clave"], "caso": e["caso"], "rotacion": e["rotacion"], "repeticion": e["repeticion"],
           "condicion": e["condicion"], "orden": orden, "correcto": bool(e.get("correcto")),
           "ilegible": bool(e.get("ilegible")), "posicion": e.get("posicion_elegida"), "letra_dicha": letra_dicha,
           "linea_elegida": elegida, "motivo": mot, "nombradas": nombradas, "coherencia": coherencia,
           "nombra_sembrada": any(ln in nombradas for ln in sembradas),
           "terminos_solo_verificacion": sorted(pal & (p_verif - p_codigo - p_salida - p_traza)),
           "terminos_solo_traza": sorted(pal & (p_traza - p_codigo - p_salida - p_verif)),
           "terminos_solo_salida": sorted(pal & (p_salida - p_codigo - p_verif - p_traza))}
    if traza:
        bl = bloques_traza(traza)
        pos = e.get("posicion_elegida")
        letra = LETRAS[pos] if pos is not None else None
        largos = {l: len(t) for l, t in bl.items()}
        sem_letras = sorted({LETRAS[orden.index(ln)] for ln in sembradas if ln in orden})
        out.update({"etiqueta_elegida": etiquetas(bl.get(letra, "")) if letra else None,
                    "etiqueta_sembrada": [etiquetas(bl.get(l, "")) for l in sem_letras],
                    "elegida_bloque_mas_largo": letra is not None and largos.get(letra) == max(largos.values()),
                    "elegida_bloque_ultimo": letra is not None and letra == max(bl),
                    "largo_bloques": largos})
    return out


def analizar_carpeta(carpeta: Path) -> dict:
    carpeta = Path(carpeta)
    info = json.loads((carpeta / "casos.json").read_text())
    casos = {c["id"]: c for c in info["casos"]}
    ensayos = [json.loads(x) for x in (carpeta / "ensayos.jsonl").read_text().splitlines() if x.strip()]
    filas = [describir(e, casos[e["caso"]]) for e in ensayos]
    por = {(f["caso"], f["rotacion"], f["repeticion"], f["condicion"]): f for f in filas}
    discordantes = []
    for (cid, r, rep, cond), f in sorted(por.items()):
        if cond != "control":
            continue
        t = por.get((cid, r, rep, "tratamiento"))
        if t and f["correcto"] != t["correcto"]:
            discordantes.append({"caso": cid, "rotacion": r, "repeticion": rep,
                                 "efecto": "ayudó" if t["correcto"] else "perjudicó", "control": f,
                                 "tratamiento": t,
                                 "traza": next(_parte(e, "traza") for e in ensayos
                                               if e["clave"] == t["clave"])})
    matriz = {}
    for f in filas:
        matriz.setdefault(f["caso"], {}).setdefault(f"rot {f['rotacion']}", {}).setdefault(f["condicion"], []) \
            .append(f"{LETRAS[f['posicion']] if f['posicion'] is not None else '?'}={f['linea_elegida']}"
                    f"{'✓' if f['correcto'] else '✗'}")

    def resumen(cond):
        xs = [f for f in filas if f["condicion"] == cond]
        mal = [f for f in xs if not f["correcto"]]
        return {"respuestas": len(xs), "incorrectas": len(mal),
                "coherencia": dict(Counter(f["coherencia"] for f in xs)),
                "incorrectas_que_nombran_la_sembrada": sum(1 for f in mal if f["nombra_sembrada"]),
                "con_terminos_solo_de_verificacion": sum(1 for f in xs if f["terminos_solo_verificacion"]),
                "acierto_con_terminos_solo_de_verificacion": _acc([f for f in xs if f["terminos_solo_verificacion"]]),
                "acierto_sin_terminos_solo_de_verificacion": _acc([f for f in xs
                                                                  if not f["terminos_solo_verificacion"]]),
                "terminos_solo_verificacion": dict(Counter(t for f in xs for t in f["terminos_solo_verificacion"])),
                "terminos_solo_traza": dict(Counter(t for f in xs for t in f["terminos_solo_traza"]))}

    trat = [f for f in filas if f["condicion"] == "tratamiento" and f["posicion"] is not None]
    huella = hashlib.sha256()
    for e in sorted(ensayos, key=lambda x: x["clave"]):
        huella.update("\n".join(t for _, t in e.get("partes") or []).encode())
    return {"carpeta": str(carpeta), "respuestas": len(filas), "discordantes": discordantes,
            "huella_prompts": huella.hexdigest()[:16],
            "por_condicion": {c: resumen(c) for c in ("control", "tratamiento")},
            "traza": {"etiqueta_de_la_elegida": dict(Counter("+".join(f["etiqueta_elegida"]) for f in trat)),
                      "etiqueta_de_la_elegida_si_incorrecta": dict(Counter("+".join(f["etiqueta_elegida"])
                                                                           for f in trat if not f["correcto"])),
                      "elegida_bloque_mas_largo": sum(1 for f in trat if f["elegida_bloque_mas_largo"]),
                      "elegida_bloque_ultimo": sum(1 for f in trat if f["elegida_bloque_ultimo"]),
                      "respuestas": len(trat)},
            "matriz": matriz, "filas": filas}


def _acc(xs):
    return round(sum(1 for x in xs if x["correcto"]) / len(xs), 3) if xs else None


def markdown(res: dict) -> str:
    L = [f"# Análisis forense (solo lectura): {res['carpeta']}", "",
         f"{res['respuestas']} respuestas. No cambia el veredicto preregistrado; describe patrones para hipótesis.",
         f"Huella de los prompts guardados: `{res['huella_prompts']}` (sha256 de los textos, ordenados por clave).", "",
         "## Por condición", ""]
    for c, r in res["por_condicion"].items():
        L += [f"**{c}**: {r['incorrectas']} incorrectas de {r['respuestas']} · coherencia motivo/candidato "
              f"{r['coherencia']} · incorrectas cuyo motivo nombra la línea sembrada: "
              f"{r['incorrectas_que_nombran_la_sembrada']}",
              f"- motivos con términos que solo están en las verificaciones: "
              f"{r['con_terminos_solo_de_verificacion']} (acierto {r['acierto_con_terminos_solo_de_verificacion']} "
              f"contra {r['acierto_sin_terminos_solo_de_verificacion']} sin ellos) · {r['terminos_solo_verificacion']}",
              f"- términos que solo están en la traza: {r['terminos_solo_traza']}", ""]
    t = res["traza"]
    L += ["## Tratamiento: qué decía la traza de la elegida", "",
          f"- etiqueta de la elegida: {t['etiqueta_de_la_elegida']}",
          f"- etiqueta de la elegida cuando fue incorrecta: {t['etiqueta_de_la_elegida_si_incorrecta']}",
          f"- la elegida tenía el bloque de traza más largo: {t['elegida_bloque_mas_largo']} de {t['respuestas']} · "
          f"era el último bloque (justo antes de la pregunta): {t['elegida_bloque_ultimo']} de {t['respuestas']}",
          "", "## Elecciones por caso, rotación y condición (letra=línea, ✓/✗)", ""]
    for cid, rots in res["matriz"].items():
        L.append(f"- **{cid}**: " + " · ".join(
            f"{r}: control {v.get('control', [])} / tratamiento {v.get('tratamiento', [])}"
            for r, v in sorted(rots.items())))
    L += ["", f"## Pares discordantes ({len(res['discordantes'])})", ""]
    for d in res["discordantes"]:
        c, tr = d["control"], d["tratamiento"]
        L += [f"### {d['caso']} · rotación {d['rotacion']} · repetición {d['repeticion']}: la traza {d['efecto']}",
              f"Orden mostrado: " + ", ".join(f"{l}={ln}" for l, ln in zip(LETRAS, c["orden"])), "",
              f"- control → {c['letra_dicha'] or '?'} (línea {c['linea_elegida']}) {'✓' if c['correcto'] else '✗'} · "
              f"{c['coherencia']} · nombra {c['nombradas']} · solo-verificación {c['terminos_solo_verificacion']}",
              f"  > {c['motivo'][:500]}",
              f"- tratamiento → {tr['letra_dicha'] or '?'} (línea {tr['linea_elegida']}) "
              f"{'✓' if tr['correcto'] else '✗'} · {tr['coherencia']} · nombra {tr['nombradas']} · "
              f"etiqueta de la elegida {tr.get('etiqueta_elegida')} · de la sembrada {tr.get('etiqueta_sembrada')} · "
              f"solo-traza {tr['terminos_solo_traza']} · solo-verificación {tr['terminos_solo_verificacion']}",
              f"  > {tr['motivo'][:500]}", "", "<details><summary>Traza mostrada</summary>", "", "```",
              d["traza"].strip(), "```", "</details>", ""]
    return "\n".join(L) + "\n"
