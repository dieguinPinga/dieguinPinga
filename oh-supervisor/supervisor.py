"""Supervisor mínimo de intentos para oh-smoke.

Ejecuta oh-smoke/correr.sh hasta N veces (3 por omisión), de a una, con semillas distintas. Se detiene en el
primer intento que cumple todos los criterios o cuando se agotan los intentos. No toca oh-smoke: solo lo
ejecuta y lee lo que dejó en resultados/. Su propio registro va en oh-supervisor/registros/<fecha>/.

Independencia de los intentos (ver README):
  - cada intento es un proceso correr.sh nuevo, en su propio grupo de procesos, que crea una carpeta nueva
    con una copia limpia de tarea/ y una conversación nueva de OpenHands;
  - el supervisor no le pasa nada al agente: solo la semilla, como argumento de correr.sh;
  - antes y después de cada intento compara las huellas de los originales (tarea/, ocultas/, control/,
    scripts) con las del inicio y verifica que la carpeta del intento sea nueva y haya partido de esos
    originales (huellas_inicio.txt).

Solo biblioteca estándar.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"
CRITERIOS = (
    "1_llamada_a_herramienta_valida",
    "2_corrio_pruebas_y_vio_fallo",
    "3_edito_codigo_y_no_las_pruebas",
    "4_visibles_pasan_y_termino_con_finish",
    "5_ocultas_pasan",
    "6_num_ctx_en_cada_pedido",
)
# Lo que no puede cambiar entre intentos. Las rutas son relativas a oh-smoke/.
PROTEGIDOS = ("tarea", "ocultas", "control", "smoke.py", "relay.py", "correr.sh",
              "requisitos.lock", "requisitos-tools.lock")
# Margen del supervisor por encima del peor caso de correr.sh (control previo + agente + evaluación).
# No es un límite del experimento: los de correr.sh no se tocan.
TIMEOUT_SUPERVISOR = 3600


class Integridad(Exception):
    """Algo que debía quedar intacto cambió: se detiene todo."""


def ahora() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def huellas(base: Path) -> dict[str, str]:
    """sha256 de cada archivo regular protegido (sin seguir enlaces). Un enlace o algo raro también cuenta."""
    salida = {}
    for nombre in PROTEGIDOS:
        p = base / nombre
        archivos = sorted(x for x in p.iterdir()) if p.is_dir() and not p.is_symlink() else [p]
        for a in archivos:
            if a.name == "__pycache__":
                continue
            rel = str(a.relative_to(base))
            try:
                fd = os.open(a, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError as e:
                salida[rel] = f"ilegible:{e.errno}"
                continue
            with os.fdopen(fd, "rb") as f:
                salida[rel] = (hashlib.sha256(f.read()).hexdigest()
                               if stat.S_ISREG(os.fstat(f.fileno()).st_mode) else "no-regular")
    return salida


def otro_correr_activo(propio: int | None = None) -> list[int]:
    """PIDs de cualquier correr.sh de oh-smoke en ejecución (también los lanzados a mano)."""
    pids = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) in (os.getpid(), propio):
            continue
        try:
            cmd = (d / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(c.endswith(b"correr.sh") for c in cmd[:3]):
            pids.append(int(d.name))
    return pids


def leer_json(p: Path) -> dict | None:
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            return json.loads(f.read(4 << 20))
    except (OSError, ValueError):
        return None


def clasificar(res: dict | None, salida_correr: int | None, salida_texto: str, timeout: bool) -> tuple[bool, str, list]:
    """(éxito, motivo principal, criterios que fallaron). El éxito exige los 6 criterios y originales intactos."""
    if timeout:
        return False, "timeout_supervisor", list(CRITERIOS)
    if res is None:
        if "control previo FALLÓ" in salida_texto:
            return False, "control_previo", list(CRITERIOS)
        if "el puente no arrancó" in salida_texto:
            return False, "puente", list(CRITERIOS)
        return False, f"sin_resultado (salida {salida_correr})", list(CRITERIOS)
    crit = res.get("criterios") or {}
    fallidos = [c for c in CRITERIOS if crit.get(c) is not True]
    if res.get("originales_intactos") != "ok":
        return False, "originales_alterados", fallidos
    if not fallidos:
        return True, "exito", []
    estado = res.get("estado_final")
    if res.get("nota"):
        motivo = "agente_sin_resultado"
    elif res.get("agente_exit") == "124":
        motivo = "corte_externo"
    elif res.get("corte_forzado"):
        motivo = "corte_forzado"
    elif res.get("vencio_tiempo"):
        motivo = "tiempo"
    elif estado == "stuck":
        motivo = "stuck"
    elif res.get("error"):
        motivo = "error_sdk"
    elif estado == "finished" and not res.get("termino_con_finish"):
        # El SDK da por terminada la tarea si el modelo responde texto sin <function=...> (p. ej. JSON).
        sin_herramienta = (res.get("detalle") or {}).get("mensajes_de_texto_del_agente")
        motivo = "texto_sin_herramienta" if sin_herramienta else "termino_sin_finish"
    else:
        motivo = f"criterios ({estado})"
    return False, motivo, fallidos


def carpeta_del_intento(texto: str, resultados: Path, antes: set[str], semilla: int) -> Path | None:
    """La carpeta que creó correr.sh: la que imprime, que tiene que ser nueva y de esta semilla."""
    m = re.findall(r"[Cc]arpeta: (\S+)", texto)
    nuevas = sorted(set(os.listdir(resultados)) - antes) if resultados.is_dir() else []
    candidata = Path(m[-1]) if m else (resultados / nuevas[-1] if len(nuevas) == 1 else None)
    if candidata is None:
        return None
    if candidata.parent.resolve() != resultados.resolve() or candidata.name not in nuevas:
        raise Integridad(f"la carpeta del intento no es nueva o está fuera de resultados/: {candidata}")
    if not candidata.name.endswith(f"-s{semilla}"):
        raise Integridad(f"la carpeta del intento no corresponde a la semilla {semilla}: {candidata}")
    return candidata


def partio_del_original(carpeta: Path, iniciales: dict[str, str]) -> bool:
    """huellas_inicio.txt (lo escribe correr.sh al copiar tarea/) coincide con los originales del supervisor."""
    try:
        lineas = (carpeta / "huellas_inicio.txt").read_text().splitlines()
    except OSError:
        return False
    vistas = dict(reversed(l.split(None, 1)) for l in lineas if l.strip())
    return all(vistas.get(k) == v for k, v in iniciales.items() if k.startswith("tarea/"))


def ejecutar(correr: Path, semilla: int, log: Path, timeout: int) -> tuple[int | None, str, bool]:
    """Corre correr.sh en su propio grupo de procesos; ante un timeout o Ctrl-C mata el grupo entero."""
    with open(log, "wb") as f:
        p = subprocess.Popen([str(correr), str(semilla)], cwd=correr.parent, stdout=f, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        vencido = False
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            vencido = True
        except KeyboardInterrupt:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(30)
            raise
        if vencido:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
    return (None if vencido else p.returncode), log.read_text(errors="replace"), vencido


def supervisar(oh_smoke: Path, semillas: list[int], registros: Path, timeout: int = TIMEOUT_SUPERVISOR) -> dict:
    if len(set(semillas)) != len(semillas) or not semillas:
        raise SystemExit("las semillas tienen que ser distintas y al menos una")
    correr, resultados = oh_smoke / "correr.sh", oh_smoke / "resultados"
    if not os.access(correr, os.X_OK):
        raise SystemExit(f"no se puede ejecutar {correr}")
    registros.mkdir(parents=True, exist_ok=True)

    # Un solo supervisor a la vez, y nunca junto a un correr.sh lanzado a mano.
    candado = open(registros.parent / ".candado", "w")
    try:
        fcntl.flock(candado, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("ya hay otro supervisor en ejecución")

    iniciales = huellas(oh_smoke)
    resumen = {
        "supervisor": VERSION, "oh_smoke": str(oh_smoke), "inicio": ahora(), "semillas": semillas,
        "max_intentos": len(semillas), "huellas_originales": iniciales, "intentos": [],
        "exito": False, "intento_exitoso": None, "motivo_detencion": None,
    }
    t0 = time.monotonic()
    try:
        for n, semilla in enumerate(semillas, 1):
            if otros := otro_correr_activo():
                raise Integridad(f"hay otro correr.sh en ejecución (pids {otros}); no se corre en paralelo")
            if huellas(oh_smoke) != iniciales:
                raise Integridad(f"los originales cambiaron antes del intento {n}")
            antes = set(os.listdir(resultados)) if resultados.is_dir() else set()
            inicio, ti = ahora(), time.monotonic()
            print(f"== Intento {n}/{len(semillas)} (semilla {semilla})", flush=True)
            salida, texto, vencido = ejecutar(correr, semilla, registros / f"intento-{n}.log", timeout)
            intento = {"intento": n, "semilla": semilla, "inicio": inicio, "fin": ahora(),
                       "segundos": round(time.monotonic() - ti, 1), "salida_correr": salida}
            resumen["intentos"].append(intento)
            carpeta = carpeta_del_intento(texto, resultados, antes, semilla)
            res = leer_json(carpeta / "resultado_final.json") if carpeta else None
            exito, motivo, fallidos = clasificar(res, salida, texto, vencido)
            intento.update({
                "carpeta": str(carpeta) if carpeta else None,
                "partio_del_original": partio_del_original(carpeta, iniciales) if carpeta else None,
                "exito": exito, "motivo": motivo, "criterios_fallidos": fallidos,
                "criterios": (res or {}).get("criterios"),
                "estado_final": (res or {}).get("estado_final"),
                "llamadas_llm": (res or {}).get("llamadas_llm"),
                "tokens_prompt": (res or {}).get("tokens_prompt"),
                "tokens_generados": (res or {}).get("tokens_generados"),
                "segundos_agente": (res or {}).get("segundos"),
                "acciones": (res or {}).get("acciones"),
            })
            if res is not None:
                (registros / f"intento-{n}.resultado_final.json").write_text(
                    json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
            falla = None
            if huellas(oh_smoke) != iniciales:
                falla = f"los originales cambiaron durante el intento {n}"
            elif carpeta and not intento["partio_del_original"]:
                falla = f"el intento {n} no partió de los originales (huellas_inicio.txt)"
            elif motivo == "originales_alterados":
                falla = f"correr.sh informó originales alterados en el intento {n}"
            if falla:   # un intento que compromete la integridad nunca cuenta como éxito
                intento.update(exito=False, motivo="integridad")
                raise Integridad(falla)
            print(f"   {'ÉXITO' if exito else 'falló'}: {motivo}"
                  + (f" ({', '.join(fallidos)})" if fallidos and not exito else "")
                  + f" — {intento['segundos']} s", flush=True)
            if exito:
                resumen.update(exito=True, intento_exitoso=n, motivo_detencion="exito")
                break
        else:
            resumen["motivo_detencion"] = "intentos_agotados"
    except Integridad as e:
        resumen["motivo_detencion"] = "integridad"
        resumen["detalle_detencion"] = str(e)
        print(f"   DETENIDO por integridad: {e}", flush=True)
    except KeyboardInterrupt:
        resumen["motivo_detencion"] = "interrumpido"
    finally:
        resumen["fin"] = ahora()
        resumen["segundos_total"] = round(time.monotonic() - t0, 1)
        resumen["intentos_realizados"] = len(resumen["intentos"])
        (registros / "resumen.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=1), encoding="utf-8")
        candado.close()
    return resumen


def main(argv: list[str] | None = None) -> int:
    aqui = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--oh-smoke", type=Path, default=Path.home() / "oh-smoke")
    p.add_argument("--semillas", default="11,12,13",
                   help="semillas distintas, separadas por coma; su cantidad es el máximo de intentos (≤ 3)")
    p.add_argument("--registros", type=Path, default=aqui / "registros")
    a = p.parse_args(argv)
    semillas = [int(s) for s in a.semillas.split(",") if s.strip()]
    if len(semillas) > 3:
        raise SystemExit("como máximo 3 intentos")
    destino = a.registros / datetime.now().strftime("%Y%m%d-%H%M%S")
    resumen = supervisar(a.oh_smoke.resolve(), semillas, destino)
    print(f"== {resumen['motivo_detencion']}: {resumen['intentos_realizados']} intento(s), "
          f"{resumen['segundos_total']} s. Resumen: {destino / 'resumen.json'}")
    return 0 if resumen["exito"] else 1


if __name__ == "__main__":
    sys.exit(main())
