"""Experimento A1: compara las trayectorias de N controles (misma semilla, mismas condiciones). SOLO LECTURA.

  <venv>/bin/python -I comparar_a1.py --experimento informes/a1-p1-s11-<etiqueta> [--corridas DIR]

Por corrida usa diagnostico/analizar_par.py (acciones, tokens, latencias, detector stuck reproducido) más el
registro crudo del SDK en estado/llm/ (cada pedido y su respuesta textual). Por par de corridas informa:
  - la primera ACCIÓN distinta;
  - la primera LLAMADA cuya respuesta difiere y si en esa llamada el pedido era idéntico (entonces la diferencia
    es del modelo/Ollama, no arrastrada), con la posición del primer carácter distinto y el texto alrededor;
y para el experimento, si las condiciones iniciales fueron equivalentes (estado_ollama/rN-antes.json) y si
alguna corrida quedó contaminada (otro modelo apareció durante la corrida).
Veredicto: REPRODUCIBLE · NO REPRODUCIBLE (con la llamada de la primera divergencia) · NO CONCLUYENTE.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import itertools
import json
import re
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent / "diagnostico"))
import analizar_par as A  # noqa: E402


# Partes de las observaciones que cambian de una corrida a otra sin que cambie nada de lo que hizo el agente
# (las agrega el entorno, no el modelo). Se reemplazan por un marcador para comparar pedidos «salvo ruido».
RUIDO = [re.compile(r"Ran (\d+) tests? in \d+\.\d+s"), re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(\.\d+)?"),
         re.compile(r"\b\d+\.\d+ ?(s|seconds|ms)\b")]


def sin_ruido(t: str) -> str:
    for r in RUIDO:
        t = r.sub("«ruido»", t)
    return t


def sha(x) -> str:
    return hashlib.sha256(x.encode() if isinstance(x, str) else json.dumps(x, sort_keys=True).encode()).hexdigest()[:16]


def valor(v):
    """El SDK guarda algunos campos como repr de Python: se intenta leerlos."""
    if isinstance(v, str):
        for f in (json.loads, ast.literal_eval):
            try:
                return f(v)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                pass
    return v


def llamadas_crudas(resultado: Path) -> list[dict]:
    d = resultado / "estado" / "llm"
    if not d.is_dir():
        return []
    filas = []
    for p in d.glob("*.json"):
        try:
            x = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        r = valor(x.get("raw_response")) or valor(x.get("response")) or {}
        try:
            texto = r["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            texto = str(r)[:4000]
        uso = valor(x.get("usage_summary")) or {}
        filas.append({"archivo": p.name, "momento": float(x.get("timestamp") or 0),
                      "pedido_sha": sha(str(x.get("raw_messages"))), "pedido_sha_sin_ruido": sha(sin_ruido(str(x.get("raw_messages")))),
                      "respuesta": texto, "respuesta_sha": sha(texto),
                      "prompt_tokens": uso.get("prompt_tokens") if isinstance(uso, dict) else None,
                      "completion_tokens": uso.get("completion_tokens") if isinstance(uso, dict) else None,
                      "latencia": round(float(x.get("latency_sec") or 0), 2)})
    return sorted(filas, key=lambda f: f["momento"])


def primera_diferencia(a: str, b: str) -> int | None:
    for k, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return k
    return None if len(a) == len(b) else min(len(a), len(b))


def comparar_par(x: dict, y: dict) -> dict:
    ax, ay = x["analisis"]["acciones"], y["analisis"]["acciones"]
    acc = next((k for k, (p, q) in enumerate(zip(ax, ay)) if p["firma"] != q["firma"]), None)
    if acc is None and len(ax) != len(ay):
        acc = min(len(ax), len(ay))
    lx, ly = x["crudas"], y["crudas"]
    comunes = list(zip(lx, ly))
    # por llamada: ¿mismo pedido (exacto / salvo ruido del entorno)?, ¿misma respuesta?
    llamadas = [{"pedido": "identico" if p["pedido_sha"] == q["pedido_sha"] else
                 "solo_ruido_del_entorno" if p["pedido_sha_sin_ruido"] == q["pedido_sha_sin_ruido"] else "distinto",
                 "respuesta_igual": p["respuesta_sha"] == q["respuesta_sha"]} for p, q in comunes]
    lla = next((k for k, c in enumerate(llamadas) if not c["respuesta_igual"]), None)
    if lla is None and len(lx) != len(ly):
        lla = min(len(lx), len(ly))
    # evidencia de no determinismo del modelo: misma entrada exacta, otra salida
    del_modelo = [k for k, c in enumerate(llamadas) if c["pedido"] == "identico" and not c["respuesta_igual"]]
    detalle = None
    if lla is not None and lla < len(comunes):
        p, q = comunes[lla]
        c = primera_diferencia(p["respuesta"], q["respuesta"])
        ventana = lambda t: t[max(0, (c or 0) - 60):(c or 0) + 120]  # noqa: E731
        detalle = {"llamada": lla, "pedido": llamadas[lla]["pedido"], "pedido_identico": llamadas[lla]["pedido"] == "identico",
                   "caracter": c, "texto": {x["nombre"]: ventana(p["respuesta"]), y["nombre"]: ventana(q["respuesta"])},
                   "tokens": {x["nombre"]: [p["prompt_tokens"], p["completion_tokens"]], y["nombre"]: [q["prompt_tokens"], q["completion_tokens"]]},
                   "latencia": {x["nombre"]: p["latencia"], y["nombre"]: q["latencia"]}}
    return {"par": [x["nombre"], y["nombre"]], "primera_accion_distinta": acc, "acciones_identicas_antes": acc if acc is not None else len(ax),
            "primera_llamada_distinta": lla, "detalle": detalle,
            "llamadas_comparadas": len(comunes),
            "pedidos": {k: sum(1 for c in llamadas if c["pedido"] == k) for k in ("identico", "solo_ruido_del_entorno", "distinto")},
            "primer_pedido_con_ruido": next((k for k, c in enumerate(llamadas) if c["pedido"] == "solo_ruido_del_entorno"), None),
            "no_determinismo_del_modelo_en_llamadas": del_modelo,
            "identicas": acc is None and lla is None and x["analisis"]["estado_final_sdk"] == y["analisis"]["estado_final_sdk"]}


def equivalencia(estados: dict[str, dict]) -> dict:
    """¿Empezaron las corridas en condiciones equivalentes? (cada rN-antes.json es la salida de `preparar`)."""
    problemas = []
    for n, e in estados.items():
        if not e:
            problemas.append(f"{n}: falta el estado inicial")
            continue
        if e.get("resultado") != "en_frio":
            problemas.append(f"{n}: no empezó en frío ({e.get('resultado')})")
        if e.get("inicial", {}).get("cargados"):
            problemas.append(f"{n}: había modelos cargados al empezar")
    versiones = {(e or {}).get("inicial", {}).get("version") for e in estados.values()}
    if len(versiones) > 1:
        problemas.append(f"versiones de Ollama distintas: {sorted(map(str, versiones))}")
    entornos = {json.dumps((e or {}).get("inicial", {}).get("entorno_ollama"), sort_keys=True) for e in estados.values()}
    if len(entornos) > 1:
        problemas.append("la configuración de Ollama cambió entre corridas")
    memorias = [int(g["memoria_usada_mib"]) for e in estados.values() for g in ((e or {}).get("inicial", {}).get("gpu") or [])[:1]
                if str(g.get("memoria_usada_mib", "")).isdigit()]
    if memorias and max(memorias) - min(memorias) > 300:
        problemas.append(f"la memoria de GPU ocupada al empezar varió más de 300 MiB: {memorias}")
    return {"equivalentes": not problemas, "problemas": problemas, "memoria_gpu_inicial_mib": memorias}


def analizar(experimento: Path, corridas: Path) -> dict:
    pr = json.loads((experimento / "experimento.json").read_text())
    filas = []
    for n in pr["corridas"]:
        c = corridas / n
        an = A.analizar_corrida(c)
        r = Path(an["resultado"]) if an["resultado"] else None
        filas.append({"nombre": n, "analisis": an, "crudas": llamadas_crudas(r) if r else [],
                      "antes": A.leer(experimento / "estado_ollama" / f"{n}-antes.json"),
                      "despues": A.leer(experimento / "estado_ollama" / f"{n}-despues.json")})
    pares = [comparar_par(x, y) for x, y in itertools.combinations(filas, 2)]
    eq = equivalencia({f["nombre"]: f["antes"] for f in filas})
    contaminadas = [f["nombre"] for f in filas if (f["despues"] or {}).get("contaminada")]
    sin_crudas = [f["nombre"] for f in filas if not f["crudas"]]
    if not eq["equivalentes"] or contaminadas or sin_crudas or len(filas) < 2:
        veredicto = "NO CONCLUYENTE"
    elif all(p["identicas"] for p in pares):
        veredicto = "REPRODUCIBLE"
    elif any(p["no_determinismo_del_modelo_en_llamadas"] for p in pares):
        veredicto = "NO REPRODUCIBLE (el modelo respondió distinto a pedidos idénticos)"
    elif all((p["detalle"] or {}).get("pedido") == "solo_ruido_del_entorno" for p in pares if not p["identicas"]):
        veredicto = "NO REPRODUCIBLE (divergencia tras ruido del entorno en las observaciones)"
    else:
        veredicto = "NO REPRODUCIBLE"
    return {"experimento": experimento.name, "veredicto": veredicto, "equivalencia": eq, "contaminadas": contaminadas,
            "sin_registro_crudo": sin_crudas, "pares": pares,
            "corridas": [{"nombre": f["nombre"], "estado_final_sdk": f["analisis"]["estado_final_sdk"],
                          "estado_ejecucion": f["analisis"]["estado_ejecucion"], "acciones": len(f["analisis"]["acciones"]),
                          "llamadas": len(f["crudas"]), "stuck": f["analisis"]["stuck"],
                          "entradas": f["analisis"]["entradas"],
                          "primera_llamada": (f["crudas"] or [{}])[0] | {"respuesta": None},
                          "primeras_acciones": [{k: a[k] for k in ("herramienta", "argumentos", "pensamiento")} for a in f["analisis"]["acciones"][:3]]}
                         for f in filas]}


def markdown(r: dict) -> str:
    ls = [f"# Experimento A1 `{r['experimento']}`: **{r['veredicto']}**", ""]
    eq = r["equivalencia"]
    ls += ["## Condiciones iniciales", "", "equivalentes: " + ("sí" if eq["equivalentes"] else "**NO** — " + "; ".join(eq["problemas"])),
           f"memoria de GPU ocupada al empezar (MiB): {eq['memoria_gpu_inicial_mib'] or 'no disponible (sin nvidia-smi u Ollama en otra máquina)'}",
           f"corridas contaminadas (otro modelo apareció): {r['contaminadas'] or 'ninguna'}",
           f"sin registro crudo de respuestas: {r['sin_registro_crudo'] or 'ninguna'}", ""]
    ls += ["## Corridas", "", "| corrida | estado SDK | ejecución | acciones | llamadas | 1ª llamada: prompt_tokens / completion / latencia s | stuck reproducido |",
           "|---|---|---|---|---|---|---|"]
    for c in r["corridas"]:
        p = c["primera_llamada"]
        s = c["stuck"]
        st = f"evento {s['stuck_tras_evento']}" if s.get("stuck_tras_evento") is not None else ("no" if s.get("disponible") else "sin SDK")
        ls.append(f"| {c['nombre']} | {c['estado_final_sdk']} | {c['estado_ejecucion']} | {c['acciones']} | {c['llamadas']} | "
                  f"{p.get('prompt_tokens')} / {p.get('completion_tokens')} / {p.get('latencia')} | {st} |")
    ls += ["", "## Pares", "", "| par | idénticas | acciones iguales antes de divergir | 1ª respuesta distinta | pedido en esa llamada | 1er carácter distinto | pedidos idénticos / solo ruido / distintos | respuestas distintas con pedido idéntico |",
           "|---|---|---|---|---|---|---|---|"]
    for p in r["pares"]:
        d = p["detalle"] or {}
        ls.append(f"| {' / '.join(p['par'])} | {'sí' if p['identicas'] else 'no'} | {p['acciones_identicas_antes']} | "
                  f"{p['primera_llamada_distinta']} | {d.get('pedido', '—')} | {d.get('caracter', '—')} | "
                  f"{p['pedidos']['identico']} / {p['pedidos']['solo_ruido_del_entorno']} / {p['pedidos']['distinto']} | "
                  f"{p['no_determinismo_del_modelo_en_llamadas'] or 'ninguna'} |")
    for p in r["pares"]:
        d = p["detalle"]
        if d:
            ls += ["", f"### {' / '.join(p['par'])}: llamada {d['llamada']}", ""]
            for n, t in d["texto"].items():
                ls.append(f"- **{n}** (tokens {d['tokens'][n]}, {d['latencia'][n]} s): `{' '.join(t.split())[:200]}`")
    ls += ["", "Lectura: una respuesta distinta con el **pedido idéntico** la produjo el modelo (Ollama) con la misma entrada. "
               "«solo ruido» quiere decir que los pedidos difieren únicamente en datos que agrega el entorno (por ejemplo "
               "`Ran 3 tests in 0.001s` frente a `0.002s`); si la respuesta cambia ahí, no se puede separar el efecto del "
               "modelo del de ese ruido. Si el pedido ya era distinto en serio, la diferencia viene arrastrada.", ""]
    return "\n".join(ls)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--experimento", type=Path, required=True)
    ap.add_argument("--corridas", type=Path, default=AQUI.parent / "corridas")
    a = ap.parse_args(argv)
    r = analizar(a.experimento, a.corridas)
    (a.experimento / "comparacion_a1.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))
    (a.experimento / "comparacion_a1.md").write_text(markdown(r))
    print(markdown(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
