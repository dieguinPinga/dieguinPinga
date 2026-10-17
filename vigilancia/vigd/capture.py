"""Proceso de captura: audio + video + eventos + limpieza. No depende de Whisper ni Ollama."""
import fcntl
import logging
import os
import shutil
import signal
import threading
import time

from . import db
from .audio import AudioCapture
from .config import Paths
from .events import EventManager, Finalizer
from .video import SegmentStore, VideoCapture

log = logging.getLogger("capture")


def housekeeping(conn, paths, cfg):
    """Retención por antigüedad y protección del disco. Nunca borra favoritos ni fuera del directorio."""
    g = cfg["general"]
    cutoff = time.time() - g["retention_days"] * 86400
    old = conn.execute("SELECT id FROM events WHERE started_at < ? AND starred = 0 "
                       "AND status NOT IN ('recording','processing')", (cutoff,)).fetchall()
    freed = sum(db.delete_event(conn, paths, r["id"]) for r in old)
    if old:
        log.info("Retención: %d evento(s) borrados (%.1f MB)", len(old), freed / 1e6)

    def over_limit():
        free_gb = shutil.disk_usage(paths.root).free / 1e9
        used_gb = (conn.execute("SELECT COALESCE(SUM(bytes),0) FROM media").fetchone()[0]) / 1e9
        return free_gb < g["min_free_gb"] or used_gb > g["max_storage_gb"]

    n = 0
    while over_limit():
        rows = conn.execute("SELECT id FROM events WHERE starred = 0 AND status NOT IN ('recording','processing') "
                            "ORDER BY id LIMIT 20").fetchall()
        if not rows:
            log.error("Disco bajo el mínimo y no quedan eventos borrables (sólo favoritos o ajenos)")
            break
        for r in rows:
            db.delete_event(conn, paths, r["id"])
        n += len(rows)
    if n:
        log.warning("Protección de disco: %d evento(s) antiguos borrados", n)
    for kind in ("audio", "video", "thumbs"):
        base = os.path.join(paths.root, kind)
        for d in os.listdir(base) if os.path.isdir(base) else []:
            p = os.path.join(base, d)
            try:
                if os.path.isdir(p) and not os.listdir(p):
                    os.rmdir(p)
            except OSError:
                pass


def run(cfg):
    paths = Paths(cfg["general"]["data_dir"])
    paths.ensure()
    lock = open(os.path.join(paths.root, "capture.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Ya hay otra captura corriendo sobre este directorio de datos")

    conn = db.init(paths.db)
    db.recover_interrupted(conn)
    for f in os.listdir(paths.tmp):
        try:
            os.remove(os.path.join(paths.tmp, f))
        except OSError:
            pass

    vc = cfg["video"]
    store = SegmentStore(paths.segments, vc["segment_seconds"], vc["buffer_seconds"]) if vc["enabled"] else None
    if store:
        store.clear()
    finalizer = Finalizer(cfg, paths, store)
    holder = {}
    audio = AudioCapture(cfg["audio"], lambda t, i: holder["em"].on_audio(t, i)) if cfg["audio"]["enabled"] else None
    em = EventManager(cfg, conn, audio, store, finalizer)
    holder["em"] = em
    video = VideoCapture(vc, store, em.on_motion, em.on_light) if store else None

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    finalizer.start()
    for th in (audio, video):
        if th:
            th.start()
    log.info("Captura en marcha. Datos en %s", paths.root)

    last_status = last_seg = 0.0
    last_hk = time.time() - 540  # primera limpieza al minuto de arrancar
    while not stop.is_set():
        now = time.time()
        try:
            em.tick()
            for th in (audio, video):
                if th:
                    th.watchdog(now)
            if store and now - last_seg >= 5:
                store.cleanup(now)
                last_seg = now
            if now - last_status >= 2:
                if audio:
                    s = audio.status(); db.set_status(conn, "audio", s.pop("state"), s)
                else:
                    db.set_status(conn, "audio", "deshabilitado")
                if video:
                    s = video.status(); db.set_status(conn, "video", s.pop("state"), s)
                else:
                    db.set_status(conn, "video", "deshabilitado")
                du = shutil.disk_usage(paths.root)
                db.set_status(conn, "captura", "ok", {
                    "evento_abierto": em.current_id(), "finalizando": finalizer.pending(),
                    "disco_libre_gb": round(du.free / 1e9, 1), "pid": os.getpid()})
                last_status = now
            if now - last_hk >= 600:
                housekeeping(conn, paths, cfg)
                last_hk = now
        except Exception:
            log.exception("Error en el ciclo principal de captura")
        stop.wait(0.5)

    log.info("Deteniendo captura...")
    em.shutdown()
    for th in (audio, video):
        if th:
            th.stop()
    finalizer.submit(None)
    finalizer.join(timeout=60)
    db.set_status(conn, "captura", "detenido", {})
    db.set_status(conn, "audio", "detenido", {})
    db.set_status(conn, "video", "detenido", {})
    log.info("Captura detenida")
