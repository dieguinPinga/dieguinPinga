"""OH-DUO-05: cadena en vivo observador (OH-DUO-03) → coordinador (OH-DUO-04) → despachador → buzón.

  python3 cadena.py --corrida corridas/NOMBRE [--enviar] [--maximo N]

Se lanza en otra terminal antes (espera hasta 10 min la marca) o mientras ejecutar.py corre la misma corrida. Importa OH-DUO-03 y OH-DUO-04
sin modificarlos. Solo acepta corridas de oh-duo-05 (marca .oh-duo-05): nunca las de producción de OH-DUO-01.
Sin --enviar, la cadena completa corre pero el despachador no escribe en el buzón (corrida de control).
Salida, fuera de lo observado: informes/<corrida>/{observador/, coordinador/, despachos.jsonl}.
Solo biblioteca estándar.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import threading
import time
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
import despachador as D  # noqa: E402
from ejecutar import MARCA, hermano  # noqa: E402


def importar(nombre: str, ruta: Path):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def correr(corrida: Path, salida: Path, oh_duo_02: Path, oh_duo_03: Path, oh_duo_04: Path, enviar: bool,
           maximo: int | None = None, intervalo: float = 0.5, min_restantes: int = 5, advertencias: bool = False,
           espera_marca: float = 600.0, termina=lambda: False) -> dict:
    corrida = corrida.resolve()
    limite = time.monotonic() + espera_marca      # ejecutar.py crea la marca al empezar; se la espera
    while not (corrida / MARCA).is_file():
        if time.monotonic() > limite or termina():
            raise SystemExit(f"{corrida} no es una corrida de oh-duo-05 (falta {MARCA})")
        time.sleep(intervalo)
    O = importar("oh_duo_03_observador", oh_duo_03 / "observador.py")
    C = importar("oh_duo_04_coordinador", oh_duo_04 / "coordinador.py")
    E = O.cargar_detector(oh_duo_02)
    u, cfg = E.cargar_umbrales(None), C.cargar_config(None)
    obs, coo = salida / "observador", salida / "coordinador"
    tipos = D.TIPOS + (("advertencia",) if advertencias else ())
    desp = D.Despachador(corrida, oh_duo_04, salida / "despachos.jsonl", enviar, limite=u["limite_de_iteraciones"],
                         min_restantes=min_restantes, tipos=tipos)
    fin_obs, fin = threading.Event(), threading.Event()
    informes, coordinadores = {}, {}

    def observar():
        try:
            informes["observador"] = O.vigilar_corrida(corrida, obs, E, u, intervalo, maximo=maximo,
                                                       termina=lambda: termina() or fin.is_set())
        finally:
            fin_obs.set()

    def coordinar(nombre: str):
        try:
            informes[nombre] = C.vigilar(obs / nombre, coo / nombre, cfg, intervalo, termina=lambda: fin.is_set())
        except ValueError as e:
            # OH-DUO-04 lee supervision.json justo cuando OH-DUO-03 lo escribe (no es atómico): las decisiones ya
            # quedaron en decisiones.jsonl; solo falta su resumen. Se regenera con `coordinador.py simular`.
            informes[nombre] = {"error_resumen": repr(e)}
            print(f"   aviso: resumen del coordinador incompleto para {nombre}: {e!r}", flush=True)

    hilos = [threading.Thread(target=observar, daemon=True),
             threading.Thread(target=D.seguir, args=(coo, desp, intervalo / 2, fin.is_set), daemon=True)]
    for h in hilos:
        h.start()
    print(f"== cadena {'ENVIANDO al buzón' if enviar else 'SIN enviar (control)'} → {corrida}", flush=True)
    try:
        while not (fin_obs.is_set() and all(not h.is_alive() for h in coordinadores.values())):
            for d in sorted(obs.glob("*")) if obs.is_dir() else []:
                if d.is_dir() and d.name not in coordinadores:
                    coordinadores[d.name] = threading.Thread(target=coordinar, args=(d.name,), daemon=True)
                    coordinadores[d.name].start()
            if termina():
                break
            time.sleep(intervalo)
        time.sleep(intervalo * 2)          # el despachador lee las últimas decisiones
    finally:
        fin.set()
        for h in [*hilos, *coordinadores.values()]:
            h.join(10)
    return {"corrida": corrida.name, "enviar": enviar, "ejecuciones": sorted(coordinadores),
            "despachos": str(salida / "despachos.jsonl")}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--corrida", type=Path, required=True, help="oh-duo-05/corridas/<nombre>")
    p.add_argument("--enviar", action="store_true", help="escribir en el buzón (sin esto: control, nada se envía)")
    p.add_argument("--maximo", type=int, help="ejecuciones a observar antes de salir")
    p.add_argument("--intervalo", type=float, default=0.5)
    p.add_argument("--min-restantes", type=int, default=5)
    p.add_argument("--incluir-advertencias", action="store_true")
    p.add_argument("--salida", type=Path)
    a = p.parse_args(argv)
    corrida = a.corrida.resolve()
    salida = (a.salida or AQUI / "informes" / corrida.name).resolve()
    if salida == corrida or corrida in salida.parents:
        raise SystemExit("la salida no puede estar dentro de lo observado")
    try:
        r = correr(corrida, salida, hermano("oh-duo-02", "estancamiento.py"), hermano("oh-duo-03", "observador.py"),
                   hermano("oh-duo-04", "coordinador.py"), a.enviar, a.maximo, a.intervalo, a.min_restantes,
                   a.incluir_advertencias)
    except KeyboardInterrupt:
        print("== cadena detenida por el operador")
        return 0
    print(f"== listo: {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
