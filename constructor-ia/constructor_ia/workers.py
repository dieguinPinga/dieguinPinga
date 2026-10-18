"""Hilos de trabajo en segundo plano, independientes de las peticiones HTTP.

* MissionWorker: una misión pesada a la vez, tomada de la cola en SQLite.
* ChatWorker: genera respuestas de conversación pendientes.
Ambos comparten InferenceGate, así nunca hay dos inferencias simultáneas.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback

from . import prompts
from .config import Config
from .db import Database
from .engine import Engine
from .events import EventBus
from .files import FileStore
from .missions import Missions
from .ollama import Cancelled, InferenceGate, OllamaClient, OllamaError

log = logging.getLogger(__name__)


class MissionWorker(threading.Thread):
    def __init__(self, engine: Engine, missions: Missions, db: Database):
        super().__init__(name="mission-worker", daemon=True)
        self.engine, self.missions, self.db = engine, missions, db
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.current: str | None = None
        self.cancel = threading.Event()

    def notify(self) -> None:
        self.wake.set()

    def cancel_current(self, mid: str) -> None:
        if self.current == mid:
            self.cancel.set()

    def shutdown(self) -> None:
        self.stop_event.set()
        self.cancel.set()
        self.wake.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                mid = self.missions.claim_next(self.name)
            except Exception:  # noqa: BLE001
                log.exception("Error leyendo la cola")
                mid = None
            if mid is None:
                self.wake.wait(5)
                self.wake.clear()
                continue
            self.current = mid
            self.cancel = threading.Event()
            try:
                self.engine.run(mid, self.cancel)
            except Exception as e:  # noqa: BLE001
                log.exception("Fallo inesperado en la misión %s", mid)
                self.db.kv_set("last_coordinator_error", {"ts": time.time(), "mission": mid,
                                                          "error": f"{type(e).__name__}: {e}"})
                state = "interrumpida" if self.stop_event.is_set() else "fallida"
                try:
                    self.missions.set_state(mid, state, finished_at=time.time(),
                                            error=f"Error interno del coordinador: {type(e).__name__}")
                except Exception:  # noqa: BLE001
                    log.error(traceback.format_exc())
            finally:
                self.current = None
            if self.stop_event.is_set():
                # Abortada por apagado del servidor, no por el operador.
                row = self.missions.get(mid)
                if row and row["state"] == "cancelada" and not row["cancel_requested"]:
                    self.missions.set_state(mid, "interrumpida", error="Servidor detenido durante la misión")
        self.db.close()
        log.info("MissionWorker detenido")


class ChatWorker(threading.Thread):
    def __init__(self, cfg: Config, db: Database, bus: EventBus, files: FileStore, ollama: OllamaClient,
                 gate: InferenceGate):
        super().__init__(name="chat-worker", daemon=True)
        self.cfg, self.db, self.bus, self.files, self.ollama, self.gate = cfg, db, bus, files, ollama, gate
        self.stop_event = threading.Event()
        self.wake = threading.Event()

    def notify(self) -> None:
        self.wake.set()

    def shutdown(self) -> None:
        self.stop_event.set()
        self.wake.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            row = self.db.one("SELECT * FROM messages WHERE status='pendiente' AND role='assistant' ORDER BY id LIMIT 1")
            if row is None:
                self.wake.wait(5)
                self.wake.clear()
                continue
            try:
                self._answer(row)
            except Exception as e:  # noqa: BLE001
                log.exception("Fallo en respuesta de chat")
                self._finish(row, "error", "", error=f"Error interno: {type(e).__name__}")
        self.db.close()

    def _finish(self, row, status, content, error=None, meta=None):
        self.db.execute("UPDATE messages SET status=?, content=?, error=?, meta_json=COALESCE(?, meta_json),"
                        " finished_at=? WHERE id=?",
                        (status, content, error, json.dumps(meta, ensure_ascii=False) if meta else None,
                         time.time(), row["id"]))
        self.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (time.time(), row["conversation_id"]))
        self.bus.publish("chat", "respuesta lista" if status == "lista" else "error de chat",
                         conversation_id=row["conversation_id"], data={"message_id": row["id"], "status": status})

    def _context(self, row) -> list[dict]:
        conv = self.db.one("SELECT * FROM conversations WHERE id=?", (row["conversation_id"],))
        model = self.cfg.chat
        budget = prompts.budget_chars(model.num_ctx, model.num_predict)
        system = prompts.DRAFT_SYSTEM if row["kind"] == "borrador" else prompts.CHAT_SYSTEM
        if conv["mission_id"] and row["kind"] != "borrador":
            m = self.db.one("SELECT * FROM missions WHERE id=?", (conv["mission_id"],))
            if m:
                atts = self.db.all("SELECT n, test_status, passed, total, error FROM attempts WHERE mission_id=?"
                                   " ORDER BY n", (m["id"],))
                summary = "; ".join(f"intento {a['n']}: {a['test_status'] or '-'} {a['passed'] or 0}/{a['total'] or 0}"
                                    + (f" ({a['error'][:120]})" if a["error"] else "") for a in atts)
                system += (f"\n\nContexto de la misión {m['id']} «{m['title']}»: estado {m['state']}. "
                           f"Descripción: {prompts.clip(m['description'], 800)}. Intentos: {summary or 'ninguno'}."
                           + (f" Error: {m['error']}" if m["error"] else ""))
        history = self.db.all("SELECT role, content, kind, meta_json FROM messages WHERE conversation_id=? AND id<?"
                              " AND status IN ('lista') ORDER BY id DESC LIMIT 20",
                              (row["conversation_id"], row["id"]))
        msgs: list[dict] = []
        used = len(system)
        for h in history:
            content = h["content"]
            meta = json.loads(h["meta_json"]) if h["meta_json"] else {}
            if h["role"] == "user" and meta.get("file_id") and not msgs:
                # Solo el pedido de explicación más reciente adjunta el archivo completo.
                try:
                    info, data = self.files.read(int(meta["file_id"]))
                    content += (f"\n\nArchivo {info['rel_path']} de la misión {info['mission_id']}:\n```\n"
                                + prompts.clip(data.decode("utf-8", "replace"), budget // 2) + "\n```")
                except Exception as e:  # noqa: BLE001
                    content += f"\n\n(No se pudo adjuntar el archivo: {e})"
            if used + len(content) > budget:
                if not msgs:
                    content = prompts.clip(content, budget - used, tail=True)
                else:
                    break
            used += len(content)
            msgs.append({"role": h["role"], "content": content})
        msgs.reverse()
        return [{"role": "system", "content": system}] + msgs

    def _answer(self, row) -> None:
        cid = row["conversation_id"]
        self.db.execute("UPDATE messages SET status='generando' WHERE id=?", (row["id"],))
        self.bus.publish("chat", "generando respuesta", conversation_id=cid, data={"message_id": row["id"],
                                                                                   "status": "generando"})
        messages = self._context(row)
        cancel = self.stop_event
        try:
            self.gate.acquire("chat", cancel)
            try:
                gen = self.ollama.chat(self.cfg.chat, messages, cancel)
            finally:
                self.gate.release()
        except Cancelled:
            self.db.execute("UPDATE messages SET status='pendiente' WHERE id=?", (row["id"],))
            return
        except OllamaError as e:
            self._finish(row, "error", "", error=str(e))
            return
        text = gen.text + ("\n\n[respuesta cortada por el límite de tokens]" if gen.truncated else "")
        meta = {"ms": gen.duration_ms, "tokens": gen.eval_tokens}
        if row["kind"] == "borrador":
            draft = parse_draft(gen.text)
            if draft is None:
                self._finish(row, "lista", "No pude generar un borrador válido. Respuesta del modelo:\n\n" + text,
                             meta=meta)
                return
            meta["draft"] = draft
            text = ("Borrador de misión (revisá y corregí los casos antes de autorizar):\n\n"
                    f"Título: {draft['title']}\n\n{draft['description']}")
        self._finish(row, "lista", text, meta=meta)


def parse_draft(text: str) -> dict | None:
    """Extrae el JSON del borrador de forma tolerante; nunca lo ejecuta."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        d = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    mode = d.get("mode") if d.get("mode") in ("funcion", "script") else "funcion"
    cases = []
    for c in d.get("cases", [])[:10] if isinstance(d.get("cases"), list) else []:
        if not isinstance(c, dict):
            continue
        if mode == "script" and isinstance(c.get("stdin"), str) and isinstance(c.get("stdout"), str):
            cases.append({"stdin": c["stdin"][:2000], "stdout": c["stdout"][:2000], "visible": True})
        elif mode == "funcion" and isinstance(c.get("args"), list) and "expect" in c:
            cases.append({"args": c["args"], "expect": c["expect"], "visible": True})
    fn = d.get("function")
    return {
        "title": str(d.get("title") or "Misión")[:120],
        "description": str(d.get("description") or "")[:6000],
        "mode": mode,
        "function": fn if isinstance(fn, str) and fn.isidentifier() else "",
        "cases": cases,
    }
