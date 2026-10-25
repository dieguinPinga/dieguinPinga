"""OH-DUO-01: los mismos problemas, dos programadores (Qwen corrección mínima, Gemma invariantes).

Tres comandos, en este orden, como procesos separados:

  ejecutar   corre cada (problema, modelo) UNA vez, de a una, desde una copia limpia, y congela el resultado
             (archivos finales + manifiesto sha256 + registros). No lee las pruebas ocultas: las instancias no
             las contienen y el ejecutor solo evalúa las visibles.
  verificar  proceso verificador: exige que todo esté congelado, sella la lista de manifiestos, corre visibles
             y ocultas sobre los archivos congelados en un sandbox sin red y no escribe nada que vean los
             agentes. Después de verificar, la corrida ya no admite ejecuciones.
  informe    clasifica cada ejecución y escribe resultados.json, matriz.csv e informe.md.

No modifica oh-smoke, oh-supervisor ni oh-lab. Reutiliza el venv y relay.py de oh-smoke, y de oh-supervisor
las funciones de ejecución en grupo de procesos y de detección de otro correr.sh. Solo biblioteca estándar.
"""
from __future__ import annotations

import argparse
import ast
import csv
import difflib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"
AQUI = Path(__file__).resolve().parent
MODELOS = {
    "qwen": {"modelo": "qwen2.5-coder:7b", "rol": "qwen_minimo.txt", "enfoque": "corrección mínima"},
    "gemma": {"modelo": "gemma4:12b", "rol": "gemma_invariantes.txt", "enfoque": "razonamiento por invariantes"},
}
CATEGORIAS = ("correcta", "incorrecta", "potencialmente_correcta_sin_finalizar", "incompleta",
              "error_infraestructura")
# Lo que se copia de cada ejecución (nada de esto contiene pruebas ocultas: la instancia no las tiene).
REGISTROS = ("estado/eventos.json", "estado/resultado.json", "resultado_final.json", "agente.log", "agente.exit",
             "puente.jsonl", "eval.visibles.txt", "eval.visibles.exit", "cambios.diff", "control/sandbox.txt",
             "control/original.visibles.txt", "api_ps.json", "huellas_inicio.txt", "originales_intactos")
MAX_ARCHIVO = 1 << 20
TIMEOUT_EJECUCION = 3600   # seguridad del orquestador; los límites del experimento son los de correr.sh


def ahora() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def leer_regular(p: Path, maximo: int = MAX_ARCHIVO) -> bytes | None:
    """Contenido de un archivo regular, sin seguir enlaces; None si no lo es."""
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            return None
        return f.read(maximo + 1)[:maximo]


def leer_json(p: Path):
    datos = leer_regular(p, 16 << 20)
    try:
        return json.loads(datos) if datos is not None else None
    except ValueError:
        return None


@contextmanager
def candado(ruta: Path):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    f = open(ruta, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise SystemExit(f"otro proceso de oh-duo-01 tiene el candado {ruta}")
    try:
        yield
    finally:
        f.close()


# ----------------------------------------------------------------------------------------------- banco

def cargar_banco(banco: Path) -> list[dict]:
    """Valida la estructura. No lee el contenido de ocultas/ ni de referencia/ (solo que existan)."""
    problemas = []
    for d in sorted(p for p in banco.iterdir() if p.is_dir()):
        p = json.loads((d / "problema.json").read_text(encoding="utf-8"))
        mod = p.get("modulo", "")
        if p.get("id") != d.name or not re.fullmatch(r"[a-z_][a-z0-9_]*", mod):
            raise SystemExit(f"{d.name}: id o módulo inválidos")
        tarea = sorted(x.name for x in (d / "tarea").iterdir())
        if tarea != sorted([f"{mod}.py", f"test_{mod}.py"]):
            raise SystemExit(f"{d.name}: tarea/ debe tener exactamente {mod}.py y test_{mod}.py")
        for requerido in (d / "ocultas" / "test_ocultas.py", d / "referencia" / f"{mod}.py"):
            if not requerido.is_file():
                raise SystemExit(f"{d.name}: falta {requerido.relative_to(d)}")
        problemas.append({"id": p["id"], "modulo": mod, "dir": d})
    if not problemas:
        raise SystemExit("banco vacío")
    return problemas


def huella_tareas(problemas: list[dict]) -> str:
    """Huella de lo que reciben los agentes (tarea/ de cada problema)."""
    h = hashlib.sha256()
    for p in problemas:
        for f in sorted((p["dir"] / "tarea").iterdir()):
            h.update(f"{p['id']}/{f.name}".encode() + b"\0" + f.read_bytes())
    return h.hexdigest()


def huella_ocultas(problemas: list[dict]) -> str:
    """La calcula solo el verificador."""
    return sha(b"".join((p["dir"] / "ocultas" / "test_ocultas.py").read_bytes() for p in problemas))


# ------------------------------------------------------------------------------------------- ejecución

def cargar_supervisor(oh_supervisor: Path):
    spec = importlib.util.spec_from_file_location("oh_supervisor_supervisor", oh_supervisor / "supervisor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def plan(problemas: list[dict], modelos: list[str], orden: str) -> list[tuple[dict, str]]:
    """alterno: P1 qwen→gemma, P2 gemma→qwen, ... (contrapesa el orden); bloque: todos con uno, luego el otro."""
    if orden == "bloque":
        return [(p, m) for m in modelos for p in problemas]
    return [(p, m) for i, p in enumerate(problemas) for m in (modelos if i % 2 == 0 else modelos[::-1])]


def armar_instancia(p: dict, clave: str, destino: Path, oh_smoke: Path, ejecutor: Path, roles: Path) -> None:
    """Solo tarea/ del problema. Sin ocultas, sin referencia, sin descripción del defecto."""
    destino.mkdir(parents=True)
    shutil.copytree(p["dir"] / "tarea", destino / "tarea", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ejecutor / "correr.sh", destino / "correr.sh")
    shutil.copy2(ejecutor / "agente.py", destino / "smoke.py")
    shutil.copy2(roles / MODELOS[clave]["rol"], destino / "enfoque.txt")
    for f in ("relay.py", "requisitos.lock", "requisitos-tools.lock"):
        if (oh_smoke / f).is_file():
            shutil.copy2(oh_smoke / f, destino / f)
    (destino / "venv").symlink_to(oh_smoke / "venv")
    (destino / "mision.json").write_text(json.dumps({"id": p["id"], "modulo": p["modulo"]}) + "\n")
    (destino / "resultados").mkdir()
    os.chmod(destino / "correr.sh", 0o755)


def modelos_cargados(url: str) -> list[str] | None:
    """GET /api/ps (solo lectura) para saber qué modelo había cargado antes de cada ejecución."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/ps", timeout=3) as r:
            return [m.get("name") for m in json.loads(r.read()).get("models", [])]
    except (OSError, ValueError):
        return None


def congelar(corrida_dir: Path, destino: Path) -> dict:
    """Copia los archivos regulares de work/ (sin seguir enlaces) y los registros, con manifiesto sha256."""
    final, logs = destino / "final", destino / "logs"
    final.mkdir(parents=True)
    logs.mkdir()
    manifiesto, omitidos = {}, []
    work = corrida_dir / "work"
    for p in sorted(work.rglob("*")) if work.is_dir() else []:
        rel = p.relative_to(work)
        if "__pycache__" in rel.parts or p.is_dir():
            continue
        datos = leer_regular(p)
        if datos is None:
            omitidos.append(str(rel))       # enlaces, dispositivos, etc.: no se siguen
            continue
        (final / rel).parent.mkdir(parents=True, exist_ok=True)
        (final / rel).write_bytes(datos)
        manifiesto[str(rel)] = sha(datos)
    for nombre in REGISTROS:
        datos = leer_regular(corrida_dir / nombre, 16 << 20)
        if datos is not None:
            (logs / nombre.replace("/", "__")).write_bytes(datos)
    (destino / "manifiesto.json").write_text(json.dumps({"archivos": manifiesto, "omitidos": omitidos}, indent=1))
    for p in [*final.rglob("*"), *logs.iterdir(), destino / "manifiesto.json"]:
        if p.is_file():
            os.chmod(p, 0o444)
    return {"archivos": len(manifiesto), "omitidos": omitidos}


def estado_ejecucion(res: dict | None, salida: int | None, texto: str, vencido: bool) -> tuple[str, str]:
    """(finalizada | incompleta | error_infraestructura, motivo). Solo con datos visibles de la ejecución."""
    if vencido:
        return "error_infraestructura", "timeout_del_orquestador"
    if res is None:
        if "control previo FALLÓ" in texto:
            return "error_infraestructura", "control_previo"
        if "el puente no arrancó" in texto:
            return "error_infraestructura", "puente"
        return "error_infraestructura", f"sin_resultado (salida {salida})"
    if res.get("nota"):
        return "error_infraestructura", "agente_sin_resultado"
    if res.get("error"):
        return "error_infraestructura", f"error_sdk: {str(res['error'])[:200]}"
    if res.get("estado_final") == "error":
        return "error_infraestructura", "estado_error_del_sdk"
    if res.get("agente_exit") == "124":
        return "incompleta", "corte_externo"
    if res.get("estado_final") == "finished" and res.get("termino_con_finish"):
        return "finalizada", "finish"
    if res.get("corte_forzado"):
        return "incompleta", "corte_forzado"
    if res.get("vencio_tiempo"):
        return "incompleta", "tiempo"
    if res.get("estado_final") == "stuck":
        return "incompleta", "stuck"
    if (res.get("detalle") or {}).get("mensajes_de_texto_del_agente"):
        return "incompleta", "texto_sin_herramienta"
    return "incompleta", f"sin_finish ({res.get('estado_final')})"


def ejecutar(corridas: Path, nombre: str, oh_smoke: Path, oh_supervisor: Path, problemas_ids: list[str] | None,
             modelos: list[str], semilla: int, orden: str, banco: Path = AQUI / "banco",
             ejecutor: Path = AQUI / "ejecutor", roles: Path = AQUI / "roles",
             timeout: int = TIMEOUT_EJECUCION) -> list[dict]:
    sup = cargar_supervisor(oh_supervisor)
    problemas = cargar_banco(banco)
    if problemas_ids:
        if faltan := sorted(set(problemas_ids) - {p["id"] for p in problemas}):
            raise SystemExit(f"problemas desconocidos: {faltan}")
        problemas = [p for p in problemas if p["id"] in problemas_ids]
    if faltan := sorted(set(modelos) - set(MODELOS)):
        raise SystemExit(f"modelos desconocidos: {faltan}")
    if not (oh_smoke / "venv").is_dir():
        raise SystemExit(f"no existe {oh_smoke / 'venv'}")
    c = corridas / nombre
    with candado(corridas / ".candado"):
        if (c / "congelado.json").exists() or (c / "verificacion").exists():
            raise SystemExit("esta corrida ya fue verificada: no admite más ejecuciones")
        c.mkdir(parents=True, exist_ok=True)
        config = {
            "experimento": "OH-DUO-01", "duo": VERSION, "semilla": semilla, "orden": orden,
            "modelos": {k: {**MODELOS[k], "sha256_rol": sha((roles / MODELOS[k]["rol"]).read_bytes())}
                        for k in MODELOS},
            "limites": {k: os.environ.get(k) for k in ("MAX_SEG", "MAX_ITER", "GRACIA", "NUM_CTX")},
            "ollama": os.environ.get("OLLAMA_URL", "http://100.92.153.59:11435"),
            "huellas": {"tareas": huella_tareas(cargar_banco(banco)),
                        "correr": sha((ejecutor / "correr.sh").read_bytes()),
                        "agente": sha((ejecutor / "agente.py").read_bytes()),
                        "supervisor": sha((oh_supervisor / "supervisor.py").read_bytes()),
                        "relay": sha((oh_smoke / "relay.py").read_bytes()) if (oh_smoke / "relay.py").is_file() else None},
        }
        previo = leer_json(c / "config.json")
        if previo is not None and previo != config:
            raise SystemExit("la configuración no coincide con la de las ejecuciones ya hechas en esta corrida")
        (c / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=1))

        hechas = []
        for p, clave in plan(problemas, modelos, orden):
            e = c / "ejecuciones" / f"{p['id']}__{clave}"
            if (e / "ejecucion.json").exists():
                print(f"== {p['id']} {clave}: ya ejecutada, se omite", flush=True)
                continue
            if (e / "instancia").exists():   # una ejecución anterior se cortó: se conserva aparte
                (e / "instancia").rename(e / f"instancia.abortada-{int(time.time())}")
            if otros := sup.otro_correr_activo():
                raise SystemExit(f"hay otro correr.sh en ejecución (pids {otros}); no se corre en paralelo")
            armar_instancia(p, clave, e / "instancia", oh_smoke, ejecutor, roles)
            antes = modelos_cargados(config["ollama"])
            inicio, t0 = ahora(), time.monotonic()
            print(f"== {p['id']} {clave} ({MODELOS[clave]['modelo']}, semilla {semilla})", flush=True)
            anterior = os.environ.get("MODELO")
            os.environ["MODELO"] = MODELOS[clave]["modelo"]
            try:
                salida, texto, vencido = sup.ejecutar(e / "instancia" / "correr.sh", semilla, e / "correr.log", timeout)
            finally:
                if anterior is None:
                    os.environ.pop("MODELO", None)
                else:
                    os.environ["MODELO"] = anterior
            segundos = round(time.monotonic() - t0, 1)
            carpetas = sorted((e / "instancia" / "resultados").iterdir())
            corrida_dir = carpetas[-1] if len(carpetas) == 1 else None
            congelado = congelar(corrida_dir, e) if corrida_dir else congelar(e / "instancia" / "vacio", e)
            res = leer_json(corrida_dir / "resultado_final.json") if corrida_dir else None
            estado, motivo = estado_ejecucion(res, salida, texto, vencido)
            visibles = leer_regular(corrida_dir / "eval.visibles.exit") if corrida_dir else None
            reg = {
                "problema": p["id"], "modulo": p["modulo"], "programador": clave, "modelo": MODELOS[clave]["modelo"],
                "enfoque": MODELOS[clave]["enfoque"], "semilla": semilla, "inicio": inicio, "fin": ahora(),
                "segundos": segundos, "salida_correr": salida, "estado_ejecucion": estado, "motivo": motivo,
                "visibles_pasan": visibles is not None and visibles.strip() == b"0",
                "modelos_cargados_antes": antes,
                "cambio_de_modelo": None if antes is None else MODELOS[clave]["modelo"] not in antes,
                "llamadas_llm": (res or {}).get("llamadas_llm"), "tokens_prompt": (res or {}).get("tokens_prompt"),
                "tokens_generados": (res or {}).get("tokens_generados"),
                "segundos_agente": (res or {}).get("segundos"), "acciones": (res or {}).get("acciones"),
                "estado_final_sdk": (res or {}).get("estado_final"),
                "originales_intactos": (res or {}).get("originales_intactos"), "congelado": congelado,
            }
            (e / "ejecucion.json").write_text(json.dumps(reg, ensure_ascii=False, indent=1))
            hechas.append(reg)
            print(f"   {estado} ({motivo}), visibles={'sí' if reg['visibles_pasan'] else 'no'}, {segundos} s", flush=True)
        return hechas


# ----------------------------------------------------------------------------------------- verificación

def literales_ocultos(problema: dict) -> set[str]:
    """Textos de las ocultas que no aparecen en lo que reciben los agentes: nombres de prueba y strings."""
    arbol = ast.parse((problema["dir"] / "ocultas" / "test_ocultas.py").read_text(encoding="utf-8"))
    candidatos = {n.name for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}
    candidatos |= {n.value for n in ast.walk(arbol) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and len(n.value.strip()) >= 4 and "\n" not in n.value}
    visible = "".join(f.read_text(encoding="utf-8") for f in (problema["dir"] / "tarea").iterdir())
    visible += "".join((AQUI / "roles" / m["rol"]).read_text(encoding="utf-8") for m in MODELOS.values())
    return {t for t in candidatos if t not in visible and not t.startswith("/work")}


def correr_pruebas(dir_eval: Path, ocultas: Path | None, prueba: str, bwrap: str | None) -> tuple[int, str]:
    """Visibles (ocultas=None) u ocultas, en un sandbox sin red con el código montado en /work en solo lectura."""
    if ocultas is None:
        args = ["-m", "unittest", "discover", "-s", "/work" if bwrap else str(dir_eval), "-p", prueba, "-v"]
    else:
        args = ["-c", "import sys, unittest; sys.path.insert(0, sys.argv[1]); unittest.main(module=None, "
                      "argv=['x', 'discover', '-s', sys.argv[2], '-p', 'test_*.py', '-v'])",
                "/work" if bwrap else str(dir_eval), "/ocultas" if bwrap else str(ocultas)]
    cmd = ["/usr/bin/python3.12" if bwrap else sys.executable, "-I", "-B", *args]
    if bwrap:
        cmd = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv", "--cap-drop", "ALL",
               "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
               "--symlink", "usr/lib64", "/lib64", "--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache",
               "--ro-bind-try", "/etc/alternatives", "/etc/alternatives", "--proc", "/proc", "--dev", "/dev",
               "--tmpfs", "/tmp", "--ro-bind", str(dir_eval), "/work",
               *(["--ro-bind", str(ocultas), "/ocultas"] if ocultas else []),
               "--chdir", "/tmp", "--setenv", "PATH", "/usr/bin", "--remount-ro", "/", *cmd]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout + r.stderr)[-20000:]
    except subprocess.TimeoutExpired:
        return 124, "tiempo agotado (60 s)"


def verificar(corridas: Path, nombre: str, banco: Path = AQUI / "banco", bwrap: str | None = "bwrap") -> dict:
    c = corridas / nombre
    problemas = {p["id"]: p for p in cargar_banco(banco)}
    with candado(corridas / ".candado"):
        config = leer_json(c / "config.json")
        if config is None:
            raise SystemExit("la corrida no tiene config.json")
        if config["huellas"]["tareas"] != huella_tareas(list(problemas.values())):
            raise SystemExit("el banco cambió desde que se ejecutó la corrida")
        ejecs = sorted(p for p in (c / "ejecuciones").iterdir() if p.is_dir())
        sin_congelar = [e.name for e in ejecs if not (e / "ejecucion.json").exists()
                        or not (e / "manifiesto.json").exists()]
        if sin_congelar or not ejecs:
            raise SystemExit(f"hay ejecuciones sin congelar: {sin_congelar or 'ninguna ejecución'}")
        manifiestos = {}
        for e in ejecs:
            m = json.loads((e / "manifiesto.json").read_text())
            for rel, h in m["archivos"].items():
                if sha((e / "final" / rel).read_bytes()) != h:
                    raise SystemExit(f"{e.name}: {rel} no coincide con su manifiesto")
            manifiestos[e.name] = sha((e / "manifiesto.json").read_bytes())
        if not (c / "congelado.json").exists():
            (c / "congelado.json").write_text(json.dumps(
                {"sellado": ahora(), "manifiestos": manifiestos, "huella_ocultas": huella_ocultas(list(problemas.values()))},
                indent=1))
        sellado = json.loads((c / "congelado.json").read_text())
        if sellado["manifiestos"] != manifiestos:
            raise SystemExit("los manifiestos cambiaron después del sellado")

        ver = c / "verificacion"
        ver.mkdir(exist_ok=True)
        resultados = {}
        for e in ejecs:
            reg = json.loads((e / "ejecucion.json").read_text())
            p = problemas[reg["problema"]]
            codigo, prueba = f"{p['modulo']}.py", f"test_{p['modulo']}.py"
            datos = leer_regular(e / "final" / codigo)
            r = {"codigo_presente": datos is not None}
            with tempfile.TemporaryDirectory(prefix="duo-ver-") as t:
                if datos is not None:
                    Path(t, codigo).write_bytes(datos)
                    shutil.copy(p["dir"] / "tarea" / prueba, Path(t, prueba))
                    os.chmod(t, 0o755)
                    r["visibles_exit"], vis = correr_pruebas(Path(t), None, prueba, bwrap)
                    r["ocultas_exit"], ocu = correr_pruebas(Path(t), p["dir"] / "ocultas", prueba, bwrap)
                    original = (p["dir"] / "tarea" / codigo).read_text(encoding="utf-8").splitlines()
                    nuevo = datos.decode(errors="replace").splitlines()
                    r["lineas_cambiadas"] = sum(1 for l in difflib.unified_diff(original, nuevo, lineterm="")
                                                if l[:1] in "+-" and l[:3] not in ("+++", "---"))
                else:
                    vis = ocu = "sin código"
                    r.update(visibles_exit=None, ocultas_exit=None, lineas_cambiadas=None)
            r["importo"] = bool(re.search(r"^Ran [1-9]", ocu, re.M)) and "ImportError" not in ocu
            r["pasa"] = r["visibles_exit"] == 0 and r["ocultas_exit"] == 0
            (ver / f"{e.name}.visibles.txt").write_text(vis)
            (ver / f"{e.name}.ocultas.txt").write_text(ocu)
            resultados[e.name] = r
        # Auditoría: ningún texto propio de las ocultas en lo que produjeron o registraron los agentes.
        fugas = []
        for e in ejecs:
            reg = json.loads((e / "ejecucion.json").read_text())
            literales = literales_ocultos(problemas[reg["problema"]])
            for f in [*(e / "final").rglob("*"), *(e / "logs").iterdir(), e / "correr.log"]:
                if f.is_file():
                    texto = f.read_bytes().decode(errors="replace")
                    fugas += [{"ejecucion": e.name, "archivo": str(f.relative_to(e)), "literal": lit}
                              for lit in literales if lit in texto]
        verif = {"verificado": ahora(), "resultados": resultados, "auditoria_fugas": fugas,
                 "sandbox": bool(bwrap)}
        (ver / "verificacion.json").write_text(json.dumps(verif, ensure_ascii=False, indent=1))
    return informe(corridas, nombre)


# --------------------------------------------------------------------------------------------- informe

def categoria(estado: str, pasa: bool) -> str:
    if estado == "finalizada":
        return "correcta" if pasa else "incorrecta"
    if pasa:
        return "potencialmente_correcta_sin_finalizar"
    return "incompleta" if estado == "incompleta" else "error_infraestructura"


def _suma(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(xs), 1) if xs else 0


def informe(corridas: Path, nombre: str) -> dict:
    c = corridas / nombre
    verif = leer_json(c / "verificacion" / "verificacion.json")
    if verif is None:
        raise SystemExit("falta verificar la corrida")
    config = json.loads((c / "config.json").read_text())
    filas = []
    for e in sorted(p for p in (c / "ejecuciones").iterdir() if p.is_dir()):
        reg = json.loads((e / "ejecucion.json").read_text())
        v = verif["resultados"][e.name]
        filas.append({**reg, **{f"verif_{k}": x for k, x in v.items()},
                      "categoria": categoria(reg["estado_ejecucion"], v["pasa"]),
                      "archivos_finales": str((e / "final").relative_to(c)), "registros": str((e / "logs").relative_to(c))})
    problemas = sorted({f["problema"] for f in filas})
    claves = [k for k in MODELOS if any(f["programador"] == k for f in filas)]
    cat = {(f["problema"], f["programador"]): f["categoria"] for f in filas}
    resuelve = {k: {p for p in problemas if cat.get((p, k)) == "correcta"} for k in claves}
    potencial = {k: {p for p in problemas if cat.get((p, k)) in ("correcta", "potencialmente_correcta_sin_finalizar")}
                 for k in claves}
    por_modelo = {}
    for k in claves:
        fs = [f for f in filas if f["programador"] == k]
        por_modelo[k] = {
            "modelo": MODELOS[k]["modelo"], "enfoque": MODELOS[k]["enfoque"], "ejecuciones": len(fs),
            **{c_: sum(f["categoria"] == c_ for f in fs) for c_ in CATEGORIAS},
            "tasa_resolucion": round(len(resuelve[k]) / len(fs), 3) if fs else None,
            "segundos_total": _suma(f["segundos"] for f in fs),
            "segundos_medio": round(_suma(f["segundos"] for f in fs) / len(fs), 1) if fs else None,
            "llamadas_llm": _suma(f["llamadas_llm"] for f in fs), "tokens_prompt": _suma(f["tokens_prompt"] for f in fs),
            "tokens_generados": _suma(f["tokens_generados"] for f in fs),
            "cambios_de_modelo": sum(1 for f in fs if f["cambio_de_modelo"]),
            "cambios_de_modelo_desconocidos": sum(1 for f in fs if f["cambio_de_modelo"] is None),
        }
    a, b = (claves + [None, None])[:2]
    comparacion = None
    if a and b:
        comparacion = {
            "resueltos_por_ambos": sorted(resuelve[a] & resuelve[b]),
            f"solo_{a}": sorted(resuelve[a] - resuelve[b]), f"solo_{b}": sorted(resuelve[b] - resuelve[a]),
            "no_resueltos": sorted(set(problemas) - resuelve[a] - resuelve[b]),
            "cobertura_conjunta_teorica": {
                "nota": "Oráculo retrospectivo: cuenta un problema si CUALQUIERA de los dos lo resolvió, sabiendo "
                        "las ocultas. No es una capacidad del sistema ni una estrategia de selección.",
                "problemas": sorted(resuelve[a] | resuelve[b]),
                "fraccion": round(len(resuelve[a] | resuelve[b]) / len(problemas), 3),
                "incluyendo_potencialmente_correctas": round(len(potencial[a] | potencial[b]) / len(problemas), 3)},
        }
    datos = {"experimento": "OH-DUO-01", "corrida": nombre, "generado": ahora(), "config": config,
             "verificado": verif["verificado"], "sandbox_verificador": verif["sandbox"],
             "auditoria_fugas": verif["auditoria_fugas"], "por_modelo": por_modelo, "comparacion": comparacion,
             "matriz": {p: {k: cat.get((p, k)) for k in claves} for p in problemas}, "ejecuciones": filas}
    (c / "resultados.json").write_text(json.dumps(datos, ensure_ascii=False, indent=1))

    columnas = ["problema", "programador", "modelo", "categoria", "estado_ejecucion", "motivo", "visibles_pasan",
                "verif_visibles_exit", "verif_ocultas_exit", "verif_lineas_cambiadas", "segundos", "segundos_agente",
                "llamadas_llm", "tokens_prompt", "tokens_generados", "cambio_de_modelo", "archivos_finales", "registros"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columnas, extrasaction="ignore")
    w.writeheader()
    w.writerows(sorted(filas, key=lambda f: (f["problema"], f["programador"])))
    (c / "matriz.csv").write_text(buf.getvalue())
    (c / "informe.md").write_text(redactar(datos, claves, problemas, cat))
    return datos


def redactar(d: dict, claves: list[str], problemas: list[str], cat: dict) -> str:
    ls = ["# OH-DUO-01: informe", "",
          f"Corrida `{d['corrida']}`, verificada el {d['verificado']}. Semilla {d['config']['semilla']}, "
          f"orden {d['config']['orden']}, una ejecución por problema y modelo, con los mismos límites.", "",
          "## Matriz", "", "| Problema | " + " | ".join(f"{k} ({MODELOS[k]['enfoque']})" for k in claves) + " |",
          "|---|" + "---|" * len(claves)]
    ls += [f"| {p} | " + " | ".join(cat.get((p, k)) or "—" for k in claves) + " |" for p in problemas]
    ls += ["", "## Por modelo", "", "| | " + " | ".join(claves) + " |", "|---|" + "---|" * len(claves)]
    for campo in ("ejecuciones", *CATEGORIAS, "tasa_resolucion", "segundos_total", "segundos_medio", "llamadas_llm",
                  "tokens_prompt", "tokens_generados", "cambios_de_modelo", "cambios_de_modelo_desconocidos"):
        ls.append(f"| {campo} | " + " | ".join(str(d["por_modelo"][k][campo]) for k in claves) + " |")
    if d["comparacion"]:
        cmp_ = d["comparacion"]
        ls += ["", "## Comparación (solo soluciones «correcta»)", ""]
        ls += [f"- **{k.replace('_', ' ')}:** {', '.join(v) or 'ninguno'}" for k, v in cmp_.items() if isinstance(v, list)]
        cc = cmp_["cobertura_conjunta_teorica"]
        ls += [f"- **Cobertura conjunta teórica:** {cc['fraccion']:.0%} ({', '.join(cc['problemas']) or 'ninguno'}); "
               f"con las potencialmente correctas: {cc['incluyendo_potencialmente_correctas']:.0%}.",
               f"  > {cc['nota']}"]
    ls += ["", "## Auditoría", "",
           f"- Fugas de textos de las pruebas ocultas en archivos o registros de los agentes: "
           f"**{len(d['auditoria_fugas'])}**.",
           f"- Verificador en sandbox sin red: {'sí' if d['sandbox_verificador'] else 'NO (solo pruebas)'}.", "",
           "## Cómo leer esto", "",
           "- Una ejecución por celda y seis problemas: los números describen esta corrida, no estiman la capacidad "
           "de cada modelo. No alcanzan para afirmar que un modelo o un enfoque es mejor.",
           "- Cada programador combina un modelo y un enfoque: una diferencia no se puede atribuir a uno solo de "
           "los dos (para eso está previsto el diseño 2×2).",
           "- Problemas, pruebas y textos de rol los escribió Claude: es un banco exploratorio, no de confirmación.",
           "- «correcta» exige terminar con finish y aprobar visibles y ocultas. «potencialmente correcta sin "
           "finalizar» aprueba las dos pero el agente no terminó (tiempo, bucle, error del SDK)."]
    return "\n".join(ls) + "\n"


# ------------------------------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--corridas", type=Path, default=AQUI / "corridas")
    sub = p.add_subparsers(dest="comando", required=True)
    e = sub.add_parser("ejecutar")
    e.add_argument("--corrida", required=True)
    e.add_argument("--problemas", help="ids separados por coma (por omisión, todos)")
    e.add_argument("--modelos", default="qwen,gemma")
    e.add_argument("--semilla", type=int, default=11)
    e.add_argument("--orden", choices=("alterno", "bloque"), default="alterno")
    e.add_argument("--oh-smoke", type=Path, default=Path.home() / "oh-smoke")
    e.add_argument("--oh-supervisor", type=Path, default=Path.home() / "oh-supervisor")
    for nombre in ("verificar", "informe"):
        s = sub.add_parser(nombre)
        s.add_argument("--corrida", required=True)
    a = p.parse_args(argv)
    if a.comando == "ejecutar":
        ids = [x.strip() for x in a.problemas.split(",")] if a.problemas else None
        modelos = [x.strip() for x in a.modelos.split(",") if x.strip()]
        ejecutar(a.corridas.resolve(), a.corrida, a.oh_smoke.resolve(), a.oh_supervisor.resolve(), ids, modelos,
                 a.semilla, a.orden)
        print("== Ejecuciones congeladas. Para evaluar: python3 duo.py verificar --corrida", a.corrida)
    elif a.comando == "verificar":
        d = verificar(a.corridas.resolve(), a.corrida)
        print(f"== Verificado. Fugas: {len(d['auditoria_fugas'])}. Ver {a.corridas.resolve() / a.corrida}/informe.md")
    else:
        informe(a.corridas.resolve(), a.corrida)
    return 0


if __name__ == "__main__":
    sys.exit(main())
