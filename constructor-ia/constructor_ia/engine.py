"""Motor de programación: objetivo → código → pruebas → resultados → reparación.

Una misión se aprueba ÚNICAMENTE si el arnés del sandbox devuelve un resultado
firmado con todas las pruebas independientes superadas.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from . import codeextract, prompts
from .config import Config
from .db import Database
from .events import EventBus
from .files import FileStore
from .missions import Missions
from .ollama import Cancelled, InferenceGate, OllamaClient, OllamaError
from .sandbox.executor import Executor

log = logging.getLogger(__name__)


class MissionAborted(Exception):
    def __init__(self, state: str, reason: str):
        super().__init__(reason)
        self.state = state
        self.reason = reason


class Engine:
    def __init__(self, cfg: Config, db: Database, bus: EventBus, missions: Missions, files: FileStore,
                 ollama: OllamaClient, gate: InferenceGate, executor: Executor):
        self.cfg, self.db, self.bus, self.missions, self.files = cfg, db, bus, missions, files
        self.ollama, self.gate, self.executor = ollama, gate, executor

    def _event(self, mid, kind, msg, **data):
        self.bus.publish(kind, msg, mission_id=mid, data=data or None)

    def _check_cancel(self, mid: str, cancel: threading.Event) -> None:
        if cancel.is_set() or self.missions.cancel_requested(mid):
            cancel.set()
            raise MissionAborted("cancelada", "Cancelada por el operador")

    def run(self, mid: str, cancel: threading.Event) -> str:
        row = self.missions.get(mid)
        spec = json.loads(row["spec_json"])
        tests_py = spec.pop("tests_py", "")
        description = row["description"]
        max_attempts = row["max_attempts"]
        try:
            self._check_cancel(mid, cancel)
            if not self.executor.enabled:
                raise MissionAborted("fallida", "Ejecución aislada bloqueada: " + self.executor.disabled_reason)
            self.files.save(mid, "pruebas/especificacion.json",
                            json.dumps({"spec": spec, "tests_py": tests_py}, ensure_ascii=False, indent=2),
                            "pruebas")
            self._event(mid, "preparada", f"Especificación guardada: {len(spec.get('cases', []))} casos"
                        + (" + pruebas avanzadas" if tests_py else ""))
            budget = prompts.budget_chars(self.cfg.builder.num_ctx, self.cfg.builder.num_predict)
            last_code: str | None = None
            report = ""
            for n in range(1, max_attempts + 1):
                self._check_cancel(mid, cancel)
                outcome = self._attempt(mid, n, max_attempts, description, spec, tests_py, last_code, report,
                                        budget, cancel)
                if outcome["approved"]:
                    self.missions.set_state(mid, "aprobada", finished_at=time.time(), final_attempt=n, error=None)
                    self._event(mid, "terminada", f"Misión aprobada en el intento {n}: todas las pruebas pasaron")
                    return "aprobada"
                last_code = outcome["code"] or last_code
                report = outcome["report"]
            raise MissionAborted("fallida", f"No superó las pruebas en {max_attempts} intentos")
        except MissionAborted as e:
            self.missions.set_state(mid, e.state, finished_at=time.time(), error=e.reason)
            self._event(mid, "terminada", f"Misión {e.state}: {e.reason}")
            return e.state

    def _attempt(self, mid, n, max_attempts, description, spec, tests_py, last_code, report, budget, cancel):
        self.missions.set_state(mid, "generando" if n == 1 else "corrigiendo", attempt=n)
        self.db.execute("INSERT INTO attempts(mission_id, n, started_at) VALUES(?,?,?)", (mid, n, time.time()))

        def update(**f):
            cols = ", ".join(f"{k}=?" for k in f)
            self.db.execute(f"UPDATE attempts SET {cols} WHERE mission_id=? AND n=?", (*f.values(), mid, n))

        if n == 1 or last_code is None:
            messages = prompts.initial(description, spec, budget)
            if report:
                messages[1]["content"] += "\n\nEl intento anterior falló:\n" + report
        else:
            messages = prompts.repair(description, spec, last_code, report, budget)
        self._event(mid, "generacion", f"Intento {n}/{max_attempts}: generación iniciada con {self.cfg.builder.name}")
        try:
            self.gate.acquire(f"mision {mid}", cancel)
            try:
                gen = self.ollama.chat(self.cfg.builder, messages, cancel,
                                       on_progress=lambda size: self._event(mid, "progreso",
                                                                            f"Generando… {size} caracteres"))
            finally:
                self.gate.release()
        except Cancelled:
            update(finished_at=time.time(), error="cancelada")
            raise MissionAborted("cancelada", "Cancelada durante la generación") from None
        except OllamaError as e:
            update(finished_at=time.time(), error=str(e))
            self.db.kv_set("last_coordinator_error", {"ts": time.time(), "mission": mid, "error": str(e)})
            if "contactar" in str(e) or "no disponible" in str(e):
                raise MissionAborted("fallida", f"Ollama CPU no disponible: {e}") from None
            self._event(mid, "error", f"Error de generación: {e}")
            return {"approved": False, "code": None, "report": f"La generación anterior falló: {e}. Sé más conciso."}
        self.files.save(mid, f"intento-{n}/respuesta_modelo.md", gen.text, "respuesta", n)
        update(gen_ms=gen.duration_ms, prompt_tokens=gen.prompt_tokens, eval_tokens=gen.eval_tokens,
               done_reason=gen.done_reason)
        self._event(mid, "codigo_recibido",
                    f"Respuesta recibida en {gen.duration_ms / 1000:.1f} s"
                    + (f" ({gen.eval_tokens} tokens)" if gen.eval_tokens else "")
                    + (" — INCOMPLETA (límite de tokens)" if gen.truncated else ""))
        self._check_cancel(mid, cancel)

        code, note = codeextract.extract_code(gen.text)
        if code is None:
            problems = [note]
        else:
            self.files.save(mid, f"intento-{n}/solucion.py", code, "codigo", n)
            if note:
                self._event(mid, "aviso", note)
            problems = codeextract.validate_code(code, spec)
        if problems:
            if gen.truncated:
                problems.insert(0, "La respuesta se cortó por el límite de tokens: escribe una solución más corta.")
            update(finished_at=time.time(), syntax_ok=0 if code is not None else None, test_status="no_ejecutadas",
                   error="; ".join(problems)[:1000])
            self._event(mid, "validacion", "Validación estática fallida: " + "; ".join(problems)[:500], ok=False)
            return {"approved": False, "code": code, "report": prompts.failure_report(None, problems, 2500)}
        update(syntax_ok=1)
        self._event(mid, "validacion", "Validación estática correcta (sintaxis y estructura)", ok=True)

        self.missions.set_state(mid, "probando", attempt=n)
        self._event(mid, "pruebas", "Pruebas iniciadas en el ejecutor aislado")
        res = self.executor.run(code, spec, tests_py, cancel)
        update(sandbox_json=json.dumps(res.summary()))
        if res.status == "cancelled":
            update(finished_at=time.time(), test_status="cancelada")
            raise MissionAborted("cancelada", "Cancelada durante las pruebas")
        if res.status == "blocked":
            update(finished_at=time.time(), test_status="bloqueada", error=res.reason)
            raise MissionAborted("fallida", res.reason)
        if res.status != "ok":
            update(finished_at=time.time(), test_status=res.status, error=res.reason)
            self.files.save(mid, f"intento-{n}/resultado.json",
                            json.dumps({"status": res.status, "reason": res.reason, "stderr": res.stderr},
                                       ensure_ascii=False, indent=2), "resultado", n)
            self._event(mid, "pruebas_fallidas", f"Ejecución {res.status}: {res.reason}", ok=False)
            return {"approved": False, "code": code,
                    "report": f"La ejecución de las pruebas terminó con '{res.status}': {res.reason}. "
                              "Revisa bucles infinitos, recursión excesiva o uso excesivo de memoria."}
        result = res.result
        self.files.save(mid, f"intento-{n}/resultado.json", json.dumps(result, ensure_ascii=False, indent=2),
                        "resultado", n)
        passed, failed, total = result.get("passed", 0), result.get("failed", 0), result.get("total", 0)
        approved = total > 0 and failed == 0 and passed == total
        update(finished_at=time.time(), test_status="aprobadas" if approved else "fallidas",
               passed=passed, failed=failed, total=total, result_json=json.dumps(result)[:200_000])
        if approved:
            self.files.save(mid, "final/solucion.py", code, "final", n)
            self._event(mid, "pruebas_aprobadas", f"Pruebas aprobadas: {passed}/{total}", ok=True)
            return {"approved": True, "code": code, "report": ""}
        self._event(mid, "pruebas_fallidas", f"Pruebas fallidas: {failed} de {total}", ok=False)
        if n < max_attempts:
            self._event(mid, "reparacion", "Se enviarán los errores reales al modelo para un nuevo intento")
        return {"approved": False, "code": code, "report": prompts.failure_report(result, [], 2500)}
