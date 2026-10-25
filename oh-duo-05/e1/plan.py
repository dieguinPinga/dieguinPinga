"""E1: textos fijos de las preguntas y puntos de inserción calculados desde la trayectoria de A1. SOLO LECTURA.

  python3 plan.py --a1 informes/a1-p1-s11-<et> --salida DIR       escribe plan.json, guion_ab.json y guion_c.json

Puntos (índices de acción desde 0, en la trayectoria determinista de A1):
  k  primera acción tras cuya observación el agente ya vio el CÓDIGO de intervalos.py (def unir_intervalos) y el
     FALLO de la prueba visible (AssertionError/FAIL). Si en A1 nunca vio el código, k es la primera acción tras la
     que vio el fallo (codigo_a_la_vista = no, y eso ya es un hallazgo: no reunió la información). La pregunta A le
     permite abrir el archivo antes de responder, así que es la misma en los dos casos.
  j  primera edición de A1 que la herramienta RECHAZÓ (file_editor sin efecto). E1-C pregunta justo después de esa
     observación real: mismas preguntas, con un rechazo real en el contexto.
Las preguntas no contienen la solución; piden lo que el agente ya tiene a la vista (qué falla, dónde) y que aplique
lo que él mismo dijo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path[:0] = [str(AQUI.parent / "diagnostico"), str(AQUI.parent)]
import analizar_par as A  # noqa: E402

SONDA_A = ("Before changing anything, stop and diagnose. If you have not read intervalos.py yet, open it first; "
           "do not edit any file. Then answer in plain text: "
           "(1) which test fails, and what result it expected versus what it got; "
           "(2) which line of intervalos.py produces the wrong result; "
           "(3) the exact corrected line.")
SONDA_B = ("Now apply exactly the corrected line you stated to intervalos.py with the file editor, "
           "then run the tests with: python3 -m unittest -v")
MAX_A, MAX_B = 2, 6             # acciones permitidas después de cada pregunta antes de pausar (A: para abrir el archivo)
PROMPT_A0 = ("The module intervalos.py below has a bug: its tests fail. Find the bug and return the complete corrected "
             "intervalos.py in a single Python code block.\n\nintervalos.py:\n```python\n{codigo}\n```\n\n"
             "test_intervalos.py:\n```python\n{pruebas}\n```\n")


def sha(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def texto_obs(o: dict | None) -> str:
    obs = (o or {}).get("observation") or {}
    return "".join(c.get("text", "") for c in obs.get("content") or [] if isinstance(c, dict)) or str((o or {}).get("error") or "")


def puntos(corrida: Path) -> dict:
    loc = A.localizar(corrida)
    eventos = A.cargar_eventos(loc["events"])
    obs = {e.get("action_id"): e for e in eventos if e.get("kind") == "ObservationEvent"}
    err = {e.get("tool_call_id"): e for e in eventos if e.get("kind") == "AgentErrorEvent"}
    vio_codigo = vio_fallo = False
    k = k_fallo = k_codigo = j = None
    detalle_j = None
    n = 0
    for e in eventos:
        if e.get("kind") != "ActionEvent":
            continue
        o = obs.get(e.get("id")) or err.get(e.get("tool_call_id"))
        t = texto_obs(o)
        vio_codigo = vio_codigo or "def unir_intervalos" in t
        vio_fallo = vio_fallo or "AssertionError" in t or "FAIL:" in t
        if k_fallo is None and vio_fallo:
            k_fallo = n
        if k_codigo is None and vio_codigo:
            k_codigo = n
        if k is None and vio_codigo and vio_fallo:
            k = n
        a = e.get("action") or {}
        if j is None and e.get("tool_name") == "file_editor" and a.get("command") not in (None, "view") \
                and not ("has been edited" in t or "created successfully" in t.lower()):
            j = n
            detalle_j = {"ruta": a.get("path"), "comando": a.get("command"), "old_str": a.get("old_str"),
                         "new_str": a.get("new_str"), "observacion": t[:600]}
        n += 1
    return {"k": k if k is not None else k_fallo, "codigo_a_la_vista_en_k": k is not None, "a1_vio_el_fallo_en": k_fallo,
            "a1_vio_el_codigo_en": k_codigo, "j": j, "acciones_a1": n, "edicion_rechazada": detalle_j}


def guion(condicion: str, tras: int) -> dict:
    return {"condicion": condicion, "tras_accion": tras,
            "sondas": [{"id": "A", "texto": SONDA_A, "max_acciones": MAX_A}, {"id": "B", "texto": SONDA_B, "max_acciones": MAX_B}]}


def planificar(a1: Path, corridas: Path | None, salida: Path) -> dict:
    ea = json.loads((a1 / "experimento.json").read_text())
    ca = corridas or Path(ea.get("corridas_dir") or AQUI.parent / "corridas")
    p = puntos(ca / ea["corridas"][0])
    avisos = []
    if p["k"] is None:
        avisos.append("en A1 el agente nunca vio el fallo de la prueba: E1-AB no es aplicable")
    elif not p["codigo_a_la_vista_en_k"]:
        avisos.append(f"en A1 el agente NUNCA leyó intervalos.py (vio el fallo en la acción {p['a1_vio_el_fallo_en']}): "
                      "la pregunta A le permite abrirlo antes de responder")
    if p["j"] is None:
        avisos.append("A1 no tiene ninguna edición rechazada: E1-C no es aplicable")
    elif p["k"] is not None and p["j"] < p["k"]:
        avisos.append(f"el rechazo (acción {p['j']}) ocurre antes de ver código y fallo (acción {p['k']}): E1-C pregunta con menos contexto")
    salida.mkdir(parents=True, exist_ok=True)
    plan = {"a1": str(a1), "corrida_de_referencia": ea["corridas"][0], **p, "avisos": avisos,
            "sonda_a": SONDA_A, "sonda_a_sha256": sha(SONDA_A), "sonda_b": SONDA_B, "sonda_b_sha256": sha(SONDA_B),
            "max_acciones": {"A": MAX_A, "B": MAX_B}, "prompt_a0_plantilla": PROMPT_A0, "prompt_a0_sha256": sha(PROMPT_A0),
            "condiciones": {}}
    if p["k"] is not None:
        (salida / "guion_ab.json").write_text(json.dumps(guion("E1-AB", p["k"]), ensure_ascii=False, indent=1))
        plan["condiciones"]["E1-AB"] = {"tras_accion": p["k"], "guion": "guion_ab.json"}
    if p["j"] is not None:
        (salida / "guion_c.json").write_text(json.dumps(guion("E1-C", p["j"]), ensure_ascii=False, indent=1))
        plan["condiciones"]["E1-C"] = {"tras_accion": p["j"], "guion": "guion_c.json"}
    (salida / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1))
    return plan


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--a1", type=Path, required=True)
    ap.add_argument("--corridas", type=Path)
    ap.add_argument("--salida", type=Path, required=True)
    a = ap.parse_args(argv)
    p = planificar(a.a1, a.corridas, a.salida)
    print(f"  A1 {p['corrida_de_referencia']}: {p['acciones_a1']} acciones · vio el fallo en {p['a1_vio_el_fallo_en']}, "
          f"el código en {p['a1_vio_el_codigo_en']} · k = {p['k']} (código a la vista: {p['codigo_a_la_vista_en_k']}) · "
          f"j (primera edición rechazada) = {p['j']}")
    if p["edicion_rechazada"]:
        r = p["edicion_rechazada"]
        print(f"  edición rechazada en A1: {r['comando']} {r['ruta']}: {str(r['old_str'])[:60]!r} → {str(r['new_str'])[:60]!r}")
    for x in p["avisos"]:
        print(f"  aviso: {x}")
    print(f"  condiciones: {list(p['condiciones'])}")
    return 0 if p["condiciones"] else 3


if __name__ == "__main__":
    sys.exit(main())
