"""Ensamblado de componentes y ciclo de vida del servicio."""
from __future__ import annotations

import logging
import os
import threading
import time

from . import VERSION
from .auth import Auth
from .config import Config
from .db import Database
from .engine import Engine
from .events import EventBus
from .files import FileStore
from .missions import Missions
from .ollama import InferenceGate, OllamaClient, OllamaError
from .sandbox.executor import Executor
from .workers import ChatWorker, MissionWorker

log = logging.getLogger(__name__)


class App:
    def __init__(self, cfg: Config, executor: Executor | None = None, ollama: OllamaClient | None = None):
        self.cfg = cfg
        self.started_at = time.time()
        for d in (cfg.data_dir, cfg.missions_dir, cfg.runs_dir):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(d, 0o700)
        self.db = Database(cfg.db_path)
        self.db.migrate()
        self.bus = EventBus(self.db, cfg.max_events_per_mission)
        self.auth = Auth(self.db, cfg.session_idle_hours, cfg.session_max_days)
        self.missions = Missions(self.db, self.bus)
        self.files = FileStore(self.db, cfg.missions_dir)
        self.ollama = ollama or OllamaClient(cfg.ollama_url)
        self.gate = InferenceGate()
        self.executor = executor or Executor(cfg)
        self.engine = Engine(cfg, self.db, self.bus, self.missions, self.files, self.ollama, self.gate,
                             self.executor)
        from .explorer.agent import Explorer
        from .explorer.broker import BrokerRegistry

        self.brokers = BrokerRegistry(cfg, self.db)
        if cfg.explorer_enabled:
            self.engine.explorer = Explorer(cfg, self.db, self.bus, self.missions, self.files, self.ollama, self.gate,
                                            self.brokers)
        self.mission_worker = MissionWorker(self.engine, self.missions, self.db)
        self.chat_worker = ChatWorker(cfg, self.db, self.bus, self.files, self.ollama, self.gate)
        self._status_cache: tuple[float, dict] = (0.0, {})
        self._status_lock = threading.Lock()
        self._maint_stop = threading.Event()
        from .web.preview import PreviewTokens

        self.previews = PreviewTokens(cfg.web_preview_minutes)
        self.preview_available = False  # True solo cuando el servidor de vista previa está escuchando

    def start(self, run_selftest: bool = True) -> None:
        stale = self.executor.limiter.cleanup_stale()
        if stale:
            log.warning("Unidades de sandbox huérfanas detenidas: %s", stale)
        recovered = self.missions.recover_interrupted()
        if recovered:
            log.warning("Misiones interrumpidas por reinicio: %s", recovered)
        self.db.execute("UPDATE messages SET status='pendiente' WHERE status='generando'")
        for p in self.cfg.runs_dir.iterdir():
            if p.name.startswith("run-"):
                Executor._remove_dir(p)
        if run_selftest:
            report = self.executor.self_test()
            self.db.kv_set("sandbox_selftest", report)
            if report["ok"]:
                log.info("Autodiagnóstico del aislamiento: correcto")
            else:
                log.error("Ejecución BLOQUEADA: %s", self.executor.disabled_reason)
        if self.cfg.explorer_enabled and self.brokers.brokers:
            threading.Thread(target=self.brokers.verify_all, name="verify-db", daemon=True).start()
        self.mission_worker.start()
        self.chat_worker.start()
        threading.Thread(target=self._maintenance, name="maintenance", daemon=True).start()
        self.db.audit("servidor_iniciado", detail=f"version {VERSION}")

    def stop(self) -> None:
        self._maint_stop.set()
        self.mission_worker.shutdown()
        self.chat_worker.shutdown()
        self.mission_worker.join(30)
        self.chat_worker.join(10)
        self.executor.limiter.cleanup_stale()
        self.db.audit("servidor_detenido")

    def _maintenance(self) -> None:
        while not self._maint_stop.wait(3600):
            try:
                n = self.bus.purge_older_than(self.cfg.event_retention_days)
                self.db.execute("DELETE FROM audit WHERE ts < ?", (time.time() - 365 * 86400,))
                self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
                if n:
                    log.info("Retención: %d eventos antiguos eliminados", n)
            except Exception:  # noqa: BLE001
                log.exception("Error en mantenimiento")

    # Estado del sistema (sin secretos) ----------------------------------
    def status(self) -> dict:
        with self._status_lock:
            ts, cached = self._status_cache
            if time.time() - ts < 10:
                return dict(cached, queue=self.missions.counts(), running=self.mission_worker.current,
                            inference=self.gate.holder)
            st = self._collect_status()
            self._status_cache = (time.time(), st)
            return st

    def _collect_status(self) -> dict:
        oll: dict = {"url": self.cfg.ollama_url}
        try:
            tags = self.ollama.tags()
            oll.update(available=True, version=self.ollama.version(), loaded=self.ollama.loaded(),
                       builder_present=self.cfg.builder.name in tags, chat_present=self.cfg.chat.name in tags)
        except OllamaError as e:
            oll.update(available=False, error=str(e))
        mem = {}
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    k, v = line.split(":", 1)
                    if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                        mem[k] = int(v.split()[0]) * 1024
        except OSError:
            pass
        try:
            load = os.getloadavg()
        except OSError:
            load = (0, 0, 0)
        st = self.executor.selftest_report or {}
        return {
            "version": VERSION, "uptime_s": int(time.time() - self.started_at),
            "ollama": oll, "models": {"constructor": self.cfg.builder.name, "chat": self.cfg.chat.name},
            "memory": mem, "load": [round(x, 2) for x in load], "cpus": os.cpu_count(),
            "queue": self.missions.counts(), "running": self.mission_worker.current, "inference": self.gate.holder,
            "last_error": self.db.kv_get("last_coordinator_error"),
            "sandbox": {"enabled": self.executor.enabled, "reason": self.executor.disabled_reason,
                        "limiter": self.executor.limiter.name,
                        "checks": st.get("checks", []), "ts": st.get("ts"),
                        "limits": {"memory_mb": self.cfg.sandbox_memory_mb, "tasks": self.cfg.sandbox_tasks,
                                   "cpu_percent": self.cfg.sandbox_cpu_percent,
                                   "timeout_s": self.cfg.sandbox_timeout_s}},
            "workers": {"missions": self.mission_worker.is_alive(), "chat": self.chat_worker.is_alive()},
            "web": {"enabled": self.cfg.web_enabled, "preview": self.preview_available,
                    "preview_port": self.cfg.web_preview_port, "model": self.cfg.web_model.name,
                    "node_check": os.path.exists(self.cfg.web_node)},
        }
