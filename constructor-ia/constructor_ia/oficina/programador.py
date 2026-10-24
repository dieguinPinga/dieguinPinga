"""Etapa 3: un trabajador REAL de programación sobre la cola (OFICINA.md). Reutiliza, sin modificarlos:

- engine.Engine: el ciclo original de Constructor IA, objetivo → código → pruebas en el sandbox → informe de fallos →
  corrección, con hasta max_attempts intentos y sin intervención humana entre ellos;
- sandbox.executor.Executor: Bubblewrap + seccomp + límites de memoria, procesos y CPU, sin red, con plazo por caso y
  total. El código generado se ejecuta SIEMPRE en la VivoBook, aislado: el trabajador remoto solo hace inferencia;
- prompts.failure_report: los fallos de casos ocultos se informan sin su valor esperado;
- Missions, FileStore, EventBus y Database, pero sobre una base y una carpeta PROPIAS de la oficina
  (<oficina>/taller.db y <oficina>/taller/), nunca la base de la aplicación.

Flujo de una tarea de la cola (rol «programador», carga {"mision": <nombre en MISIONES>}):
1. el planificador elige un trabajador disponible ahora; la cola asigna la tarea atómicamente con un arriendo;
2. se registra la EJECUCIÓN (tarea, intento de la cola, misión del taller); una ejecución anterior que quedó a medias
   (proceso caído) se marca «interrumpida» en el taller, nunca «aprobada»;
3. Engine.run corre la misión con el modelo remoto (ClienteRemoto + compuerta por destino); mientras tanto se renueva
   el arriendo (latido). Si el arriendo se pierde, la misión se cancela;
4. el trabajador ENTREGA una propuesta (misión, estado, intento final, huella del código, tokens y tiempos);
5. el COORDINADOR la verifica por su cuenta: vuelve a ejecutar el solucion.py final en el sandbox contra TODOS los
   casos (también los ocultos), comprueba la huella y que el intento final haya aprobado. Recién ahí «verificada».
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import time
from pathlib import Path

from ..config import ModelConfig
from ..db import Database
from ..events import EventBus
from ..files import FileStore
from ..missions import Missions
from . import remoto as RM
from .autocorreccion import MotorConRevisor
from .cola import ArriendoInvalido, Cola
from .misiones_programacion import MISIONES

ESQUEMA = """
CREATE TABLE IF NOT EXISTS ejecuciones (
  tarea_id TEXT NOT NULL, intento INTEGER NOT NULL, trabajador_id TEXT NOT NULL, mision_taller TEXT,
  estado TEXT NOT NULL, inicio REAL NOT NULL, fin REAL, detalle TEXT, PRIMARY KEY (tarea_id, intento));
"""
PARAMETROS_GEMMA = {"num_ctx": 8192, "num_predict": 2048, "temperature": 0.2, "timeout_s": 900, "keep_alive": "10m"}


def modelo_remoto(nombre: str, **over) -> ModelConfig:
    p = dict(PARAMETROS_GEMMA, **over)
    return ModelConfig(nombre, num_ctx=p["num_ctx"], num_predict=p["num_predict"], temperature=p["temperature"],
                       timeout_s=p["timeout_s"], keep_alive=p["keep_alive"])


class Taller:
    """El motor original con base y carpeta propias, y el cliente del destino elegido."""

    def __init__(self, carpeta: Path, cfg, executor, url: str, modelo: ModelConfig, cliente=None, revisor=None):
        carpeta = Path(carpeta)
        self.cfg = dataclasses.replace(cfg, data_dir=carpeta / "taller", builder=modelo)
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = Database(carpeta / "taller.db")
        self.db.migrate()
        self.bus = EventBus(self.db, self.cfg.max_events_per_mission)
        self.missions = Missions(self.db, self.bus)
        self.files = FileStore(self.db, self.cfg.missions_dir)
        self.executor = executor
        self.url = url
        # Sin revisor (None), MotorConRevisor se comporta exactamente como MotorConMemoria.
        self.engine = MotorConRevisor(self.cfg, self.db, self.bus, self.missions, self.files,
                                      cliente or RM.ClienteRemoto(url), RM.compuerta(url), executor, revisor=revisor)

    def cerrar(self):
        self.db.close()


def _huella(codigo: str | None) -> str | None:
    return hashlib.sha256(codigo.encode()).hexdigest() if codigo is not None else None


def informe_mision(taller: Taller, mid: str) -> dict:
    m = taller.missions.get(mid)
    intentos = [dict(r) for r in taller.db.all(
        "SELECT n, started_at, finished_at, gen_ms, prompt_tokens, eval_tokens, done_reason, syntax_ok, test_status, "
        "passed, failed, total, error FROM attempts WHERE mission_id=? ORDER BY n", (mid,))]
    final = taller.files.read_rel(mid, "final/solucion.py")
    mejor = json.loads(taller.files.read_rel(mid, "mejor/resumen.json") or "null")
    return {"mision_taller": mid, "estado": m["state"], "intento_final": m["final_attempt"], "error": m["error"],
            "huella_final": _huella(final), "intentos": intentos,
            "mejor": None if mejor is None else {k: mejor[k] for k in ("intento", "aprobados", "total")}
            | {"huella": _huella(taller.files.read_rel(mid, "mejor/solucion.py"))},
            "tokens": {"prompt": sum(i["prompt_tokens"] or 0 for i in intentos),
                       "generados": sum(i["eval_tokens"] or 0 for i in intentos)},
            "segundos_modelo": round(sum(i["gen_ms"] or 0 for i in intentos) / 1000, 1)}


class Programador:
    """Atiende tareas de programación de la cola con un trabajador del registro."""

    def __init__(self, carpeta: Path, cfg, executor, cola: Cola, *, latido_s: float = 30.0, cliente_para=None,
                 log=print, revisor=None):
        self.carpeta, self.cfg, self.executor, self.cola = Path(carpeta), cfg, executor, cola
        self.cola.db.connect().executescript(ESQUEMA)
        self.latido_s, self.cliente_para, self.log = latido_s, cliente_para, log
        self.revisor = revisor          # oficina.revisor.ConfigRevisor o None (sin revisor)

    def _taller(self, trabajador: dict, modelo: str) -> Taller:
        cliente = self.cliente_para(trabajador) if self.cliente_para else None
        return Taller(self.carpeta, self.cfg, self.executor, trabajador["url"], modelo_remoto(modelo), cliente,
                      revisor=self.revisor)

    def atender(self, trabajador: dict) -> dict | None:
        """Toma UNA tarea de programación para ese trabajador y la lleva hasta la propuesta (o la falla)."""
        self.cola.recuperar()
        t = self.cola.tomar(trabajador["id"], trabajador["modelos"], trabajador["permisos"], trabajador["capacidad"],
                            roles=["programador"]) if trabajador.get("disponible_ahora") else None
        if t is None:
            return None
        taller = self._taller(trabajador, t["modelo"])
        try:
            return self._ejecutar(t, trabajador, taller)
        finally:
            taller.cerrar()

    def _ejecutar(self, t: dict, trabajador: dict, taller: Taller) -> dict:
        db = self.cola.db
        for r in db.all("SELECT * FROM ejecuciones WHERE tarea_id=? AND estado='en_curso'", (t["id"],)):
            if r["mision_taller"] and taller.missions.get(r["mision_taller"]):
                st = taller.missions.get(r["mision_taller"])["state"]
                if st not in ("aprobada", "fallida", "cancelada", "interrumpida"):
                    taller.missions.set_state(r["mision_taller"], "interrumpida", finished_at=time.time(),
                                              error="el proceso que la ejecutaba se interrumpió")
            db.execute("UPDATE ejecuciones SET estado='interrumpida', fin=?, detalle=? WHERE tarea_id=? AND intento=?",
                       (time.time(), "proceso interrumpido; la cola la reasignó", r["tarea_id"], r["intento"]))
        fabrica, _ = MISIONES[t["carga"]["mision"]]
        mision = fabrica()
        mid = taller.missions.create(mision, t["modelo"], f"oficina:{trabajador['id']}")
        db.execute("INSERT INTO ejecuciones (tarea_id, intento, trabajador_id, mision_taller, estado, inicio) "
                   "VALUES (?,?,?,?,?,?)", (t["id"], t["intentos"], trabajador["id"], mid, "en_curso", time.time()))
        self.cola.iniciar(t["id"], t["token"])
        cancel, fin = threading.Event(), threading.Event()
        perdido = []

        def latir():
            while not fin.wait(self.latido_s):
                try:
                    self.cola.latido(t["id"], t["token"])
                except ArriendoInvalido as e:
                    perdido.append(str(e))
                    cancel.set()
                    return
        hilo = threading.Thread(target=latir, daemon=True)
        hilo.start()
        try:
            estado = taller.engine.run(mid, cancel)
        finally:
            fin.set()
            hilo.join(5)
        inf = informe_mision(taller, mid)
        db.execute("UPDATE ejecuciones SET estado=?, fin=?, detalle=? WHERE tarea_id=? AND intento=?",
                   (estado, time.time(), json.dumps(inf, ensure_ascii=False)[:20000], t["id"], t["intentos"]))
        if perdido:
            return {"tarea": t["id"], "resultado": "arriendo_perdido", "detalle": perdido[0], "informe": inf}
        try:
            if estado in ("aprobada", "fallida") and not _falla_de_entorno(inf):
                self.cola.entregar(t["id"], t["token"], {"mision_taller": mid, "estado": estado,
                                                         "intento_final": inf["intento_final"],
                                                         "huella_final": inf["huella_final"],
                                                         "tokens": inf["tokens"], "segundos_modelo": inf["segundos_modelo"],
                                                         "mejor": inf["mejor"]})
                return {"tarea": t["id"], "resultado": "propuesta", "informe": inf}
            nuevo = self.cola.fallar(t["id"], t["token"], f"misión {estado}: {inf['error']}")
            return {"tarea": t["id"], "resultado": f"falla_de_entorno → {nuevo}", "informe": inf}
        except ArriendoInvalido as e:
            return {"tarea": t["id"], "resultado": "arriendo_perdido", "detalle": str(e), "informe": inf}


def _falla_de_entorno(inf: dict) -> bool:
    """Ollama caído o sandbox bloqueado: no es una respuesta del modelo; la tarea se reintenta."""
    e = (inf.get("error") or "")
    return any(x in e for x in ("no disponible", "Ejecución aislada bloqueada", "Ejecución bloqueada",
                                "Cancelada"))


def verificar(cola: Cola, carpeta: Path, cfg, executor, tid: str) -> tuple[bool, str]:
    """El COORDINADOR verifica una propuesta de programación por su cuenta y decide en la cola."""
    t = cola.tarea(tid)
    p = t["propuesta"] or {}
    taller = Taller(carpeta, cfg, executor, "http://127.0.0.1:9", modelo_remoto(t["modelo"]),
                    cliente=object())                       # sin cliente: la verificación no infiere
    try:
        ok, detalle = _verificar(taller, t, p)
    finally:
        taller.cerrar()
    cola.verificar(tid, ok, detalle)
    return ok, detalle


def _verificar(taller: Taller, t: dict, p: dict) -> tuple[bool, str]:
    mid = p.get("mision_taller")
    m = taller.missions.get(mid) if mid else None
    if m is None:
        return False, "la propuesta no apunta a una misión del taller"
    if m["state"] != "aprobada" or p.get("estado") != "aprobada":
        return False, f"la misión terminó «{m['state']}» sin superar las pruebas en sus intentos" + _mejor(taller, t, mid)
    codigo = taller.files.read_rel(mid, "final/solucion.py")
    if codigo is None or _huella(codigo) != p.get("huella_final"):
        return False, "el código final no coincide con la huella entregada"
    fabrica, _ = MISIONES[t["carga"]["mision"]]
    mision = fabrica()
    res = taller.executor.run(codigo, mision["spec"], mision["tests_py"])
    if res.status != "ok":
        return False, f"la reejecución independiente terminó «{res.status}»: {res.reason}"
    r = res.result
    if not (r.get("total") and r.get("failed") == 0 and r.get("passed") == r.get("total")):
        return False, f"la reejecución independiente no aprobó: {r.get('passed')}/{r.get('total')}"
    return True, (f"reejecución independiente aprobada ({r['passed']}/{r['total']} casos, incluidos los ocultos); "
                  f"misión {mid} aprobada en el intento {m['final_attempt']}")


def _mejor(taller: Taller, t: dict, mid: str) -> str:
    """Reejecuta, de forma independiente, la mejor versión conservada de una misión que no aprobó (informativo:
    una misión solo se acepta si pasa todas las pruebas)."""
    codigo = taller.files.read_rel(mid, "mejor/solucion.py")
    resumen = json.loads(taller.files.read_rel(mid, "mejor/resumen.json") or "null")
    if codigo is None or resumen is None:
        return ""
    fabrica, _ = MISIONES[t["carga"]["mision"]]
    mision = fabrica()
    res = taller.executor.run(codigo, mision["spec"], mision["tests_py"])
    r = res.result or {}
    return (f"; mejor versión conservada: intento {resumen['intento']} ({resumen['aprobados']}/{resumen['total']}); "
            f"reejecución independiente: {r.get('passed')}/{r.get('total')}" if res.status == "ok" else
            f"; mejor versión conservada: intento {resumen['intento']}; reejecución: {res.status}")


def comprobar_mision(executor, nombre: str = "resumen_ventas") -> dict:
    """SIN inferencias ni código de la IA: ejecuta NUESTRA solución de referencia contra todos los casos."""
    fabrica, referencia = MISIONES[nombre]
    mision = fabrica()
    res = executor.run(referencia, mision["spec"], mision["tests_py"])
    r = res.result or {}
    ok = res.status == "ok" and r.get("total") and r.get("failed") == 0
    return {"mision": nombre, "ok": bool(ok), "estado": res.status, "motivo": res.reason,
            "casos": f"{r.get('passed')}/{r.get('total')}",
            "visibles": sum(1 for c in mision["spec"]["cases"] if c.get("visible", True)),
            "ocultos": sum(1 for c in mision["spec"]["cases"] if not c.get("visible", True))}
