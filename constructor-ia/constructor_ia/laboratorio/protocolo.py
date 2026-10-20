"""Protocolo del laboratorio: el modelo diseña, ejecuta, evalúa y corrige instrumentos; cada paso deja métricas.

Por tarea:
1. DISEÑO: el modelo recibe la pregunta, la estructura observada, el presupuesto y el índice de instrumentos ya
   existentes; responde un manifiesto (```json) y el código (```python) con medir(db, p).
2. EJECUCIÓN: revisión estática → sandbox con el canal de consultas → verificación (contrato y cobertura).
3. EVALUACIÓN: el modelo ve la salida y la verificación y decide si responde la pregunta (si | parcial | no), con la
   respuesta en la forma pedida y, si no alcanza, qué mejorar.
4. CORRECCIÓN: si la ejecución falló o el modelo dijo que no alcanza, escribe una versión nueva (hasta un máximo).
5. PUNTAJE: la respuesta final se compara con la verdad medida directamente; la autoevaluación se contrasta con el
   puntaje (calibración).

Tarea R: con datos NUEVOS, el modelo elige un instrumento existente y sólo propone parámetros (no reescribe código).
Tarea C: se siembra un instrumento que corre bien pero no alcanza; se mide si el modelo lo detecta y lo corrige.

Mismas condiciones para todos los modelos: misma base (semilla), prompts, presupuestos, contexto, temperatura y
máximo de correcciones. Las pruebas automáticas con un Ollama simulado validan este circuito, NO la capacidad de un
modelo.
"""
from __future__ import annotations

import dataclasses
import json
import re
import time

from ..ollama import Cancelled, ContextOverflow, OllamaError, OllamaUnavailable
from ..instrumentos.almacen import Almacen
from ..instrumentos.canal import Presupuesto
from ..instrumentos.medicion import compact_schema, ejecutar
from . import tareas as T

SYSTEM = """Sos un investigador de datos que construye INSTRUMENTOS de medición en Python.
Un instrumento es un programa pequeño y reutilizable con una función medir(db, p) que corre aislado (sin red, sin
archivos, sin credenciales) y consulta MariaDB sólo a través de db:

* db.consultar(sql, max_filas=10000) -> Resultado con .columnas, .filas (lista de tuplas), .truncado, .motivo,
  .dicts() y .valores("columna") (convierte a número; los DECIMAL como AVG() llegan como texto).
* db.estimar(sql) -> {"filas_estimadas": n, "plan": [...]} sin ejecutar la consulta.
* db.esquema() -> tablas y columnas.   db.presupuesto() -> lo que queda.
* p -> diccionario con los parámetros de esta ejecución.
Si se agota el presupuesto, consultar() lanza investiga.PresupuestoAgotado.

Reglas:
* Sólo SELECT de lectura. Sólo biblioteca estándar (math, statistics, collections, datetime, json, itertools, re…).
  Prohibido: import os/sys/subprocess/socket, open(), eval(), exec(), getattr(), atributos con doble guion bajo.
* Las tablas pueden tener millones de filas y el presupuesto es chico: AGREGÁ EN SQL (COUNT, SUM, AVG, GROUP BY,
  FLOOR(valor/ancho)) en vez de descargar filas crudas. Una muestra recortada no representa al total.
* medir devuelve un dict pequeño (máximo {resultado_kb} KB):
  {"resultado": {...lo que mediste...},
   "cobertura": {"filas_analizadas": <número total de filas que respalda el resultado, obtenido con COUNT>, ...},
   "limitaciones": ["..."]}
  Si el resultado es parcial (tope de presupuesto, recorte), decilo en «cobertura» o «limitaciones».
* Para reutilizar un instrumento anterior: import <modulo> (por ejemplo import tardias_v1) y declaralo en
  "bibliotecas" del manifiesto.
"""

FORMATO_CODIGO = """Respondé EXACTAMENTE con dos bloques, sin texto adicional:
```json
{"nombre": "nombre_corto", "proposito": "qué mide", "parametros": {"nombre": valor_por_defecto},
 "plan": "cómo responde la pregunta", "muestreo": "ninguno (agregado en SQL) | descripción", "bibliotecas": []}
```
```python
def medir(db, p):
    ...
```"""

FORMATO_EVAL = ('Respondé SÓLO con JSON: {"responde": "si" | "parcial" | "no", "motivo": "por qué", '
                '"respuesta": <la respuesta en la forma pedida, con los valores medidos>, '
                '"mejora": "qué cambiar en el instrumento si no alcanza (vacío si responde)"}')

BLOCK_RE = re.compile(r"```([a-zA-Z]*)[ \t]*\n(.*?)```", re.S)
THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class LaboratorioAbortado(Exception):
    pass


def _j(v, limit=6000) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "…(recortado)"


def parse_json(text: str):
    text = THINK_RE.sub("", text or "").strip()
    for cand in [text] + [b for _, b in BLOCK_RE.findall(text)]:
        try:
            return json.loads(cand)
        except (TypeError, ValueError):
            pass
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        try:
            return json.loads(text[a:b + 1])
        except ValueError:
            pass
    return None


def parse_diseno(text: str) -> tuple[dict | None, str | None, list[str]]:
    """Manifiesto (```json) y código (```python con medir). Devuelve (manifiesto, código, problemas)."""
    text = THINK_RE.sub("", text or "")
    manifest, code, problems = None, None, []
    for lang, body in BLOCK_RE.findall(text):
        lang = lang.lower()
        if code is None and (lang in ("python", "py") or "def medir" in body):
            code = body.strip() + "\n"
        elif manifest is None and lang in ("json", ""):
            try:
                m = json.loads(body)
                manifest = m if isinstance(m, dict) else None
            except ValueError:
                pass
    if code is None:
        problems.append("no hay un bloque ```python con def medir(db, p)")
    if manifest is None:
        problems.append("no hay un manifiesto ```json válido")
    elif not isinstance(manifest.get("parametros", {}), dict):
        problems.append("«parametros» del manifiesto debe ser un objeto")
        manifest["parametros"] = {}
    return manifest, code, problems


class Modelo:
    """Un modelo con opciones FIJAS para todo el laboratorio (las mismas para cada modelo comparado)."""

    def __init__(self, ollama, base_model, nombre: str, *, num_ctx: int, temperatura: float, timeout_s: int,
                 num_predict_codigo: int = 2048, num_predict_json: int = 768):
        self.ollama, self.nombre = ollama, nombre
        self.base = dataclasses.replace(base_model, name=nombre, num_ctx=num_ctx, temperature=temperatura,
                                        timeout_s=timeout_s)
        self.np_code, self.np_json = num_predict_codigo, num_predict_json
        self.system = SYSTEM

    def opciones(self) -> dict:
        return {"modelo": self.nombre, "num_ctx": self.base.num_ctx, "temperatura": self.base.temperature,
                "timeout_s": self.base.timeout_s, "num_predict_codigo": self.np_code,
                "num_predict_json": self.np_json, "think": self.base.think}

    def pedir(self, tipo: str, user: str, *, json_out: bool, cancel=None) -> tuple[str | None, dict]:
        model = dataclasses.replace(self.base, num_predict=self.np_json if json_out else self.np_code)
        msgs = [{"role": "system", "content": self.system}, {"role": "user", "content": user}]
        call = {"tipo": tipo, "ts": time.time()}
        try:
            gen = self.ollama.chat(model, msgs, cancel, fmt="json" if json_out else None)
        except Cancelled:
            raise LaboratorioAbortado("cancelado") from None
        except OllamaUnavailable as e:
            raise LaboratorioAbortado(f"Ollama no disponible: {e}") from None
        except ContextOverflow as e:
            return None, {**call, "resultado": "contexto_excedido", "error": str(e)[:200]}
        except OllamaError as e:
            return None, {**call, "resultado": "error", "error": str(e)[:200]}
        t = gen.timings()
        return gen.text, {**call, "resultado": "truncada" if gen.truncated else "completa",
                          "tokens_prompt": gen.prompt_tokens or 0, "tokens_generados": gen.eval_tokens or 0,
                          "reloj_ms": t["reloj_ms"], "generacion_tokens_s": t["generacion_tokens_s"]}


def calibracion(responde, puntaje) -> str | None:
    if responde not in ("si", "parcial", "no"):
        return None
    if responde == "si":
        return "calibrada" if puntaje >= 1 else "sobreconfianza"
    return "subconfianza" if puntaje >= 1 else "calibrada"


class Sesion:
    """Una pasada de las tareas para UN modelo y UNA repetición, sobre su propia base sintética y su propio almacén."""

    def __init__(self, *, cfg, modelo: Modelo, executor, broker, almacen: Almacen, server, info: dict, schema: dict,
                 max_correcciones: int = 2, cancel=None, log=print, avanzar=None):
        modelo.system = SYSTEM.replace("{resultado_kb}", str(cfg.instrumentos_resultado_kb))
        self.cfg, self.modelo, self.executor, self.broker, self.almacen = cfg, modelo, executor, broker, almacen
        self.server, self.info, self.schema = server, info, schema
        self.max_corr, self.cancel, self.log = max_correcciones, cancel, log
        self.budget = Presupuesto.from_cfg(cfg)
        self.avanzar = avanzar
        self.instrumento_de: dict[str, int] = {}   # tarea → último instrumento usado

    # Contexto para el modelo ---------------------------------------------------------------------------
    def _contexto(self, t: T.Tarea) -> str:
        b = self.budget
        idx = [{k: v for k, v in i.items() if k in ("id", "modulo", "proposito", "parametros")}
               for i in self.almacen.index()]
        return (f"Tarea {t.id}: {T.pregunta(t, self.info)}\n"
                + (f"{t.pista_parametros}\n" if t.pista_parametros else "")
                + f"Forma de la respuesta final: {_j(t.forma)}\n\n"
                f"Estructura observada (filas estimadas por el motor):\n{_j(compact_schema(self.schema), 3000)}\n\n"
                f"Presupuesto por ejecución: {b.consultas} consultas, {b.filas} filas, {b.bytes // (1024 * 1024)} MB, "
                f"{b.segundos} s de cálculo, salida de hasta {b.resultado_bytes // 1024} KB.\n"
                f"Instrumentos existentes: {_j(idx, 2500) if idx else 'ninguno'}\n")

    def _diseno_prompt(self, t):
        return self._contexto(t) + "\nDiseñá el experimento y escribí el instrumento.\n" + FORMATO_CODIGO

    def _correccion_prompt(self, t, code, que_paso):
        return (self._contexto(t) + f"\nInstrumento anterior:\n```python\n{code}```\nQué pasó: {que_paso}\n\n"
                "Escribí una versión corregida que sí responda la pregunta.\n" + FORMATO_CODIGO)

    def _eval_prompt(self, t, inst, run):
        v = run.get("verdict") or {}
        return (f"Tarea {t.id}: {T.pregunta(t, self.info)}\nForma de la respuesta: {_j(t.forma)}\n\n"
                f"Instrumento {inst['modulo']} (propósito: {inst['manifest'].get('proposito', '?')}) ejecutado con "
                f"parámetros {_j(run.get('params'))}.\nEstado: {run['status']}\n"
                f"SALIDA:\n```json\n{_j(run.get('output'), 4500)}\n```\n"
                f"Verificación automática: cobertura {v.get('cobertura')}, filas crudas recibidas "
                f"{v.get('filas_recibidas')}, truncada {v.get('truncada')}, parcial sin declarar "
                f"{v.get('parcial_sin_declarar')}, necesidades {_j(v.get('necesidades'), 600)}.\n\n"
                "¿Esta medición responde realmente la pregunta? Sé crítico: un resumen que no distingue lo que la "
                "pregunta pide NO la responde.\n" + FORMATO_EVAL)

    # Pasos --------------------------------------------------------------------------------------------
    def _guardar(self, t, manifest, code, parent=None) -> dict:
        manifest = manifest or {}
        nombre = parent["name"] if parent else (manifest.get("nombre") or f"tarea_{t.id.lower()}")
        mods = {i["modulo"]: i["id"] for i in self.almacen.index()}
        libs = [mods[m] for m in manifest.get("bibliotecas") or [] if isinstance(m, str) and m in mods]
        saved = self.almacen.save(nombre, code, manifest, author=f"modelo:{self.modelo.nombre}",
                                  parent_id=parent["id"] if parent else None, libraries=libs)
        return self.almacen.get(saved["id"])

    def _medir(self, inst, params) -> dict:
        run = ejecutar(executor=self.executor, broker=self.broker, almacen=self.almacen, cfg=self.cfg,
                       instrument_id=inst["id"], params=params, schema=self.schema, budget=self.budget,
                       cancel=self.cancel)
        if run["status"] in ("bloqueado", "cancelado"):
            # Falla de infraestructura o pedido del operador: no es un error del modelo y no se le cobra.
            raise LaboratorioAbortado(f"ejecución {run['status']}: {run.get('error')}")
        acc = run.get("accounting") or {}
        v = run.get("verdict") or {}
        self.log(f"    {inst['modulo']}: {run['status']}" + (f" cobertura={v.get('cobertura')}" if v else "")
                 + (f" ({(run.get('error') or '')[:120]})" if run["status"] != "ok" else ""))
        return {"run_id": run["id"], "instrumento_id": inst["id"], "modulo": inst["modulo"],
                "version": inst["version"], "parametros": params, "estado": run["status"],
                "error": (run.get("error") or "")[:500] or None, "contrato": v.get("contrato"),
                "cobertura": v.get("cobertura"), "truncada": v.get("truncada"),
                "parcial_sin_declarar": v.get("parcial_sin_declarar"), "necesidades": len(v.get("necesidades") or []),
                "consultas": (acc.get("usado") or {}).get("consultas"), "filas": (acc.get("usado") or {}).get("filas"),
                "costo": run.get("cost"), "_run": run}

    def _evaluar(self, t, inst, run, reg) -> dict | None:
        text, call = self.modelo.pedir(f"evaluacion-{t.id}", self._eval_prompt(t, inst, run), json_out=True,
                                       cancel=self.cancel)
        data = parse_json(text) if text is not None else None
        call["formato_ok"] = isinstance(data, dict) and data.get("responde") in ("si", "parcial", "no")
        reg["llamadas"].append(call)
        if not call["formato_ok"]:
            reg["evaluaciones"].append({"formato_ok": False, "crudo": (text or "")[:300]})
            return None
        ev = {"responde": data["responde"], "motivo": str(data.get("motivo") or "")[:400],
              "respuesta": data.get("respuesta"), "mejora": str(data.get("mejora") or "")[:400]}
        reg["evaluaciones"].append(ev)
        self.log(f"    autoevaluación: {ev['responde']} — {ev['motivo'][:100]}")
        return ev

    def _ciclo(self, t, reg, inst, code, correcciones_usadas: int) -> tuple[dict | None, dict | None]:
        """Ejecutar → evaluar → corregir mientras queden correcciones. Devuelve (última evaluación, instrumento)."""
        ev = None
        while True:
            m = self._medir(inst, (inst["manifest"].get("parametros") or {}) if isinstance(inst["manifest"], dict)
                            else {})
            run = m.pop("_run")
            reg["versiones"].append(m)
            if m["estado"] == "ok":
                ev = self._evaluar(t, inst, run, reg)
                if ev is not None and ev["responde"] == "si":
                    return ev, inst
                que_paso = (f"El instrumento corrió, pero tu evaluación fue «{ev['responde']}»: {ev['motivo']}. "
                            f"Mejora propuesta: {ev['mejora']}. Salida: {_j(run.get('output'), 1500)}") if ev else \
                    f"Salida: {_j(run.get('output'), 1500)} (la evaluación no tuvo el formato pedido)"
            else:
                que_paso = f"Estado {m['estado']}: {m['error']}"
            if correcciones_usadas >= self.max_corr:
                return ev, inst
            correcciones_usadas += 1
            reg["correcciones"] += 1
            nuevo = self._escribir(t, reg, "correccion", self._correccion_prompt(t, code, que_paso))
            if nuevo is None:
                return ev, inst
            manifest, code = nuevo
            previo = inst["id"]
            inst = self._guardar(t, manifest, code, parent=inst)
            if inst["id"] == previo:
                reg["codigo_repetido"] = reg.get("codigo_repetido", 0) + 1  # la «corrección» no cambió nada

    def _escribir(self, t, reg, tipo, prompt) -> tuple[dict, str] | None:
        text, call = self.modelo.pedir(f"{tipo}-{t.id}", prompt, json_out=False, cancel=self.cancel)
        manifest, code, problems = parse_diseno(text) if text is not None else (None, None, ["sin respuesta"])
        call["formato_ok"] = code is not None and manifest is not None
        call["problemas"] = problems
        reg["llamadas"].append(call)
        if code is None:
            self.log(f"    {tipo}: sin código utilizable ({'; '.join(problems)})")
            return None
        return manifest or {"parametros": {}}, code

    def correr(self, t: T.Tarea, verdad: dict) -> dict:
        t0 = time.monotonic()
        reg = {"tarea": t.id, "titulo": t.titulo, "pregunta": T.pregunta(t, self.info), "llamadas": [],
               "versiones": [], "evaluaciones": [], "correcciones": 0, "verdad": verdad}
        self.log(f"  {t.id} — {t.titulo}")
        ev, inst = None, None
        if t.id == "R":
            ev, inst = self._reutilizar(t, reg)
        elif t.id == "C":
            inst = self._guardar(t, {"nombre": "resumen_lecturas", "proposito": "resumen global de lecturas",
                                     "parametros": {}}, T.INSUFICIENTE.lstrip())
            ev, inst = self._ciclo(t, reg, inst, T.INSUFICIENTE.lstrip(), 0)
            first = next((e for e in reg["evaluaciones"] if e.get("formato_ok", True)), None)
            reg["detecto_insuficiencia"] = bool(first and first["responde"] != "si")
        else:
            nuevo = None
            for intento in range(1 + self.max_corr):
                nuevo = self._escribir(t, reg, "diseno" if not intento else "diseno-reintento", self._diseno_prompt(t))
                if nuevo:
                    break
                reg["correcciones"] += 1
            if nuevo:
                inst = self._guardar(t, *nuevo)
                ev, inst = self._ciclo(t, reg, inst, nuevo[1], reg["correcciones"])
        if inst is not None:
            self.instrumento_de[t.id] = inst["id"]
        respuesta = ev.get("respuesta") if ev else None
        puntaje, detalle = t.puntuar(respuesta, verdad)
        reg.update({"respuesta_final": respuesta, "puntaje": puntaje, "detalle_puntaje": detalle,
                    "autoevaluacion_final": ev["responde"] if ev else None,
                    "calibracion": calibracion(ev["responde"] if ev else None, puntaje),
                    "correccion_exitosa": (puntaje >= 1) if reg["correcciones"] else None,
                    "segundos": round(time.monotonic() - t0, 2)})
        self.log(f"    puntaje {puntaje}: {detalle}")
        return reg

    def _reutilizar(self, t, reg):
        base_id = self.instrumento_de.get("T4")
        base = self.almacen.get(base_id) if base_id else None
        reg["reutilizo"] = False
        if base is None:
            reg["nota"] = "no hay instrumento de T4 para reutilizar"
            return None, None
        prompt = (self._contexto(t) + "\nNo escribas código nuevo: elegí un instrumento existente y proponé SÓLO los "
                  "parámetros para esta ejecución.\n"
                  'Respondé SÓLO con JSON: {"instrumento_id": <id>, "parametros": {...}, "motivo": "..."}')
        text, call = self.modelo.pedir(f"reutilizacion-{t.id}", prompt, json_out=True, cancel=self.cancel)
        data = parse_json(text) if text is not None else None
        call["formato_ok"] = isinstance(data, dict) and isinstance(data.get("parametros", {}), dict)
        reg["llamadas"].append(call)
        if not call["formato_ok"]:
            return None, None
        try:
            elegido = self.almacen.get(int(data.get("instrumento_id")))
        except (TypeError, ValueError):
            elegido = None
        reg["instrumento_elegido"] = elegido["modulo"] if elegido else data.get("instrumento_id")
        if elegido is None:
            return None, None
        reg["reutilizo"] = elegido["name"] == base["name"]
        params = data.get("parametros") or {}
        m = self._medir(elegido, params)
        run = m.pop("_run")
        reg["versiones"].append(m)
        if m["estado"] != "ok":
            return None, elegido
        return self._evaluar(t, elegido, run, reg), elegido


def resumen(registros: list[dict]) -> dict:
    """Métricas agregadas de una sesión (o de varias repeticiones de un mismo modelo)."""
    llamadas = [c for r in registros for c in r["llamadas"]]
    versiones = [v for r in registros for v in r["versiones"]]
    escritas = [c for c in llamadas if c["tipo"].startswith(("diseno", "correccion"))]
    cal = [r["calibracion"] for r in registros if r.get("calibracion")]
    corr = [r for r in registros if r.get("correccion_exitosa") is not None]
    n = len(registros) or 1

    def frac(xs, pred):
        return round(sum(1 for x in xs if pred(x)) / len(xs), 3) if xs else None

    return {
        "tareas": len(registros),
        "puntaje_medio": round(sum(r["puntaje"] for r in registros) / n, 3),
        "resueltas": sum(1 for r in registros if r["puntaje"] >= 1),
        "codigo_con_formato": frac(escritas, lambda c: c.get("formato_ok")),
        "evaluacion_con_formato": frac([c for c in llamadas if c["tipo"].startswith("evaluacion")],
                                       lambda c: c.get("formato_ok")),
        "correcciones_sin_cambios": sum(r.get("codigo_repetido", 0) for r in registros),
        "ejecuciones": len(versiones),
        "rechazadas_estatico": sum(1 for v in versiones if v["estado"] == "rechazado"),
        "ejecuciones_ok": frac(versiones, lambda v: v["estado"] == "ok"),
        "cobertura_demostrada": frac([v for v in versiones if v["estado"] == "ok"],
                                     lambda v: v["cobertura"] == "demostrada"),
        "parcial_sin_declarar": sum(1 for v in versiones if v.get("parcial_sin_declarar")),
        "autoevaluacion_calibrada": frac(cal, lambda c: c == "calibrada"),
        "sobreconfianza": sum(1 for c in cal if c == "sobreconfianza"),
        "correcciones": sum(r["correcciones"] for r in registros),
        "correccion_exitosa": frac(corr, lambda r: r["correccion_exitosa"]),
        "detecto_insuficiencia": next((r.get("detecto_insuficiencia") for r in registros if r["tarea"] == "C"), None),
        "reutilizo": next((r.get("reutilizo") for r in registros if r["tarea"] == "R"), None),
        "llamadas": len(llamadas),
        "llamadas_fallidas": sum(1 for c in llamadas if c["resultado"] not in ("completa", "truncada")),
        "tokens_prompt": sum(c.get("tokens_prompt", 0) for c in llamadas),
        "tokens_generados": sum(c.get("tokens_generados", 0) for c in llamadas),
        "segundos_modelo": round(sum(c.get("reloj_ms", 0) for c in llamadas) / 1000, 1),
        "segundos_totales": round(sum(r["segundos"] for r in registros), 1),
    }
