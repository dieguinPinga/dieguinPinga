"""Transcripción asíncrona con faster-whisper, en un proceso aparte y con baja prioridad.

Si este proceso falla, se queda sin memoria o está ocupado, la captura no se entera."""
import gc
import logging
import os
import sys
import time

from . import db
from .config import Paths
from .util import mem_available_mb

log = logging.getLogger("worker")


def run(cfg):
    tc = cfg["transcription"]
    try:
        os.nice(int(tc["nice"]))
    except OSError:
        pass
    paths = Paths(cfg["general"]["data_dir"])
    conn = db.init(paths.db)
    conn.execute("UPDATE events SET transcript_status='pending' WHERE transcript_status='processing'")
    if not tc["enabled"]:
        db.set_status(conn, "worker", "deshabilitado", {})
        log.info("Transcripción deshabilitada; el worker queda inactivo")
        while True:
            time.sleep(3600)
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        msg = f"faster-whisper no disponible en {sys.executable}: {e}"
        log.error(msg)
        while True:
            db.set_status(conn, "worker", "error", {"error": msg})
            time.sleep(10)

    model, last_used, done = None, 0.0, 0
    while True:
        avail = mem_available_mb()
        detail = {"modelo": tc["model"], "cargado": model is not None, "mem_libre_mb": avail,
                  "transcriptos": done, "pid": os.getpid()}
        row = conn.execute("SELECT e.id, m.relpath FROM events e JOIN media m ON m.event_id = e.id AND m.kind = 'audio' "
                           "WHERE e.transcript_status = 'pending' AND e.status = 'ready' "
                           "ORDER BY e.id DESC LIMIT 1").fetchone()
        if row is None:
            if model is not None and time.time() - last_used > tc["unload_after_s"]:
                model = None
                gc.collect()
                log.info("Modelo descargado de memoria por inactividad")
            db.set_status(conn, "worker", "ok", {**detail, "estado": "en espera"})
            time.sleep(5)
            continue
        if model is None and avail is not None and avail < tc["min_mem_available_mb"]:
            db.set_status(conn, "worker", "ok", {**detail, "estado": "esperando memoria libre"})
            time.sleep(30)
            continue
        eid = row["id"]
        db.update_event(conn, eid, transcript_status="processing")
        db.set_status(conn, "worker", "ok", {**detail, "estado": f"transcribiendo evento {eid}"})
        try:
            if model is None:
                log.info("Cargando Whisper %s (%s)", tc["model"], tc["compute_type"])
                model = WhisperModel(tc["model"], device="cpu", compute_type=tc["compute_type"],
                                     cpu_threads=int(tc["cpu_threads"]))
            t0 = time.time()
            segments, info = model.transcribe(paths.abs(row["relpath"]), language=tc["language"] or None,
                                              beam_size=1, vad_filter=True)
            text = " ".join(s.text.strip() for s in segments).strip()
            db.update_event(conn, eid, transcript=text or None, transcript_lang=info.language,
                            transcript_status="done" if text else "empty")
            done += 1
            log.info("Evento %d transcripto en %.1f s (%d caracteres)", eid, time.time() - t0, len(text))
        except Exception as e:
            log.exception("Error transcribiendo evento %d", eid)
            db.update_event(conn, eid, transcript_status="error", error=f"transcripción: {e}"[:500])
        last_used = time.time()
