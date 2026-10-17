"""Correlación de disparadores en eventos y finalización (archivos + base de datos)."""
import json
import logging
import os
import queue
import secrets
import threading
import time
import urllib.request
from dataclasses import dataclass, field

from . import db, media
from .audio import FRAME_S, pcm_stats, write_pcm

log = logging.getLogger("events")


@dataclass
class OpenEvent:
    id: int
    uid: str
    start: float
    first_trigger: float
    last_trigger: float
    recorder: object = None
    continuation_of: int | None = None
    audio_frames: int = 0
    voice_frames: int = 0
    sound_frames: int = 0
    impulsive: bool = False
    motion_hits: int = 0
    motion_max: float = 0.0
    t_peak: float | None = None
    light_change: bool = False
    floor_db: float | None = None


@dataclass
class Job:
    ev: OpenEvent
    end: float
    pcm_start: float | None
    pcm: bytes = field(repr=False, default=b"")


def classify(ev, cfg_events, analysis_fps, duration):
    """Etiquetas honestas: sólo lo que los detectores realmente miden."""
    voice_s = ev.voice_frames * FRAME_S
    voice_ratio = ev.voice_frames / ev.audio_frames if ev.audio_frames else 0.0
    has_voice = voice_s >= cfg_events["min_voice_s"] and voice_ratio >= cfg_events["min_voice_ratio"]
    has_sound = ev.sound_frames >= 5 or ev.impulsive
    has_motion = ev.motion_hits > 0
    motion_ratio = min(1.0, ev.motion_hits / max(1.0, duration * analysis_fps)) if analysis_fps else 0.0
    labels = []
    if has_voice:
        labels.append({"name": "voz", "conf": round(voice_ratio, 2), "source": "WebRTC VAD",
                       "note": "proporción de ventanas con voz; no es una probabilidad calibrada"})
    if has_sound:
        labels.append({"name": "sonido", "conf": None, "source": "energía adaptativa",
                       "note": "nivel por encima del ambiente"})
    if ev.impulsive:
        labels.append({"name": "impulsivo", "conf": None, "source": "energía adaptativa",
                       "note": "subida brusca de nivel: posible golpe, puerta o caída (no confirmado)"})
    if has_motion:
        labels.append({"name": "movimiento", "conf": round(ev.motion_max, 3), "source": "diferencia con fondo",
                       "note": "conf = fracción máxima de la imagen que cambió"})
    if ev.light_change:
        labels.append({"name": "cambio_de_luz", "conf": None, "source": "brillo global",
                       "note": "se ignoró como movimiento"})
    if has_voice:
        category = "voz"
    elif has_motion and has_sound:
        category = "movimiento_sonido"
    elif has_motion:
        category = "movimiento"
    elif ev.impulsive:
        category = "impulsivo"
    elif has_sound:
        category = "sonido"
    else:
        category = "sin_clasificar"
    return dict(category=category, labels=json.dumps(labels, ensure_ascii=False),
                has_voice=int(has_voice), has_sound=int(has_sound), has_motion=int(has_motion),
                impulsive=int(ev.impulsive), light_change=int(ev.light_change),
                voice_ratio=round(voice_ratio, 3), motion_max=round(ev.motion_max, 4),
                motion_ratio=round(motion_ratio, 3))


class EventManager:
    def __init__(self, cfg, conn, audio, store, finalizer, clock=time.time):
        self.cfg = cfg["events"]
        self.conn = conn
        self.audio = audio
        self.store = store
        self.finalizer = finalizer
        self.clock = clock
        self.lock = threading.Lock()
        self.cur = None
        self.last_end = None
        self.last_id = None
        self.last_split = False

    def _open(self, t):
        start = t - self.cfg["pre_roll_s"]
        cont = None
        if self.last_end is not None:
            start = max(start, self.last_end)
            if self.last_split and t - self.last_end < self.cfg["merge_gap_s"]:
                cont = self.last_id
        uid = time.strftime("%Y%m%d-%H%M%S", time.localtime(start)) + "-" + secrets.token_hex(3)
        eid = db.insert_event(self.conn, uid, start, cont)
        rec = self.audio.start_recording(start) if self.audio else None
        if self.store:
            self.store.pin(eid, start)
        self.cur = OpenEvent(id=eid, uid=uid, start=start, first_trigger=t, last_trigger=t,
                             recorder=rec, continuation_of=cont)
        log.info("Evento %d abierto (%s)", eid, uid)

    def on_audio(self, t, info):
        with self.lock:
            if self.cur is None:
                self._open(t)
            ev = self.cur
            ev.last_trigger = max(ev.last_trigger, t)
            ev.audio_frames += 1
            if info["kind"] == "voz":
                ev.voice_frames += 1
            else:
                ev.sound_frames += 1
            ev.impulsive = ev.impulsive or info.get("impulsive", False)
            if ev.floor_db is None:
                ev.floor_db = info.get("floor")

    def on_motion(self, t, ratio):
        with self.lock:
            if self.cur is None:
                self._open(t)
            ev = self.cur
            ev.last_trigger = max(ev.last_trigger, t)
            ev.motion_hits += 1
            if ratio >= ev.motion_max:
                ev.motion_max, ev.t_peak = ratio, t

    def on_light(self, t):
        with self.lock:
            if self.cur is not None:
                self.cur.light_change = True

    def tick(self):
        now = self.clock()
        with self.lock:
            ev = self.cur
            if ev is None:
                return
            hold = max(self.cfg["post_roll_s"], self.cfg["merge_gap_s"])
            if now - ev.last_trigger >= hold:
                self._close(min(ev.last_trigger + self.cfg["post_roll_s"], now), split=False)
            elif now - ev.start >= self.cfg["max_event_s"]:
                self._close(now, split=True)

    def shutdown(self):
        with self.lock:
            if self.cur is not None:
                self._close(self.clock(), split=False)

    def current_id(self):
        return self.cur.id if self.cur else None

    def _close(self, end, split):
        ev, self.cur = self.cur, None
        pcm_start, pcm = (None, b"")
        if ev.recorder is not None:
            pcm_start, pcm = self.audio.stop_recording(ev.recorder, end)
        ev.recorder = None
        self.last_end, self.last_id, self.last_split = end, ev.id, split
        log.info("Evento %d cerrado (%.1f s)%s", ev.id, end - ev.start, " [continúa]" if split else "")
        self.finalizer.submit(Job(ev, end, pcm_start, pcm))


class Finalizer(threading.Thread):
    """Codifica audio, arma el clip de video y la miniatura fuera del hilo de captura."""

    def __init__(self, cfg, paths, store):
        super().__init__(name="finalizer", daemon=True)
        self.cfg = cfg
        self.paths = paths
        self.store = store
        self.q = queue.Queue()
        self.conn = db.connect(paths.db)

    def submit(self, job):
        self.q.put(job)

    def pending(self):
        return self.q.qsize()

    def run(self):
        while True:
            job = self.q.get()
            if job is None:
                return
            try:
                self.process(job)
            except Exception as e:
                log.exception("Error finalizando evento %d", job.ev.id)
                db.update_event(self.conn, job.ev.id, status="error", ended_at=job.end, error=str(e)[:500])
            finally:
                if self.store:
                    self.store.unpin(job.ev.id)

    def process(self, job):
        ev, end = job.ev, job.end
        c = self.cfg
        db.update_event(self.conn, ev.id, status="processing", ended_at=end)
        tmp = []
        new_media = []
        stats = {}

        def out_path(kind, ext):
            rel = self.paths.rel(kind, ev.start, ev.uid, ext)
            p = self.paths.abs(rel)
            os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
            return rel, p

        try:
            pcm_path = None
            if job.pcm:
                pcm_path = os.path.join(self.paths.tmp, ev.uid + ".pcm")
                write_pcm(pcm_path, job.pcm)
                tmp.append(pcm_path)
                stats["peak_db"], stats["mean_db"] = pcm_stats(job.pcm)
                rel, p = out_path("audio", "m4a")
                media.encode_audio(pcm_path, p)
                new_media.append(("audio", rel, "audio/mp4", p, len(job.pcm) / 32000))

            want_video = (self.store is not None and c["video"]["enabled"] and
                          (ev.motion_hits > 0 or c["events"]["video_on_audio"]))
            if want_video:
                deadline = time.time() + c["video"]["segment_seconds"] * 3 + 6
                while not self.store.ready(end) and time.time() < deadline:
                    time.sleep(0.5)
                segs = self.store.select(ev.start, end)
                if segs:
                    vstart = segs[0][0]
                    ss = max(0.0, ev.start - vstart)
                    dur = max(1.0, end - (vstart + ss))
                    lst = os.path.join(self.paths.tmp, ev.uid + ".txt")
                    media.concat_list(lst, [p for _, p in segs])
                    tmp.append(lst)
                    rel, p = out_path("video", "mp4")
                    off = (job.pcm_start - (vstart + ss)) if pcm_path else 0.0
                    media.build_clip(lst, p, ss, dur, pcm_path, off)
                    new_media.append(("video", rel, "video/mp4", p, dur))
                    at = (ev.t_peak or ev.first_trigger) - (vstart + ss)
                    trel, tp = out_path("thumbs", "jpg")
                    try:
                        media.thumbnail(p, min(max(at, 0.0), max(0.0, dur - 0.5)), tp)
                        new_media.append(("thumb", trel, "image/jpeg", tp, None))
                    except RuntimeError as e:
                        log.warning("Sin miniatura para %s: %s", ev.uid, e)
                else:
                    log.warning("Evento %d sin segmentos de video disponibles", ev.id)

            if pcm_path and not any(m[0] == "thumb" for m in new_media):
                trel, tp = out_path("thumbs", "png")
                try:
                    media.waveform(pcm_path, tp)
                    new_media.append(("thumb", trel, "image/png", tp, None))
                except RuntimeError as e:
                    log.warning("Sin forma de onda para %s: %s", ev.uid, e)
        finally:
            for p in tmp:
                try:
                    os.remove(p)
                except OSError:
                    pass

        fields = classify(ev, c["events"], c["video"]["analysis_fps"] if c["video"]["enabled"] else 0,
                          end - ev.start)
        has_audio = any(m[0] == "audio" for m in new_media)
        tr = c["transcription"]
        fields["transcript_status"] = "pending" if (tr["enabled"] and has_audio and fields["has_voice"]) else "none"
        self.conn.execute("BEGIN")
        try:
            for kind, rel, mime, p, dur in new_media:
                db.add_media(self.conn, ev.id, kind, rel, mime, os.path.getsize(p),
                             round(dur, 2) if dur else None)
            db.update_event(self.conn, ev.id, status="ready", ended_at=end, noise_floor_db=ev.floor_db,
                            **stats, **fields)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        log.info("Evento %d listo: %s, %d archivo(s)", ev.id, fields["category"], len(new_media))
        self._webhook(ev.id)

    def _webhook(self, eid):
        url = self.cfg["general"]["webhook_url"]
        if not url:
            return
        data = db.get_event(self.conn, eid)

        def send():
            try:
                req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=3).close()
            except Exception as e:
                log.warning("Webhook falló: %s", e)
        threading.Thread(target=send, daemon=True).start()
