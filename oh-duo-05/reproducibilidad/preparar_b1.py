"""Comprobaciones previas de B1 contra A1, SOLO LECTURA. Todas se hacen antes de tocar la GPU.

  python3 preparar_b1.py --a1 informes/a1-p1-s11-<et> --salida informes/b1-.../experimento.json \
                         --modo real|simulado --url URL --nombre b1-p1-s11-<et> --n 3 --corridas DIR

Aborta (salida 3) si cualquiera falla:
  1. A1 dio REPRODUCIBLE (comparacion_a1.json): hay un único contrafáctico;
  2. el código que afecta a las corridas es el mismo que en A1, archivo por archivo (huellas de A1);
  3. la versión de Ollama es la misma que en A1;
  4. el modelo es el mismo: digest de qwen2.5-coder:7b en /api/tags = el que A1 registró en api_ps.json
     (en modo simulado no hay digest y se informa);
  5. el coordinador, en las tres corridas de A1, tomó la misma decisión `nueva_estrategia` y el despachador la
     habría enviado («simulado», con margen): si no, B1 no mediría nada.
Escribe experimento.json de B1 con las mismas huellas, más las del despachador, y la predicción de cuándo entra
el mensaje.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

AQUI = Path(__file__).resolve().parent.parent
SOLO_ANALISIS = ("comparar_a1.py", "comparar_b1.py")
MODELO = "qwen2.5-coder:7b"


def leer(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def lineas(p: Path) -> list[dict]:
    try:
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return []


def sha(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def digest_a1(corridas: Path, nombre: str) -> str | None:
    for f in sorted((corridas / nombre).glob("ejecuciones/*/instancia/resultados/*/api_ps.json")):
        for m in (leer(f) or {}).get("models", []):
            if (m.get("name") or m.get("model")) == MODELO and m.get("digest"):
                return m["digest"]
    return None


def digest_actual(url: str) -> str | None:
    with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=10) as r:
        for m in json.loads(r.read()).get("models", []):
            if (m.get("name") or m.get("model")) == MODELO:
                return m.get("digest")
    return None


def version_actual(url: str) -> str | None:
    with urllib.request.urlopen(url.rstrip("/") + "/api/version", timeout=10) as r:
        return json.loads(r.read()).get("version")


def decisiones_a1(a1: Path, corridas_a1: list[str]) -> dict:
    """La decisión del coordinador en cada corrida de A1 (corrió sin --enviar: decidió, no envió)."""
    informes = a1.parent
    por = {}
    for n in corridas_a1:
        dec = [d for d in lineas(informes / n / "coordinador" / "P1__qwen" / "decisiones.jsonl") if d.get("tipo") == "nueva_estrategia"]
        des = lineas(informes / n / "despachos.jsonl")
        por[n] = {"decisiones": [(d.get("iteracion"), d.get("mensaje_sha256")) for d in dec],
                  "despachos": [(d.get("iteracion"), d.get("resultado")) for d in des]}
    return por


def comprobar(a1: Path, modo: str, url: str, corridas_a1: Path | None = None) -> tuple[list[str], dict]:
    problemas, info = [], {}
    ea = leer(a1 / "experimento.json") or {}
    ca = corridas_a1 or Path(ea.get("corridas_dir") or AQUI / "corridas")
    info["corridas_a1"] = str(ca)
    # 1
    v = (leer(a1 / "comparacion_a1.json") or {}).get("veredicto")
    info["a1_veredicto"] = v
    if v != "REPRODUCIBLE":
        problemas.append(f"A1 no dio REPRODUCIBLE ({v}): no hay un contrafáctico único")
    # 2
    cambiados = [p for p, h in (ea.get("huellas") or {}).items() if not p.endswith(SOLO_ANALISIS) and sha(Path(p)) != h]
    info["archivos_comparados"] = len(ea.get("huellas") or {})
    if not ea.get("huellas"):
        problemas.append("A1 no registró huellas del código")
    if cambiados:
        problemas.append(f"el código cambió desde A1: {cambiados}")
    # 3
    va = (leer(a1 / "ollama_version.json") or {}).get("version")
    try:
        vb = version_actual(url)
    except OSError as e:
        vb = None
        problemas.append(f"Ollama no responde: {e!r}")
    info["ollama_version"] = {"a1": va, "ahora": vb}
    if va != vb:
        problemas.append(f"la versión de Ollama cambió: A1 {va} / ahora {vb}")
    # 4
    da = digest_a1(ca, (ea.get("corridas") or [""])[0])
    try:
        db = digest_actual(url)
    except OSError:
        db = None
    info["digest"] = {"a1": da, "ahora": db}
    if modo == "real":
        if not da or not db:
            problemas.append(f"no se puede verificar que el modelo sea el mismo (digest A1 {da}, ahora {db})")
        elif da != db:
            problemas.append(f"el modelo cambió desde A1: digest {da[:19]} → {db[:19]}")
    # 5
    dec = decisiones_a1(a1, ea.get("corridas") or [])
    info["decisiones_a1"] = dec
    distintas = {json.dumps(x["decisiones"]) for x in dec.values()}
    if len(distintas) > 1:
        problemas.append(f"el coordinador decidió distinto en las corridas de A1: {dec}")
    primera = next(iter(dec.values()), {"decisiones": [], "despachos": []})
    enviables = [d for d in primera["despachos"] if d[1] == "simulado"]
    if not primera["decisiones"]:
        problemas.append("en A1 el coordinador nunca decidió `nueva_estrategia`: B1 no intervendría")
    elif not enviables:
        problemas.append(f"en A1 el despachador no habría enviado nada ({primera['despachos']}): B1 no intervendría")
    info["prediccion"] = {"decision_en_la_iteracion": primera["decisiones"][0][0] if primera["decisiones"] else None,
                          "mensaje_sha256": primera["decisiones"][0][1] if primera["decisiones"] else None,
                          "nota": "el mensaje entra en la primera llamada que empieza después de la entrega (latencia variable)"}
    return problemas, info


def escribir(salida: Path, a1: Path, modo: str, url: str, nombre: str, n: int, corridas: Path, info: dict) -> None:
    ea = leer(a1 / "experimento.json") or {}
    huellas = {p: sha(Path(p)) for p in ea.get("huellas", {})}       # las mismas rutas que A1
    huellas[str(AQUI / "despachador.py")] = sha(AQUI / "despachador.py")
    json.dump({"experimento": "B1 efecto del coordinador", "modo": modo, "problema": ea.get("problema"),
               "modelo": ea.get("modelo"), "semilla": ea.get("semilla"), "temperatura": ea.get("temperatura"), "ollama": url,
               "coordinador": "observa, decide y ENVÍA (cadena --enviar)", "a1": str(a1),
               "limites": {k: os.environ.get(k) for k in ("MAX_ITER", "MAX_SEG", "GRACIA", "NUM_CTX", "REGISTRO_LLM")},
               "arranque": ea.get("arranque"), "corridas": [f"{nombre}-r{k}" for k in range(1, n + 1)],
               "corridas_dir": str(corridas), "comprobaciones": info, "huellas": huellas},
              open(salida, "w"), ensure_ascii=False, indent=1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--a1", type=Path, required=True)
    ap.add_argument("--salida", type=Path, required=True)
    ap.add_argument("--modo", choices=("real", "simulado"), required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--nombre", required=True)
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--corridas", type=Path, required=True)
    a = ap.parse_args(argv)
    problemas, info = comprobar(a.a1, a.modo, a.url)
    print(f"  A1: {info['a1_veredicto']} · {info['archivos_comparados']} archivos comparados · Ollama {info['ollama_version']} · "
          f"digest {info['digest']}")
    print(f"  predicción: decisión de nueva_estrategia en la iteración {info['prediccion']['decision_en_la_iteracion']}")
    for p in problemas:
        print(f"  NO: {p}")
    if problemas:
        return 3
    escribir(a.salida, a.a1, a.modo, a.url, a.nombre, a.n, a.corridas, info)
    print("  comprobaciones previas: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
