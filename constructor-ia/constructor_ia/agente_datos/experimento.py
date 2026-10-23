"""Fase 2 del agente de datos: M1, M2 y M2′ con un modelo real, en procesos separados y con el juez aparte.

Carpeta del experimento (una por corrida; no se mezcla ni se reutiliza):
    plan.json            semilla, base, preguntas, ranuras y la HUELLA de los datos sintéticos (CHECKSUM por tabla)
    estado.json          estado de cada ranura (en_curso, completa, interrumpida, error, bloqueada)
    juez/verdad.json     la verdad oculta (0600). Su ruta NUNCA se pasa al proceso del agente
    memoria_m1_m2/       agente.db y misiones de M1 y M2 (M2 hereda la memoria de M1)
    memoria_m2p/         agente.db y misiones de M2′ (memoria vacía)
    resultados/<R>.json  por ranura: estado, nivel del coordinador, veredicto del juez (SEPARADOS), costo y fugas
    registros/<R>.err    lo que escribió el proceso del agente (decisiones en vivo)
    resumen.md

Reglas (no hay reanudación todavía):
- una ranura se ejecuta UNA vez por carpeta: si ya tiene estado (también interrumpida, error o «en_curso» de un
  proceso que murió), no se repite ni se continúa, y se informa. Para otra corrida: otra carpeta y --preparar;
- M2 exige que M1 haya dado una RESPUESTA CORRECTA según el juez (completa, no abstención, correcta y sin confirmación
  falsa), no solo que haya terminado: se quiere comprobar la memoria después de una misión exitosa. El juez controla
  la progresión del experimento, pero su verdad y sus veredictos nunca llegan al agente (ni a sus prompts ni a su
  memoria). M1 y M2′ exigen una memoria nueva (inexistente);
- en cada misión la base B1 se vuelve a crear desde la semilla en un MariaDB privado nuevo y su huella tiene que ser
  IDÉNTICA a la del plan: M2 y M2′ usan exactamente los mismos datos que M1;
- una misión interrumpida queda «interrumpida», sin veredicto del juez: nunca se confunde con una completa.
"""
from __future__ import annotations

import ctypes
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import VERSION
from . import generador as G
from . import juez as J

VERSION_EXPERIMENTO = "agente de datos fase 2 v2"
BASE = "b1_lab"
TABLAS = ("categorias", "productos", "clientes", "pedidos", "lineas_pedido", "pedidos_borrador")
CRITERIO_M2 = ("M1 completa, con respuesta (verificada o incierta, no abstención), correcta según el juez y sin "
               "confirmación falsa")
RANURAS = {"M1": {"pregunta": "Q1", "memoria": "memoria_m1_m2", "requiere": None, "memoria_nueva": True},
           "M2": {"pregunta": "Q2", "memoria": "memoria_m1_m2", "requiere": "M1", "memoria_nueva": False},
           "M2p": {"pregunta": "Q2", "memoria": "memoria_m2p", "requiere": None, "memoria_nueva": True}}
ARRANQUE = ("import sys\nsrc = sys.argv.pop(1)\nsys.path.insert(0, src)\nimport constructor_ia\n"
            "if not constructor_ia.__file__.startswith(src + '/'):\n"
            "    sys.exit(f'se cargó otro código ({constructor_ia.__file__}); se esperaba {src}')\n"
            "from constructor_ia.agente_datos.__main__ import main\nsys.exit(main(sys.argv[1:]))\n")


class ErrorExperimento(RuntimeError):
    pass


def _leer(p: Path, defecto=None):
    return json.loads(p.read_text()) if p.exists() else defecto


def _escribir(p: Path, obj, modo: int = 0o600):
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str))
    os.chmod(tmp, modo)
    tmp.replace(p)


def huella_base(srv, base: str) -> dict:
    """CHECKSUM TABLE (como administrador del MariaDB PRIVADO) y filas de cada tabla de B1."""
    out = {}
    for t in TABLAS:
        cs = srv.sql(f"CHECKSUM TABLE `{t}` EXTENDED", database=base).stdout.split()
        n = srv.sql(f"SELECT COUNT(*) FROM `{t}`", database=base).stdout.strip()
        out[t] = {"checksum": cs[-1], "filas": int(n)}
    return out


def _servidor_privado(directorio: Path):
    from ..laboratorio.privado import ServidorPrivado
    return ServidorPrivado(directorio)


def _dir_servidor(carpeta: Path, nombre: str) -> Path:
    """Carpeta del MariaDB privado de un paso. Un socket Unix admite rutas de hasta 107 bytes: si la carpeta del
    experimento es demasiado profunda, se usa una carpeta temporal corta (se borra al detener el servidor)."""
    d = carpeta / f"db-{nombre}-{secrets.token_hex(2)}"
    if len(str(d / "s.sock").encode()) <= 100:
        return d
    return Path(tempfile.mkdtemp(prefix="cia-ag-db-"))     # vacía; el servidor la borra al detenerse


def preparar(carpeta: Path, *, semilla: int = G.SEMILLA, base: str = BASE, servidor=_servidor_privado,
             log=print) -> dict:
    carpeta = Path(carpeta)
    if (carpeta / "plan.json").exists():
        raise ErrorExperimento(f"{carpeta} ya está preparada: una corrida por carpeta (usar otra para empezar de nuevo)")
    carpeta.mkdir(parents=True, exist_ok=True, mode=0o700)
    (carpeta / "juez").mkdir(exist_ok=True, mode=0o700)
    with servidor(_dir_servidor(carpeta, "prep")) as srv:
        G.crear(srv, base, semilla, verdad_en=carpeta / "juez" / "verdad.json")
        huella = huella_base(srv, base)
    os.chmod(carpeta / "juez" / "verdad.json", 0o600)
    plan = {"version": VERSION_EXPERIMENTO, "agente": VERSION, "semilla": semilla, "base": base,
            "preguntas": {r: G.PREGUNTAS[x["pregunta"]] for r, x in RANURAS.items()},
            "ranuras": RANURAS, "criterio_para_M2": CRITERIO_M2, "huella_datos": huella,
            "creado": time.strftime("%Y-%m-%d %H:%M:%S")}
    _escribir(carpeta / "plan.json", plan)
    _escribir(carpeta / "estado.json", {})
    log(f"Preparada {carpeta}: base {base} (semilla {semilla}), {sum(x['filas'] for x in huella.values())} filas en "
        f"{len(huella)} tablas. La verdad queda en juez/verdad.json (no se pasa al agente).")
    return plan


def _morir_con_el_padre():
    """El proceso del agente recibe SIGTERM si el orquestador muere (no queda un agente huérfano llamando a Ollama
    después de que el script protegido restauró el servicio)."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)   # PR_SET_PDEATHSIG
    except OSError:
        pass


def _correr_agente(argv: list[str], err: Path, log) -> tuple[int, str]:
    src = str(Path(__file__).resolve().parent.parent.parent)
    cmd = [sys.executable, "-I", "-c", ARRANQUE, src, *argv]
    with open(err, "w", encoding="utf-8") as fe, subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, preexec_fn=_morir_con_el_padre) as p:
        try:
            for linea in p.stderr:
                fe.write(linea)
                fe.flush()
                log(linea.rstrip())
            out = p.stdout.read()
            p.wait()
        except KeyboardInterrupt:
            log("[agente] interrupción: se le pide al proceso del agente que cierre la misión como interrumpida…")
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
            try:
                out, resto = p.communicate(timeout=120)
                fe.write(resto or "")
            except subprocess.TimeoutExpired:
                p.kill()
                out, _ = p.communicate()
            raise _Interrumpido(p.returncode, out or "")
    return p.returncode, out


class _Interrumpido(Exception):
    def __init__(self, rc, out):
        super().__init__("interrumpido")
        self.rc, self.out = rc, out


def mision(carpeta: Path, ranura: str, modelo_args: list[str], *, servidor=_servidor_privado, log=print) -> dict:
    carpeta = Path(carpeta)
    plan = _leer(carpeta / "plan.json")
    if plan is None:
        raise ErrorExperimento(f"{carpeta} no está preparada (correr primero --preparar)")
    if ranura not in RANURAS:
        raise ErrorExperimento(f"ranura desconocida {ranura}; hay {', '.join(RANURAS)}")
    estado = _leer(carpeta / "estado.json", {})
    if ranura in estado:
        e = estado[ranura]["estado"]
        raise ErrorExperimento(
            f"{ranura} ya se ejecutó en esta carpeta (estado: {e}"
            + ("; el proceso no llegó a cerrarla: cuenta como interrumpida" if e == "en_curso" else "")
            + "). Sin reanudación: no se repite ni se continúa. Para otra corrida, preparar una carpeta nueva.")
    r = RANURAS[ranura]
    if r["requiere"]:
        no = habilitacion(carpeta, r["requiere"], estado)
        if no:
            raise ErrorExperimento(f"{ranura} no está habilitada: {no}. Criterio: {CRITERIO_M2}")
    lab = carpeta / r["memoria"]
    if r["memoria_nueva"] and lab.exists():
        raise ErrorExperimento(f"{ranura} necesita una memoria nueva y {lab} ya existe")
    if not r["memoria_nueva"] and not (lab / "agente.db").exists():
        raise ErrorExperimento(f"{ranura} necesita la memoria de {r['requiere']} en {lab}, y no existe")
    pregunta = plan["preguntas"][ranura]
    t0 = time.time()
    with servidor(_dir_servidor(carpeta, ranura)) as srv:
        G.crear(srv, plan["base"], plan["semilla"])
        huella = huella_base(srv, plan["base"])
        if huella != plan["huella_datos"]:
            raise ErrorExperimento("los datos sintéticos regenerados NO coinciden con los del plan: no se ejecuta "
                                   f"(plan {plan['huella_datos']}; ahora {huella})")
        usuario, clave = "lector_agente", secrets.token_urlsafe(18)
        G.crear_lector(srv, plan["base"], usuario, clave)
        cdir = carpeta / "conexion"
        cdir.mkdir(exist_ok=True, mode=0o700)
        pw, cx = cdir / f"pw-{ranura}", cdir / f"conexion-{ranura}.json"
        pw.write_text(clave + "\n")
        os.chmod(pw, 0o600)
        _escribir(cx, {"name": "b1", "database": plan["base"], "user": usuario, "password_file": str(pw),
                       "socket": srv.socket})
        estado[ranura] = {"estado": "en_curso", "inicio": time.strftime("%Y-%m-%d %H:%M:%S"), "pid": os.getpid()}
        _escribir(carpeta / "estado.json", estado)
        argv = ["mision", "--carpeta", str(lab), "--conexion", str(cx), "--pregunta", pregunta, *modelo_args]
        (carpeta / "registros").mkdir(exist_ok=True, mode=0o700)
        interrumpido = False
        try:
            rc, out = _correr_agente(argv, carpeta / "registros" / f"{ranura}.err", log)
        except _Interrumpido as e:
            rc, out, interrumpido = e.rc, e.out, True
        finally:
            pw.unlink(missing_ok=True)
    informe = None
    lineas = [x for x in (out or "").splitlines() if x.startswith("{")]
    if lineas:
        try:
            informe = _leer(Path(json.loads(lineas[-1])["informe"]))
        except (ValueError, KeyError, OSError):
            informe = None
    # Con informe, vale su estado (también si la interrupción llegó con la misión ya cerrada). Sin informe: interrumpida
    # si hubo una señal o un corte; si no, el proceso falló al arrancar o terminó mal = error. Ninguna se repite.
    if informe:
        est = informe["estado"]
    elif interrumpido or rc in (4, 130, 143, -2, -9, -15):
        est = "interrumpida"
    else:
        est = "error"
    verdad = J.cargar(carpeta / "juez" / "verdad.json")
    qid = RANURAS[ranura]["pregunta"]
    juicio = (J.evaluar(informe, verdad, qid) if est == "completa" else
              {"pregunta": qid, "correcta": None, "confirmacion_falsa": False, "error_declarado": False,
               "abstencion": False, "sin_veredicto": f"misión {est}: no se juzga"})
    res = {"ranura": ranura, "pregunta": pregunta, "mision": (informe or {}).get("mision"), "estado": est,
           "codigo_salida": rc, "interrumpida_por_el_operador": interrumpido,
           "duracion_s": round(time.time() - t0, 1), "huella_datos_identica": True,
           "coordinador": {"nivel": (informe or {}).get("nivel"),
                           "verificada": bool(informe and informe.get("nivel") == "verificada"),
                           "dudas_o_motivos": (informe or {}).get("dudas_o_motivos", [])},
           "juez": juicio, "respuesta": (informe or {}).get("respuesta"),
           "costo": (informe or {}).get("costo"), "rechazos": (informe or {}).get("rechazos", []),
           "interrupciones_del_entorno": (informe or {}).get("interrupciones_del_entorno", []),
           "uso_de_memoria": (informe or {}).get("uso_de_memoria"), "nuevo": (informe or {}).get("nuevo"),
           "fugas": J.auditar_fugas([lab, carpeta / "registros", carpeta / "conexion"], verdad)}
    _escribir(carpeta / "resultados" / f"{ranura}.json", res)
    estado[ranura] = {"estado": est, "mision": res["mision"], "inicio": estado[ranura]["inicio"],
                      "fin": time.strftime("%Y-%m-%d %H:%M:%S")}
    _escribir(carpeta / "estado.json", estado)
    (carpeta / "resumen.md").write_text(resumen(carpeta))
    return res


def habilitacion(carpeta: Path, previa: str, estado: dict) -> str | None:
    """Motivo por el que la ranura previa NO habilita la siguiente (None = habilita). Lo decide el orquestador con
    el veredicto del juez; nada de esto se le pasa al agente."""
    e = estado.get(previa, {}).get("estado", "sin ejecutar")
    if e != "completa":
        return f"{previa} no terminó completa (estado: {e})"
    res = _leer(carpeta / "resultados" / f"{previa}.json")
    if res is None:
        return f"no está el resultado de {previa}"
    nivel = res["coordinador"]["nivel"]
    if nivel not in ("verificada", "incierta"):
        return f"{previa} terminó completa pero sin respuesta (nivel: {nivel})"
    if res["juez"].get("confirmacion_falsa"):
        return f"{previa} es una CONFIRMACIÓN FALSA (verificada por el coordinador, incorrecta según el juez): analizar"
    if res["juez"].get("correcta") is not True:
        return f"la respuesta de {previa} es incorrecta según el juez (nivel del coordinador: {nivel})"
    return None


def resumen(carpeta: Path) -> str:
    carpeta = Path(carpeta)
    plan = _leer(carpeta / "plan.json", {})
    estado = _leer(carpeta / "estado.json", {})
    out = [f"# {VERSION_EXPERIMENTO}", "", f"Base `{plan.get('base')}` (semilla {plan.get('semilla')}); los datos de "
           "cada misión se regeneran y su huella se compara con la del plan.", "",
           "| Ranura | Pregunta | Estado | Nivel (coordinador) | Verificada | Correcta (juez) | Confirmación falsa | "
           "Llamadas | Tokens (entrada/generados) | Consultas | Rechazos | Interrupciones | Duración |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in RANURAS:
        res = _leer(carpeta / "resultados" / f"{r}.json")
        if res is None:
            e = estado.get(r, {}).get("estado", "sin ejecutar")
            out.append(f"| {r} | {plan.get('preguntas', {}).get(r, '')} | {e} | — | — | — | — | — | — | — | — | — | — |")
            continue
        c, j, k = res["coordinador"], res["juez"], res.get("costo") or {}
        tok = k.get("tokens") or {}
        corr = "—" if j.get("correcta") is None else ("sí" if j["correcta"] else "no")
        out.append(f"| {r} | {res['pregunta']} | {res['estado']} | {c['nivel'] or '—'} | "
                   f"{'sí' if c['verificada'] else 'no'} | {corr} | {'**SÍ**' if j.get('confirmacion_falsa') else 'no'} | "
                   f"{k.get('llamadas', '—')} {k.get('por_microtarea', '')} | {tok.get('prompt', '—')}/"
                   f"{tok.get('generados', '—')} | {k.get('consultas', '—')} | {len(res['rechazos'])} | "
                   f"{len(res['interrupciones_del_entorno'])} | {res['duracion_s']} s |")
    out += ["", "El nivel lo decide el coordinador sin conocer la verdad; «correcta» la decide el juez después, en otro "
                "proceso. Una misión interrumpida no se juzga y no cuenta como abstención.", ""]
    for r in RANURAS:
        res = _leer(carpeta / "resultados" / f"{r}.json")
        if res:
            out += [f"## {r}", "", f"- Respuesta: {(res.get('respuesta') or {}).get('texto', '—')}",
                    f"- Dudas o motivos: {res['coordinador']['dudas_o_motivos']}",
                    f"- Rechazos: {res['rechazos']}", f"- Interrupciones del entorno: "
                    f"{res['interrupciones_del_entorno']}", f"- Memoria usada: {res.get('uso_de_memoria')}",
                    f"- Fugas: {res['fugas'] or 'ninguna'}", ""]
    return "\n".join(out) + "\n"


def comprobar(*, semilla: int = G.SEMILLA, base: str = BASE, carpeta_plan: Path | None = None,
              servidor=_servidor_privado, ollama_url: str | None = None, modelo: str = "qwen3:4b-instruct",
              log=print) -> dict:
    """Comprobación SIN modelo real: MariaDB privado, datos idénticos al plan, cuenta de solo lectura verificada,
    escritura rechazada, una misión completa con el modelo de guion en un proceso aparte, juez y fugas. Con
    ollama_url, consulta solo /api/tags (sin inferencias)."""
    from .. import config
    from ..config import ExplorerConnection
    from ..db import Database
    from ..explorer.broker import Broker
    informe = {"pasos": []}

    def paso(nombre, ok, detalle=""):
        informe["pasos"].append({"paso": nombre, "ok": bool(ok), "detalle": str(detalle)[:400]})
        log(f"  [{'ok' if ok else 'FALLA'}] {nombre}: {detalle}")

    plan = _leer(Path(carpeta_plan) / "plan.json") if carpeta_plan else None
    if plan:
        semilla, base = plan["semilla"], plan["base"]
    tmp = Path(tempfile.mkdtemp(prefix="cia-ag-"))
    with servidor(tmp / "mariadb") as srv:
        verdad = G.crear(srv, base, semilla, verdad_en=tmp / "verdad-de-prueba.json")
        huella = huella_base(srv, base)
        paso("datos sintéticos", True, f"{sum(x['filas'] for x in huella.values())} filas en {len(huella)} tablas")
        if plan:
            paso("datos idénticos al plan", huella == plan["huella_datos"], "huella por tabla (CHECKSUM)")
        clave = secrets.token_urlsafe(18)
        G.crear_lector(srv, base, "lector_comprobar", clave)
        pw = tmp / "pw"
        pw.write_text(clave + "\n")
        os.chmod(pw, 0o600)
        cfg = config.load(tmp / "sin-config.toml", base_dir=tmp)
        db = Database(tmp / "registro.db")
        db.migrate()
        conn = ExplorerConnection("b1", base, "lector_comprobar", str(pw), socket=srv.socket)
        b = Broker(cfg, conn, db)
        v = b.verify()
        paso("cuenta de solo lectura verificada", v.get("verified"), v.get("reason") or
             f"privilegios {v.get('grants')}; sonda de escritura {v.get('write_probe')}; sesión {v.get('session')}")
        antes = huella_base(srv, base)
        o = b.run("DELETE FROM pedidos")
        paso("escritura rechazada", o.status in ("rechazada", "error"), f"{o.status}: {o.error[:120]}")
        paso("base sin cambios", huella_base(srv, base) == antes, "huella antes y después")
        db.close()
        cx = tmp / "conexion.json"
        cx.write_text(json.dumps({"name": "b1", "database": base, "user": "lector_comprobar", "password_file": str(pw),
                                  "socket": srv.socket}))
        lab = tmp / "lab"
        rc, out = _correr_agente(["mision", "--carpeta", str(lab), "--conexion", str(cx), "--pregunta",
                                  G.PREGUNTAS["Q1"], "--modelo", "guion"], tmp / "agente.err", lambda *_: None)
        inf = _leer(Path(json.loads(out.strip().splitlines()[-1])["informe"])) if rc == 0 else None
        paso("misión de prueba con el modelo de guion (proceso aparte)", inf and inf["estado"] == "completa",
             f"código {rc}; nivel {(inf or {}).get('nivel')}; {(inf or {}).get('costo')}")
        if inf:
            j = J.evaluar(inf, verdad, "Q1")
            paso("juez (con la verdad de prueba)", j["correcta"], j)
        paso("sin fugas de la verdad", not J.auditar_fugas([lab], verdad), "canario y descripciones")
    if ollama_url:
        try:
            from ..ollama import OllamaClient
            tags = OllamaClient(ollama_url).tags()
            paso("Ollama CPU responde (solo /api/tags, sin inferencias)", modelo in tags,
                 f"{modelo} {'instalado' if modelo in tags else 'NO instalado'}")
        except Exception as e:  # noqa: BLE001
            paso("Ollama CPU responde (solo /api/tags, sin inferencias)", False, e)
    shutil.rmtree(tmp, ignore_errors=True)
    informe["ok"] = all(p["ok"] for p in informe["pasos"])
    return informe
