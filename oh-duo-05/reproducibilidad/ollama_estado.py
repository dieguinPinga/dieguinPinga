"""Estado de Ollama para el experimento A1: comprobar que nadie lo usa, descargar SOLO nuestro modelo y
registrar el estado inicial. Solo biblioteca estándar. Nunca genera tokens.

  python3 ollama_estado.py foto       --url URL --salida F.json       estado actual (lectura)
  python3 ollama_estado.py inactivo   --url URL [--ventana 20]        ¿hay otra inferencia en curso? (lectura)
  python3 ollama_estado.py preparar   --url URL --salida F.json       inactivo → descarga qwen → verifica en frío
  python3 ollama_estado.py despues    --url URL --salida F.json       ¿alguien cargó otro modelo durante la corrida?

Señales de actividad (cualquiera aborta, salida 3):
  - hay cargado OTRO modelo que no es el del experimento: puede estar usándolo otro proceso → no se toca;
  - el `expires_at` de un modelo cargado cambia durante la ventana de observación: Ollama lo renueva con cada
    pedido, así que alguien lo está usando;
  - si Ollama corre en esta máquina: conexiones TCP establecidas con su puerto, o uso de la GPU por encima
    del umbral en las muestras de nvidia-smi;
  - hay un correr.sh o un agente de OH-DUO en ejecución.
Las señales locales (conexiones, GPU) describen el Ollama y la GPU DE ESTA MÁQUINA: se usan cuando la URL es
local. El Ollama simulado de las pruebas (`--simulado`, que exige /api/version = "0.0-simulado") no es ese
Ollama ni usa la GPU: con él se omiten, porque si no la prueba mediría el estado del Ollama real de la máquina.
Descarga: POST /api/generate {"model": M, "keep_alive": 0} sin prompt, que según la API de Ollama descarga el
modelo sin generar nada. Se envía solo si M está cargado y la observación no vio actividad.
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MODELO = "qwen2.5-coder:7b"
VERSION_SIMULADA = "0.0-simulado"      # la que informa tests/simulado.py
UMBRAL_GPU = 15          # % de uso de la GPU que se considera actividad


def ahora() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def pedir(url: str, ruta: str, cuerpo: dict | None = None, timeout: float = 10) -> dict:
    req = urllib.request.Request(url.rstrip("/") + ruta, data=json.dumps(cuerpo).encode() if cuerpo is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if cuerpo is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def cargados(url: str) -> list[dict]:
    return [{"name": m.get("name") or m.get("model"), "expires_at": m.get("expires_at"), "size": m.get("size"),
             "size_vram": m.get("size_vram"), "context_length": m.get("context_length")}
            for m in pedir(url, "/api/ps").get("models", [])]


def es_local(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        return True
    try:
        salida = subprocess.run(["ip", "-o", "addr"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return f" {host}/" in salida


def gpu() -> list[dict] | None:
    try:
        s = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.used,memory.total,utilization.gpu,"
                            "temperature.gpu,clocks.sm,power.draw", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if s.returncode != 0:
        return None
    claves = ("nombre", "driver", "memoria_usada_mib", "memoria_total_mib", "uso_pct", "temperatura_c", "reloj_sm_mhz", "potencia_w")
    return [dict(zip(claves, (x.strip() for x in l.split(",")))) for l in s.stdout.strip().splitlines()]


def conexiones(url: str) -> int | None:
    """Conexiones TCP establecidas con el puerto de Ollama en esta máquina (no hace falta ser root)."""
    puertos = {urllib.parse.urlparse(url).port or 11434, 11434}
    try:
        s = subprocess.run(["ss", "-Htn", "state", "established"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    n = 0
    for l in s.stdout.splitlines():
        partes = l.split()
        if len(partes) >= 4 and any(partes[-2].endswith(f":{p}") for p in puertos):
            n += 1                    # una por conexión: se cuenta el lado del servidor
    return n


def procesos_oh() -> list[str]:
    try:
        s = subprocess.run(["pgrep", "-af", r"/correr\.sh|/opt/oh/smoke\.py"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return [l for l in s.stdout.splitlines() if l.strip()]


def entorno_ollama() -> dict:
    """Variables del proceso ollama local que afectan la reproducibilidad (si se pueden leer)."""
    claves = ("OLLAMA_NUM_PARALLEL", "OLLAMA_MAX_LOADED_MODELS", "OLLAMA_FLASH_ATTENTION", "OLLAMA_KV_CACHE_TYPE",
              "OLLAMA_KEEP_ALIVE", "OLLAMA_CONTEXT_LENGTH", "OLLAMA_NEW_ENGINE", "OLLAMA_SCHED_SPREAD")
    out = {}
    try:
        pids = subprocess.run(["pgrep", "-x", "ollama"], capture_output=True, text=True, timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        pids = []
    for pid in pids[:1]:
        try:
            for kv in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
                k, _, v = kv.decode(errors="replace").partition("=")
                if k in claves:
                    out[k] = v
            out["_leido"] = True
        except OSError:
            out["_leido"] = False
    return out


def foto(url: str, senales_locales: bool | None = None) -> dict:
    """senales_locales=None: se miran la GPU y las conexiones si la URL es de esta máquina (lo de siempre).
    False: solo para el Ollama simulado, que lo tiene que declarar en /api/version."""
    local = es_local(url)
    version = pedir(url, "/api/version").get("version")
    if senales_locales is False and version != VERSION_SIMULADA:
        raise ValueError(f"las señales locales solo se pueden omitir con el Ollama simulado (versión {version!r})")
    mirar = local if senales_locales is None else senales_locales
    return {"momento": ahora(), "url": url, "version": version, "cargados": cargados(url),
            "ollama_local": local, "senales_locales": mirar, "gpu": gpu() if mirar else None,
            "conexiones": conexiones(url) if mirar else None, "entorno_ollama": entorno_ollama() if mirar else None,
            "procesos_oh": procesos_oh()}


def inactivo(url: str, modelo: str = MODELO, ventana: float = 20, muestras: int = 4, umbral_gpu: float = UMBRAL_GPU,
             dormir=time.sleep, senales_locales: bool | None = None) -> tuple[bool, list[str], list[dict]]:
    """Observa Ollama `ventana` segundos. Devuelve (inactivo, motivos de actividad, fotos)."""
    fotos, motivos = [], []
    for k in range(muestras):
        fotos.append(foto(url, senales_locales))
        if k < muestras - 1:
            dormir(ventana / (muestras - 1))
    for f in fotos:
        otros = sorted({m["name"] for m in f["cargados"] if m["name"] != modelo})
        if otros:
            motivos.append(f"hay otro modelo cargado ({', '.join(otros)}): puede estar usándolo otro proceso; no se toca")
            break
    vencimientos = {}
    for f in fotos:
        for m in f["cargados"]:
            vencimientos.setdefault(m["name"], set()).add(m["expires_at"])
    for nombre, v in vencimientos.items():
        if len(v) > 1:
            motivos.append(f"el vencimiento de {nombre} cambió durante la observación: alguien lo está usando")
    if any(f["procesos_oh"] for f in fotos):
        motivos.append(f"hay procesos de OH-DUO en ejecución: {fotos[-1]['procesos_oh'] or fotos[0]['procesos_oh']}")
    if fotos[0]["senales_locales"]:
        if any((f["conexiones"] or 0) > 0 for f in fotos):
            motivos.append(f"conexiones abiertas con Ollama: {[f['conexiones'] for f in fotos]}")
        usos = [float(g["uso_pct"]) for f in fotos for g in (f["gpu"] or []) if g.get("uso_pct", "").replace(".", "").isdigit()]
        if any(x > umbral_gpu for x in usos):
            motivos.append(f"la GPU está en uso: {usos} % (umbral {umbral_gpu} %)")
    return not motivos, motivos, fotos


def descargar(url: str, modelo: str = MODELO, espera: float = 60, dormir=time.sleep) -> dict:
    """Descarga SOLO `modelo`, y solo si está cargado. Espera a que /api/ps deje de mostrarlo."""
    if modelo not in {m["name"] for m in cargados(url)}:
        return {"enviado": False, "motivo": "no estaba cargado"}
    pedir(url, "/api/generate", {"model": modelo, "keep_alive": 0}, timeout=60)     # sin prompt: no genera
    limite = time.monotonic() + espera
    while modelo in {m["name"] for m in cargados(url)}:
        if time.monotonic() > limite:
            raise RuntimeError(f"{modelo} sigue cargado {espera} s después de pedir la descarga")
        dormir(1)
    return {"enviado": True, "motivo": "descargado"}


def preparar(url: str, modelo: str = MODELO, ventana: float = 20, dormir=time.sleep,
             senales_locales: bool | None = None) -> dict:
    ok, motivos, fotos = inactivo(url, modelo, ventana, dormir=dormir, senales_locales=senales_locales)
    r = {"momento": ahora(), "observacion": fotos, "actividad": motivos}
    if not ok:
        r["resultado"] = "abortado"
        return r
    r["descarga"] = descargar(url, modelo, dormir=dormir)
    r["inicial"] = foto(url, senales_locales)
    if r["inicial"]["cargados"]:
        r["resultado"] = "abortado"
        r["actividad"].append(f"después de descargar quedan modelos cargados: {[m['name'] for m in r['inicial']['cargados']]}")
    else:
        r["resultado"] = "en_frio"
    return r


def despues(url: str, modelo: str = MODELO) -> dict:
    f = foto(url)
    otros = sorted({m["name"] for m in f["cargados"] if m["name"] != modelo})
    return {"final": f, "otros_modelos_aparecieron": otros, "contaminada": bool(otros)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("modo", choices=("foto", "inactivo", "preparar", "despues"))
    ap.add_argument("--url", required=True)
    ap.add_argument("--modelo", default=MODELO)
    ap.add_argument("--ventana", type=float, default=20)
    ap.add_argument("--salida", type=Path)
    ap.add_argument("--simulado", action="store_true",
                    help="Ollama simulado de las pruebas: sin señales locales (exige su versión 0.0-simulado)")
    a = ap.parse_args(argv)
    sl = False if a.simulado else None
    try:
        if a.modo == "foto":
            r, codigo = foto(a.url), 0
        elif a.modo == "inactivo":
            ok, motivos, fotos = inactivo(a.url, a.modelo, a.ventana, senales_locales=sl)
            r, codigo = {"inactivo": ok, "actividad": motivos, "observacion": fotos}, 0 if ok else 3
        elif a.modo == "preparar":
            r = preparar(a.url, a.modelo, a.ventana, senales_locales=sl)
            codigo = 0 if r["resultado"] == "en_frio" else 3
        else:
            r = despues(a.url, a.modelo)
            codigo = 4 if r["contaminada"] else 0
    except (OSError, ValueError, RuntimeError) as e:
        r, codigo = {"error": repr(e)}, 2
    texto = json.dumps(r, ensure_ascii=False, indent=1)
    if a.salida:
        a.salida.parent.mkdir(parents=True, exist_ok=True)
        a.salida.write_text(texto)
    for m in r.get("actividad") or []:
        print(f"  actividad: {m}")
    if "error" in r:
        print(f"  error: {r['error']}")
    print(f"  {a.modo}: {'OK' if codigo == 0 else 'NO'}" + (f" ({r.get('resultado')})" if "resultado" in r else ""))
    return codigo


if __name__ == "__main__":
    sys.exit(main())
