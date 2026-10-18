"""Motor de programación: objetivo → código → pruebas → resultados → reparación.

Una misión se aprueba ÚNICAMENTE si el arnés del sandbox devuelve un resultado
firmado con todas las pruebas independientes superadas.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from . import codeextract, prompts, webproject
from .config import Config
from .db import Database
from .events import EventBus
from .files import FileStore
from .missions import Missions
from .ollama import Cancelled, InferenceGate, OllamaClient, OllamaError
from .sandbox.executor import Executor

log = logging.getLogger(__name__)


def _change_summary(base: dict, final: dict) -> str:
    added = sorted(set(final) - set(base))
    removed = sorted(set(base) - set(final))
    modified = sorted(p for p in set(base) & set(final) if base[p] != final[p])
    parts = [f"modificados: {', '.join(modified) or 'ninguno'}"]
    if added:
        parts.append(f"agregados: {', '.join(added)}")
    if removed:
        parts.append(f"eliminados: {', '.join(removed)}")
    parts.append(f"sin cambios: {len(set(base) & set(final)) - len(modified)}")
    return "; ".join(parts)


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
        self.explorer = None  # lo asigna App si el Explorador está habilitado
        self.revisions = None  # lo asigna App

    def _base_files(self, mid: str) -> dict[str, str]:
        return self.revisions.base_files(mid) if self.revisions is not None else {}

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
        if spec.get("mode") == "exploracion":
            # La exploración no ejecuta código generado: no depende del sandbox, sí del intermediario SQL.
            if self.explorer is None:
                self.missions.set_state(mid, "fallida", finished_at=time.time(), error="Explorador desactivado")
                return "fallida"
            return self.explorer.run(mid, spec, description, cancel)
        try:
            self._check_cancel(mid, cancel)
            if not self.executor.enabled:
                raise MissionAborted("fallida", "Ejecución aislada bloqueada: " + self.executor.disabled_reason)
            self.files.save(mid, "pruebas/especificacion.json",
                            json.dumps({"spec": spec, "tests_py": tests_py}, ensure_ascii=False, indent=2),
                            "pruebas")
            self._event(mid, "preparada", f"Especificación guardada: {len(spec.get('cases', spec.get('checks', [])))} "
                        + ("requisitos web" if spec.get("mode") == "web" else "casos")
                        + (" + pruebas avanzadas" if tests_py else ""))
            if spec.get("mode") == "web":
                return self._run_web(mid, description, spec, max_attempts, cancel)
            budget = prompts.budget_chars(self.cfg.builder.num_ctx, self.cfg.builder.num_predict)
            last_code: str | None = None
            base_code = None
            if spec.get("revision"):
                base_code = self._base_files(mid).get("solucion.py")
                if base_code is None:
                    raise MissionAborted("fallida", "La revisión no tiene la copia de su versión base")
                last_code = base_code
                self._event(mid, "revision", f"Revisión r{self.missions.get(mid)['revision']}: se parte de "
                            f"{spec['revision']['base_mission']}:{spec['revision']['base_ref']} "
                            f"con la instrucción «{spec['revision']['instruction'][:200]}»")
            report = ""
            for n in range(1, max_attempts + 1):
                self._check_cancel(mid, cancel)
                outcome = self._attempt(mid, n, max_attempts, description, spec, tests_py, last_code, report,
                                        budget, cancel)
                if outcome["approved"]:
                    if base_code is not None:
                        changed = "modificado" if outcome["code"] != base_code else "sin cambios"
                        self._event(mid, "revision", f"Cambios respecto de la base: solucion.py {changed}")
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

        if n == 1 and spec.get("revision") and last_code is not None:
            messages = prompts.revision(description, spec, last_code, budget)
        elif n == 1 or last_code is None:
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

    # --- Taller Web ---------------------------------------------------------
    def _run_web(self, mid, description, spec, max_attempts, cancel) -> str:
        if not self.cfg.web_enabled:
            raise MissionAborted("fallida", "El Taller Web está desactivado en la configuración")
        model = self.cfg.web_model
        budget = prompts.budget_chars(model.num_ctx, model.num_predict)
        files: dict[str, str] = {}
        if spec.get("revision"):
            files = self._base_files(mid)
            if not files:
                raise MissionAborted("fallida", "La revisión no tiene la copia de su versión base")
            self._event(mid, "revision", f"Revisión r{self.missions.get(mid)['revision']}: se parte de "
                        f"{spec['revision']['base_mission']}:{spec['revision']['base_ref']} ({len(files)} archivos) "
                        f"con la instrucción «{spec['revision']['instruction'][:200]}»")
        base_files = dict(files)
        files.update(spec.get("seed_files") or {})
        result, report = None, ""
        for n in range(1, max_attempts + 1):
            self._check_cancel(mid, cancel)
            out = self._attempt_web(mid, n, max_attempts, description, spec, files, result, report, budget, cancel)
            if out["approved"]:
                if spec.get("revision"):
                    self._event(mid, "revision", "Cambios respecto de la base: " + _change_summary(base_files, out["files"]))
                self.missions.set_state(mid, "aprobada", finished_at=time.time(), final_attempt=n, error=None)
                self._event(mid, "terminada", f"Proyecto web aprobado en el intento {n}: todas las verificaciones pasaron")
                return "aprobada"
            files, result, report = out["files"], out["result"], out["report"]
        raise MissionAborted("fallida", f"No superó las verificaciones en {max_attempts} intentos")

    def _attempt_web(self, mid, n, max_attempts, description, spec, prev_files, prev_result, report, budget, cancel):
        cfg, model = self.cfg, self.cfg.web_model
        self.missions.set_state(mid, "generando" if n == 1 else "corrigiendo", attempt=n)
        self.db.execute("INSERT INTO attempts(mission_id, n, started_at) VALUES(?,?,?)", (mid, n, time.time()))

        def update(**f):
            cols = ", ".join(f"{k}=?" for k in f)
            self.db.execute(f"UPDATE attempts SET {cols} WHERE mission_id=? AND n=?", (*f.values(), mid, n))

        seeds = spec.get("seed_files") or {}
        if n == 1 and spec.get("revision"):
            messages = webproject.revision_prompt(description, spec, prev_files, budget)
        elif not set(prev_files) - set(seeds):
            messages = webproject.initial_prompt(description, spec, budget)
            if report:
                messages[1]["content"] += "\n\nEl intento anterior falló:\n" + report
        else:
            focus = webproject.failing_files(prev_result, prev_files)
            messages = webproject.repair_prompt(description, spec, prev_files, report, focus, budget)
        self._event(mid, "generacion", f"Intento {n}/{max_attempts}: generación del proyecto web con {model.name}"
                    + (f" (corrigiendo sobre {len(prev_files)} archivos existentes)" if prev_files else ""))
        try:
            self.gate.acquire(f"mision {mid}", cancel)
            try:
                gen = self.ollama.chat(model, messages, cancel,
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
            return {"approved": False, "files": prev_files, "result": prev_result,
                    "report": f"La generación anterior falló: {e}. Devuelve menos archivos o archivos más cortos."}
        self.files.save(mid, f"intento-{n}/respuesta_modelo.md", gen.text, "respuesta", n)
        update(gen_ms=gen.duration_ms, prompt_tokens=gen.prompt_tokens, eval_tokens=gen.eval_tokens,
               done_reason=gen.done_reason)
        self._check_cancel(mid, cancel)

        changed, deleted, problems = webproject.extract_files(gen.text, cfg.web_max_files, cfg.web_max_file_kb * 1024)
        if not changed and not deleted and not problems:
            problems.append("No se encontraron archivos con el formato «### archivo: ruta» seguido de un bloque de código")
        touched = sorted(set(seeds) & (set(changed) | set(deleted)))
        if touched:
            self._event(mid, "aviso", "Se ignoraron cambios a datos de solo lectura: " + ", ".join(touched))
        files = webproject.merge(prev_files, changed, deleted)
        files.update(seeds)  # las instantáneas de datos son inmutables
        problems += webproject.check_limits(files, spec.get("entry", "index.html"), cfg.web_max_files,
                                            cfg.web_max_total_kb * 1024)
        if gen.truncated:
            problems.insert(0, "La respuesta se cortó por el límite de tokens: devuelve menos archivos o más cortos.")
        unchanged = sorted(p for p in changed if prev_files.get(p) == changed[p])
        reused = sorted(set(files) - set(changed))
        self._event(mid, "codigo_recibido",
                    f"Respuesta en {gen.duration_ms / 1000:.1f} s: {len(changed)} archivo(s) nuevos o modificados"
                    + (f" ({len(unchanged)} reenviados sin cambios)" if unchanged else "")
                    + (f", {len(reused)} reutilizados sin regenerar" if reused else "")
                    + (f", {len(deleted)} eliminados" if deleted else ""))
        for path, content in files.items():
            self.files.save(mid, f"intento-{n}/web/{path}", content, "web", n)
        if problems:
            update(finished_at=time.time(), syntax_ok=0, test_status="no_ejecutadas", error="; ".join(problems)[:1000])
            self._event(mid, "validacion", "Proyecto incompleto: " + "; ".join(problems)[:500], ok=False)
            return {"approved": False, "files": files or prev_files, "result": prev_result,
                    "report": webproject.failure_report(None, problems, 2500)}
        update(syntax_ok=1)

        self.missions.set_state(mid, "probando", attempt=n)
        self._event(mid, "pruebas", f"Verificación de {len(files)} archivos en el ejecutor aislado "
                                    "(estructura, sintaxis, referencias, requisitos)")
        res = self.executor.run("", spec, None, cancel, files=files)
        update(sandbox_json=json.dumps(res.summary()))
        if res.status == "cancelled":
            update(finished_at=time.time(), test_status="cancelada")
            raise MissionAborted("cancelada", "Cancelada durante las pruebas")
        if res.status == "blocked":
            update(finished_at=time.time(), test_status="bloqueada", error=res.reason)
            raise MissionAborted("fallida", res.reason)
        if res.status != "ok":
            update(finished_at=time.time(), test_status=res.status, error=res.reason)
            self._event(mid, "pruebas_fallidas", f"Verificación {res.status}: {res.reason}", ok=False)
            return {"approved": False, "files": files, "result": None,
                    "report": f"La verificación terminó con '{res.status}': {res.reason}. Reduce el tamaño de los archivos."}
        result = res.result
        self.files.save(mid, f"intento-{n}/resultado.json", json.dumps(result, ensure_ascii=False, indent=2),
                        "resultado", n)
        passed, failed, total = result.get("passed", 0), result.get("failed", 0), result.get("total", 0)
        approved = total > 0 and failed == 0 and passed == total
        update(finished_at=time.time(), test_status="aprobadas" if approved else "fallidas",
               passed=passed, failed=failed, total=total, result_json=json.dumps(result)[:200_000])
        if approved:
            for path, content in files.items():
                self.files.save(mid, f"final/web/{path}", content, "final", n)
            self._event(mid, "pruebas_aprobadas", f"Verificaciones aprobadas: {passed}/{total}", ok=True)
            return {"approved": True, "files": files, "result": result, "report": ""}
        self._event(mid, "pruebas_fallidas", f"Verificaciones fallidas: {failed} de {total}", ok=False)
        if n < max_attempts:
            self._event(mid, "reparacion", "Se enviarán los errores reales y los archivos actuales al modelo")
        return {"approved": False, "files": files, "result": result,
                "report": webproject.failure_report(result, [], 2500)}
