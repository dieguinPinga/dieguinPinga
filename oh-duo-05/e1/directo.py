"""E1-A0: diagnóstico directo, sin OpenHands ni herramientas (réplica registrada de la prueba manual).

  python3 directo.py --url URL --salida DIR [--prompt ARCHIVO] [--sin-pedido] [--evaluar ARCHIVO ...]

Un pedido a /api/chat con el código y las pruebas visibles AUTÉNTICOS de P1 (oh-duo-01/banco/P1/tarea, solo
lectura) y las mismas opciones de inferencia que el agente: temperatura 0,2, semilla 11, num_ctx 16384,
num_predict 2048, think false (el agente agrega stop "</function", que aquí no corresponde: no hay herramientas).
Registra pedido y respuesta íntegros con sus sha256. Del texto extrae SOLO el código de producción (con ast: se
descartan clases de unittest, `import unittest` y el bloque __main__) y lo evalúa en un bwrap nuevo, sobre una
copia, con las pruebas visibles y las ocultas por separado. --evaluar evalúa además archivos ya existentes (por
ejemplo ~/pruebas-qwen/P1_REAL_DIRECTO/intervalos_extraido.py) sin hacer ningún pedido.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import shutil
import sys
import urllib.request
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path[:0] = [str(AQUI), str(AQUI.parent / "diagnostico"), str(AQUI.parent)]
import plan as PLAN  # noqa: E402
from ejecutar import hermano  # noqa: E402

OPCIONES = {"temperature": 0.2, "seed": 11, "num_ctx": 16384, "num_predict": 2048}


def sha(t: str | bytes) -> str:
    return hashlib.sha256(t if isinstance(t, bytes) else t.encode()).hexdigest()


def pedido(tarea: Path, prompt: str | None = None, modelo: str = "qwen2.5-coder:7b") -> dict:
    codigo = (tarea / "intervalos.py").read_text(encoding="utf-8")
    pruebas = (tarea / "test_intervalos.py").read_text(encoding="utf-8")
    texto = prompt if prompt is not None else PLAN.PROMPT_A0.format(codigo=codigo.rstrip("\n"), pruebas=pruebas.rstrip("\n"))
    return {"model": modelo, "messages": [{"role": "user", "content": texto}], "stream": False, "think": False,
            "options": dict(OPCIONES)}


def bloques(texto: str) -> list[str]:
    return re.findall(r"```(?:python|py)?\s*\n(.*?)```", texto, re.S)


def solo_produccion(fuente: str) -> str | None:
    """El módulo sin pruebas: quita clases que heredan de unittest.TestCase, `import unittest`/`from unittest`,
    `from intervalos import …` (la prueba importándose el módulo) y el bloque `if __name__ == "__main__"`. None si no hay una función unir_intervalos que compile."""
    try:
        arbol = ast.parse(fuente)
    except SyntaxError:
        return None
    lineas = fuente.splitlines(keepends=True)
    fuera = []
    for n in arbol.body:
        prueba = isinstance(n, ast.ClassDef) and any("TestCase" in ast.unparse(b) for b in n.bases)
        imp = isinstance(n, (ast.Import, ast.ImportFrom)) and ("unittest" in ast.unparse(n)
                                                              or (isinstance(n, ast.ImportFrom) and n.module == "intervalos"))
        main = isinstance(n, ast.If) and "__name__" in ast.unparse(n.test)
        if prueba or imp or main:
            ini = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
            fuera.append((ini - 1, n.end_lineno))
    if not any(isinstance(n, ast.FunctionDef) and n.name == "unir_intervalos" for n in arbol.body):
        return None
    quedan = [l for i, l in enumerate(lineas) if not any(a <= i < b for a, b in fuera)]
    return re.sub(r"\n{3,}", "\n\n\n", "".join(quedan)).rstrip("\n") + "\n"


def extraer(respuesta: str) -> dict:
    candidatos = [b for b in bloques(respuesta) if "def unir_intervalos" in b] or ([respuesta] if "def unir_intervalos" in respuesta else [])
    for c in candidatos:
        prod = solo_produccion(c)
        if prod:
            return {"codigo": prod, "bloque_incluia_pruebas": "TestCase" in c, "bloques": len(bloques(respuesta))}
    return {"codigo": None, "bloque_incluia_pruebas": None, "bloques": len(bloques(respuesta))}


def evaluar(codigo: str, oh1: Path, bwrap: str | None) -> dict:
    import auditar_b1 as AUD
    ev = AUD.Evaluador(oh1, bwrap)
    r = ev.evaluar(codigo)
    ref = ev.referencia
    r["identico_a_la_referencia"] = codigo.strip() == ref.strip()
    r["sha256"] = sha(codigo)
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url")
    ap.add_argument("--salida", type=Path, required=True)
    ap.add_argument("--prompt", type=Path, help="texto exacto del pedido (por omisión, la plantilla de plan.py)")
    ap.add_argument("--sin-pedido", action="store_true", help="no hacer ningún pedido: solo evaluar --evaluar")
    ap.add_argument("--evaluar", type=Path, nargs="*", default=[])
    a = ap.parse_args(argv)
    oh1 = hermano("oh-duo-01", "duo.py")
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise SystemExit("hace falta bwrap: el código del modelo solo se ejecuta aislado")
    a.salida.mkdir(parents=True, exist_ok=True)
    r = {"evaluados": {}}
    for f in a.evaluar:
        if f.is_file():
            c = f.read_text(encoding="utf-8")
            r["evaluados"][str(f)] = {"sha256": sha(c), **evaluar(c, oh1, bwrap)}
    if not a.sin_pedido:
        if not a.url:
            raise SystemExit("falta --url")
        tarea = oh1 / "banco" / "P1" / "tarea"
        body = pedido(tarea, a.prompt.read_text(encoding="utf-8") if a.prompt else None)
        (a.salida / "pedido.json").write_text(json.dumps(body, ensure_ascii=False, indent=1))
        req = urllib.request.Request(a.url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as resp:
            crudo = resp.read()
        (a.salida / "respuesta.json").write_bytes(crudo)
        d = json.loads(crudo)
        texto = (d.get("message") or {}).get("content") or ""
        (a.salida / "respuesta.txt").write_text(texto)
        x = extraer(texto)
        r.update({"pedido_sha256": sha(json.dumps(body, sort_keys=True)), "respuesta_sha256": sha(texto),
                  "prompt_sha256": sha(body["messages"][0]["content"]), "opciones": body["options"],
                  "tarea_sha256": {f.name: sha(f.read_bytes()) for f in sorted(tarea.iterdir())},
                  "ollama": {k: d.get(k) for k in ("model", "created_at", "done_reason", "prompt_eval_count", "eval_count",
                                                   "total_duration", "load_duration")},
                  "extraccion": {k: v for k, v in x.items() if k != "codigo"}})
        if x["codigo"]:
            (a.salida / "intervalos_extraido.py").write_text(x["codigo"])
            r["evaluacion"] = evaluar(x["codigo"], oh1, bwrap)
        else:
            r["evaluacion"] = None
    (a.salida / "directo.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))
    e = r.get("evaluacion") or {}
    if not a.sin_pedido:
        print(f"  A0: código extraído={'sí' if e else 'no'} · visibles {(e.get('visibles') or {}).get('pasan')}/"
              f"{(e.get('visibles') or {}).get('ejecutadas')} · ocultas {(e.get('ocultas') or {}).get('pasan')}/"
              f"{(e.get('ocultas') or {}).get('ejecutadas')} · idéntico a la referencia: {e.get('identico_a_la_referencia')}")
    for f, v in r["evaluados"].items():
        print(f"  {f}: visibles {v['visibles']['pasan']}/{v['visibles']['ejecutadas']} · ocultas {v['ocultas']['pasan']}/"
              f"{v['ocultas']['ejecutadas']} · idéntico a la referencia: {v['identico_a_la_referencia']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
