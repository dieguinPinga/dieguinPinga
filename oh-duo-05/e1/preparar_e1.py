"""Comprobaciones previas de E1 contra A1 y registro de las diferencias exactas. SOLO LECTURA (salvo experimento.json).

  python3 preparar_e1.py --a1 DIR --plan DIR/plan.json --salida DIR/experimento.json --modo real|simulado --url URL
                         --nombre e1-p1-s11-<et> --corridas DIR

Aborta (salida 3) si: A1 no dio REPRODUCIBLE; cambió el código que afecta a las corridas (huellas de A1; el
ejecutor de A1 tiene que seguir intacto); cambió la versión de Ollama o el modelo (digest, en modo real). Registra
el diff unificado entre el ejecutor de A1 y el de E1 y, por condición, qué se agrega al contexto respecto de A1.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
sys.path[:0] = [str(RAIZ / "reproducibilidad")]
import preparar_b1 as PB  # noqa: E402


def comprobar(a1: Path, modo: str, url: str) -> tuple[list[str], dict]:
    problemas, info = [], {}
    ea = PB.leer(a1 / "experimento.json") or {}
    v = (PB.leer(a1 / "comparacion_a1.json") or {}).get("veredicto")
    info["a1_veredicto"] = v
    if v != "REPRODUCIBLE":
        problemas.append(f"A1 no dio REPRODUCIBLE ({v}): no hay una trayectoria única que reproducir")
    cambiados = [p for p, h in (ea.get("huellas") or {}).items() if not p.endswith(PB.SOLO_ANALISIS) and PB.sha(Path(p)) != h]
    if not ea.get("huellas"):
        problemas.append("A1 no registró huellas del código")
    if cambiados:
        problemas.append(f"el código cambió desde A1: {cambiados}")
    va = (PB.leer(a1 / "ollama_version.json") or {}).get("version")
    try:
        vb = PB.version_actual(url)
    except OSError as e:
        vb = None
        problemas.append(f"Ollama no responde: {e!r}")
    info["ollama_version"] = {"a1": va, "ahora": vb}
    if va != vb:
        problemas.append(f"la versión de Ollama cambió: A1 {va} / ahora {vb}")
    ca = Path(ea.get("corridas_dir") or RAIZ / "corridas")
    da = PB.digest_a1(ca, (ea.get("corridas") or [""])[0])
    try:
        db = PB.digest_actual(url)
    except OSError:
        db = None
    info["digest"] = {"a1": da, "ahora": db}
    if modo == "real":
        if not da or not db:
            problemas.append(f"no se puede verificar que el modelo sea el mismo (digest A1 {da}, ahora {db})")
        elif da != db:
            problemas.append(f"el modelo cambió desde A1: digest {da[:19]} → {db[:19]}")
    return problemas, info


def diff(a: Path, b: Path) -> str:
    return "".join(difflib.unified_diff(a.read_text().splitlines(True), b.read_text().splitlines(True),
                                        fromfile=str(a.relative_to(RAIZ)), tofile=str(b.relative_to(RAIZ))))


def escribir(salida: Path, a1: Path, plan: dict, modo: str, url: str, nombre: str, corridas: Path, info: dict) -> None:
    ea = PB.leer(a1 / "experimento.json") or {}
    archivos = [AQUI / f for f in ("ejecutor/correr.sh", "ejecutor/agente.py", "ejecutar_e1.py", "plan.py", "directo.py",
                                   "analizar_e1.py", "preparar_e1.py")] + [RAIZ / "ejecutor" / "puente.py"]
    conds = {c: f"{nombre}-{c.split('-')[1].lower()}" for c in plan["condiciones"]}
    json.dump({
        "experimento": "E1 de la solución a la edición", "modo": modo, "a1": str(a1), "problema": ea.get("problema"),
        "modelo": ea.get("modelo"), "semilla": ea.get("semilla"), "temperatura": ea.get("temperatura"), "ollama": url,
        "limites": {k: os.environ.get(k) for k in ("MAX_ITER", "MAX_SEG", "GRACIA", "NUM_CTX", "REGISTRO_LLM")},
        "arranque": ea.get("arranque"), "corridas": conds, "corridas_dir": str(corridas), "comprobaciones": info,
        "plan": {k: plan[k] for k in ("k", "j", "sonda_a_sha256", "sonda_b_sha256", "prompt_a0_sha256", "max_acciones")},
        "diferencias": {
            "ejecutor_respecto_de_a1": {"correr.sh": diff(RAIZ / "ejecutor" / "correr.sh", AQUI / "ejecutor" / "correr.sh"),
                                        "agente.py": diff(RAIZ / "ejecutor" / "agente.py", AQUI / "ejecutor" / "agente.py")},
            "E1-A0": "sin OpenHands: un único pedido de usuario con el código y las pruebas visibles auténticos "
                     "(plantilla prompt_a0); mismas opciones de inferencia salvo stop; sin system prompt ni herramientas",
            "E1-AB": f"idéntico a A1 hasta la observación de la acción {plan['k']}; después se agrega la pregunta A "
                     f"(tope {plan['max_acciones']['A']} acciones) y, al terminar o pausar, la pregunta B (tope {plan['max_acciones']['B']})",
            "E1-C": f"idéntico a A1 hasta la observación de la acción {plan['j']} (la edición rechazada real de A1); "
                    "después las mismas preguntas A y B con los mismos topes" if plan["j"] is not None else None,
            "sin_coordinador": "E1 no corre la cadena observador/coordinador/despachador (en A1 corría sin enviar); "
                               "solo cambia la carga de la máquina, y la identidad del prefijo lo verifica"},
        "huellas": {str(p): PB.sha(p) for p in archivos},
    }, open(salida, "w"), ensure_ascii=False, indent=1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--a1", type=Path, required=True)
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--salida", type=Path, required=True)
    ap.add_argument("--modo", choices=("real", "simulado"), required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--nombre", required=True)
    ap.add_argument("--corridas", type=Path, required=True)
    a = ap.parse_args(argv)
    problemas, info = comprobar(a.a1, a.modo, a.url)
    print(f"  A1: {info['a1_veredicto']} · Ollama {info['ollama_version']} · digest {info['digest']}")
    for p in problemas:
        print(f"  NO: {p}")
    if problemas:
        return 3
    escribir(a.salida, a.a1, json.loads(a.plan.read_text()), a.modo, a.url, a.nombre, a.corridas, info)
    print("  comprobaciones previas: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
