"""Laboratorio de reparación: varias misiones, una tras otra, cada una con el supervisor de oh-supervisor.

Por cada misión del banco arma una INSTANCIA con el mismo contrato que oh-smoke (correr.sh, smoke.py,
relay.py, tarea/, ocultas/, control/, venv, resultados/) y le pasa esa carpeta, sin cambios, a
oh-supervisor/supervisor.py: hasta 3 intentos independientes, cada uno desde el código defectuoso original.
Cuando una misión termina (éxito o intentos agotados) sigue con la siguiente, sin intervención. Al final, y
después de cada misión, escribe registros/<corrida>/informe.json.

No modifica oh-smoke ni oh-supervisor: de oh-smoke usa venv/ (solo lectura, como enlace), relay.py y los
requisitos (copias); de oh-supervisor importa supervisor.py tal cual. Solo biblioteca estándar.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"
AQUI = Path(__file__).resolve().parent
CATEGORIAS = {"cálculo": 4, "sobrescritura": 4, "condición": 2, "omisión": 2}
MODULO_VALIDO = re.compile(r"[a-z_][a-z0-9_]*")


class BancoInvalido(SystemExit):
    pass


def ahora() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def huella_arbol(raiz: Path) -> str:
    """sha256 de todos los archivos de un árbol (rutas + contenido), sin seguir enlaces."""
    h = hashlib.sha256()
    for p in sorted(raiz.rglob("*")):
        if "__pycache__" in p.parts or p.is_symlink() or not p.is_file():
            continue
        h.update(str(p.relative_to(raiz)).encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def huella(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def cargar_banco(banco: Path, completo: bool = True) -> list[dict]:
    """Lee y valida banco/<ID>/mision.json y sus archivos. Con completo=True exige la distribución 4/4/2/2."""
    misiones = []
    for d in sorted(p for p in banco.iterdir() if p.is_dir()):
        try:
            m = json.loads((d / "mision.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise BancoInvalido(f"{d.name}: mision.json ilegible ({e})")
        mod = m.get("modulo", "")
        if m.get("id") != d.name or not MODULO_VALIDO.fullmatch(mod) or m.get("categoria") not in CATEGORIAS:
            raise BancoInvalido(f"{d.name}: id, módulo o categoría inválidos")
        tarea = sorted(p.name for p in (d / "tarea").iterdir()) if (d / "tarea").is_dir() else []
        if tarea != sorted([f"{mod}.py", f"test_{mod}.py"]):
            raise BancoInvalido(f"{d.name}: tarea/ debe tener exactamente {mod}.py y test_{mod}.py")
        if not (d / "ocultas" / "test_ocultas.py").is_file() or not (d / "control" / f"{mod}.py").is_file():
            raise BancoInvalido(f"{d.name}: faltan ocultas/test_ocultas.py o control/{mod}.py")
        misiones.append({**m, "dir": d})
    if len({m["modulo"] for m in misiones}) != len(misiones):
        raise BancoInvalido("hay módulos repetidos entre misiones")
    if completo:
        cuenta = {c: sum(m["categoria"] == c for m in misiones) for c in CATEGORIAS}
        if cuenta != CATEGORIAS:
            raise BancoInvalido(f"distribución de categorías {cuenta}, se esperaba {CATEGORIAS}")
    return misiones


def armar_instancia(m: dict, destino: Path, oh_smoke: Path, ejecutor: Path) -> Path:
    """Carpeta con el contrato de oh-smoke para UNA misión. mision.json solo lleva id y módulo: la categoría
    y la descripción del defecto no salen del banco."""
    destino.mkdir(parents=True)
    for sub in ("tarea", "ocultas", "control"):
        shutil.copytree(m["dir"] / sub, destino / sub, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ejecutor / "correr.sh", destino / "correr.sh")
    shutil.copy2(ejecutor / "agente.py", destino / "smoke.py")
    for f in ("relay.py", "requisitos.lock", "requisitos-tools.lock"):
        if (oh_smoke / f).is_file():
            shutil.copy2(oh_smoke / f, destino / f)
    (destino / "venv").symlink_to(oh_smoke / "venv")
    (destino / "mision.json").write_text(json.dumps({"id": m["id"], "modulo": m["modulo"]}) + "\n")
    (destino / "resultados").mkdir()
    os.chmod(destino / "correr.sh", 0o755)
    return destino


def cargar_supervisor(oh_supervisor: Path):
    spec = importlib.util.spec_from_file_location("oh_supervisor_supervisor", oh_supervisor / "supervisor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _suma(valores):
    vs = [v for v in valores if isinstance(v, (int, float))]
    return sum(vs) if vs else 0


def resumen_mision(m: dict, r: dict | None, registros: Path) -> dict:
    if r is None:
        return {"id": m["id"], "categoria": m["categoria"], "modulo": m["modulo"], "ejecutada": False,
                "resuelta": False}
    intentos = r.get("intentos", [])
    return {
        "id": m["id"], "categoria": m["categoria"], "modulo": m["modulo"], "ejecutada": True,
        "resuelta": bool(r.get("exito")), "intento_exitoso": r.get("intento_exitoso"),
        "intentos_usados": len(intentos), "motivo_detencion": r.get("motivo_detencion"),
        "segundos": r.get("segundos_total"),
        "llamadas_llm": _suma(i.get("llamadas_llm") for i in intentos),
        "tokens_prompt": _suma(i.get("tokens_prompt") for i in intentos),
        "tokens_generados": _suma(i.get("tokens_generados") for i in intentos),
        "intentos": [{k: i.get(k) for k in ("intento", "semilla", "segundos", "exito", "motivo",
                                            "criterios_fallidos", "llamadas_llm", "tokens_prompt",
                                            "tokens_generados", "carpeta")} for i in intentos],
        "registro": str(registros),
    }


def informe(corrida: dict, misiones: list[dict]) -> dict:
    ejecutadas = [m for m in misiones if m["ejecutada"]]
    por_categoria = {}
    for c in CATEGORIAS:
        ms = [m for m in ejecutadas if m["categoria"] == c]
        res = sum(m["resuelta"] for m in ms)
        por_categoria[c] = {"misiones": len(ms), "resueltas": res,
                            "porcentaje_exito": round(100 * res / len(ms), 1) if ms else None}
    motivos: dict[str, int] = {}
    for m in ejecutadas:
        for i in m["intentos"]:
            if not i["exito"]:
                motivos[i["motivo"]] = motivos.get(i["motivo"], 0) + 1
    resueltas = [m["id"] for m in ejecutadas if m["resuelta"]]
    return {
        **corrida,
        "totales": {
            "misiones": len(misiones), "ejecutadas": len(ejecutadas), "resueltas": len(resueltas),
            "porcentaje_exito": round(100 * len(resueltas) / len(ejecutadas), 1) if ejecutadas else None,
            "intentos": sum(m["intentos_usados"] for m in ejecutadas),
            "segundos": round(_suma(m["segundos"] for m in ejecutadas), 1),
            "llamadas_llm": _suma(m["llamadas_llm"] for m in ejecutadas),
            "tokens_prompt": _suma(m["tokens_prompt"] for m in ejecutadas),
            "tokens_generados": _suma(m["tokens_generados"] for m in ejecutadas),
        },
        "resueltas": resueltas,
        "no_resueltas": [m["id"] for m in ejecutadas if not m["resuelta"]],
        "no_ejecutadas": [m["id"] for m in misiones if not m["ejecutada"]],
        "por_categoria": por_categoria,
        "motivos_de_fallo": dict(sorted(motivos.items(), key=lambda x: -x[1])),
        "intentos_por_mision": {m["id"]: m["intentos_usados"] for m in ejecutadas},
        "misiones": misiones,
    }


def correr_laboratorio(oh_smoke: Path, oh_supervisor: Path, semillas: list[int], ids: list[str] | None,
                       registros: Path, instancias: Path, banco: Path = AQUI / "banco",
                       ejecutor: Path = AQUI / "ejecutor") -> dict:
    misiones = cargar_banco(banco, completo=ids is None)
    if ids is not None:
        conocidas = {m["id"] for m in misiones}
        if desconocidas := [i for i in ids if i not in conocidas]:
            raise SystemExit(f"misiones desconocidas: {desconocidas}")
        misiones = [m for m in misiones if m["id"] in ids]
    if not (oh_smoke / "venv").is_dir() and not os.environ.get("OH_LAB_SIN_VENV"):
        raise SystemExit(f"no existe {oh_smoke / 'venv'}")
    sup = cargar_supervisor(oh_supervisor)

    registros.mkdir(parents=True, exist_ok=True)
    candado = open(registros / ".candado", "w")
    try:
        fcntl.flock(candado, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        candado.close()
        raise SystemExit("ya hay otro laboratorio en ejecución")
    nombre = datetime.now().strftime("%Y%m%d-%H%M%S")
    reg, inst = registros / nombre, instancias / nombre
    reg.mkdir()

    huella_banco = huella_arbol(banco)
    corrida = {
        "laboratorio": VERSION, "inicio": ahora(), "semillas": semillas, "max_intentos": len(semillas),
        "huellas": {"banco": huella_banco, "ejecutor_correr": huella(ejecutor / "correr.sh"),
                    "ejecutor_agente": huella(ejecutor / "agente.py"),
                    "supervisor": huella(oh_supervisor / "supervisor.py"),
                    "relay": huella(oh_smoke / "relay.py") if (oh_smoke / "relay.py").is_file() else None},
        "supervisor_version": getattr(sup, "VERSION", None),
        "motivo_detencion": None,
    }
    resultados = [resumen_mision(m, None, reg / m["id"]) for m in misiones]

    def guardar():
        datos = informe({**corrida, "fin": ahora()}, resultados)
        (reg / "informe.json").write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
        return datos

    t0 = time.monotonic()
    try:
        # Todas las instancias se arman antes de empezar: un banco o una copia defectuosa frena antes de la GPU.
        rutas = [armar_instancia(m, inst / m["id"], oh_smoke, ejecutor) for m in misiones]
        for k, (m, ruta) in enumerate(zip(misiones, rutas)):
            print(f"#### Misión {m['id']} ({k + 1}/{len(misiones)})", flush=True)
            r = sup.supervisar(ruta, semillas, reg / m["id"])
            resultados[k] = resumen_mision(m, r, reg / m["id"])
            guardar()
            if huella_arbol(banco) != huella_banco:
                corrida["motivo_detencion"] = "integridad"
                corrida["detalle_detencion"] = f"el banco cambió durante la misión {m['id']}"
                break
            if r.get("motivo_detencion") in ("integridad", "interrumpido"):
                corrida["motivo_detencion"] = r["motivo_detencion"]
                corrida["detalle_detencion"] = f"{m['id']}: {r.get('detalle_detencion', '')}".strip(": ")
                break
        else:
            corrida["motivo_detencion"] = "completo"
    except KeyboardInterrupt:
        corrida["motivo_detencion"] = "interrumpido"
    finally:
        corrida["segundos_total"] = round(time.monotonic() - t0, 1)
        datos = guardar()
        candado.close()
    return datos


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--oh-smoke", type=Path, default=Path.home() / "oh-smoke")
    p.add_argument("--oh-supervisor", type=Path, default=Path.home() / "oh-supervisor")
    p.add_argument("--misiones", default="todas", help="«todas» o ids separados por coma (p. ej. C01,K02)")
    p.add_argument("--semillas", default="11,12,13", help="de 1 a 3 semillas distintas (mismas para cada misión)")
    p.add_argument("--registros", type=Path, default=AQUI / "registros")
    p.add_argument("--instancias", type=Path, default=AQUI / "instancias")
    a = p.parse_args(argv)
    semillas = [int(s) for s in a.semillas.split(",") if s.strip()]
    if not 1 <= len(semillas) <= 3 or len(set(semillas)) != len(semillas):
        raise SystemExit("de 1 a 3 semillas distintas")
    ids = None if a.misiones == "todas" else [x.strip() for x in a.misiones.split(",") if x.strip()]
    datos = correr_laboratorio(a.oh_smoke.resolve(), a.oh_supervisor.resolve(), semillas, ids,
                               a.registros.resolve(), a.instancias.resolve())
    t = datos["totales"]
    print(f"#### {datos['motivo_detencion']}: {t['resueltas']}/{t['ejecutadas']} resueltas, {t['intentos']} "
          f"intentos, {t['segundos']} s. Informe: {a.registros.resolve()}/…/informe.json")
    return 0 if datos["motivo_detencion"] == "completo" else 1


if __name__ == "__main__":
    sys.exit(main())
