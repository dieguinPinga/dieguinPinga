"""Cámara: un único ffmpeg graba segmentos H.264 en disco (buffer rodante)
y entrega en paralelo cuadros grises de baja resolución para detectar movimiento.

Los clips de eventos se arman copiando segmentos (sin recodificar)."""
import collections
import logging
import os
import re
import subprocess
import threading
import time

import numpy as np

from .util import read_exact, start_drain, stop_proc

log = logging.getLogger("video")

AW, AH = 160, 120  # resolución de análisis
SEG_RE = re.compile(r"^seg_(\d+)\.ts$")


class MotionDetector:
    """Fondo adaptativo + diferencia por píxel sobre imagen 80x60 suavizada.

    Los cambios globales de luz se distinguen normalizando el brillo: si tras
    compensar la ganancia casi no queda diferencia, es luz y no movimiento."""

    def __init__(self, cfg, w=AW, h=AH):
        self.cfg = cfg
        self.w, self.h = w, h
        self.bg = None
        self.n = 0
        self.consec = 0
        self.active = False
        self.suppress = 0
        self.warmup = 10
        self.brightness = 0.0
        self.ratio = 0.0
        mh, mw = h // 2, w // 2
        self.mask = np.ones((mh, mw), dtype=bool)
        for z in cfg.get("ignore_zones", []):
            x, y, zw, zh = z
            self.mask[int(y * mh):int((y + zh) * mh), int(x * mw):int((x + zw) * mw)] = False
        self.mask_count = max(1, int(self.mask.sum()))

    def _changed(self, f):
        return float(((np.abs(f - self.bg) > self.cfg["pixel_threshold"]) & self.mask).sum()) / self.mask_count

    def process(self, raw):
        c = self.cfg
        f = np.frombuffer(raw, np.uint8).reshape(self.h, self.w).astype(np.float32)
        f = f.reshape(self.h // 2, 2, self.w // 2, 2).mean(axis=(1, 3))
        fm = float(f[self.mask].mean())
        self.brightness = fm
        self.n += 1
        if self.bg is None or self.n <= self.warmup:
            self.bg = f.copy() if self.bg is None else 0.5 * self.bg + 0.5 * f
            return None
        if self.suppress > 0:  # la autoexposición todavía se está acomodando
            self.suppress -= 1
            self.bg += 0.5 * (f - self.bg)
            self.consec, self.active = 0, False
            return None
        bm = float(self.bg[self.mask].mean())
        if abs(fm - bm) >= c["light_change_brightness"]:
            fn = f * (bm / max(fm, 1.0))
            if self._changed(fn) < c["min_area"] * 3:
                self.bg = f.copy()
                self.suppress = 10
                self.consec, self.active, self.ratio = 0, False, 0.0
                return "light"
            f = fn
        ratio = self._changed(f)
        self.ratio = ratio
        thr = c["min_area"] * (0.6 if self.active else 1.0)
        self.consec = self.consec + 1 if ratio >= thr else 0
        self.active = self.consec >= (1 if self.active else c["min_frames"])
        self.bg += (0.02 if self.active else 0.08) * (f - self.bg)
        return "motion" if self.active else None


class SegmentStore:
    def __init__(self, segdir, seg_seconds, buffer_seconds):
        self.dir = segdir
        self.seg_seconds = seg_seconds
        self.buffer_seconds = buffer_seconds
        self.pins = {}
        self.lock = threading.Lock()
        self.writer_alive = False

    def list(self):
        out = []
        try:
            names = os.listdir(self.dir)
        except FileNotFoundError:
            return out
        for name in names:
            m = SEG_RE.match(name)
            if m:
                out.append((int(m.group(1)), os.path.join(self.dir, name)))
        out.sort()
        return out

    def pin(self, key, t):
        with self.lock:
            self.pins[key] = t

    def unpin(self, key):
        with self.lock:
            self.pins.pop(key, None)

    def ready(self, end):
        """El segmento que contiene `end` ya está cerrado."""
        segs = self.list()
        if not self.writer_alive:
            return True
        return any(t > end for t, _ in segs)

    def select(self, start, end):
        segs = self.list()
        sel = []
        for i, (t, p) in enumerate(segs):
            t_next = segs[i + 1][0] if i + 1 < len(segs) else t + self.seg_seconds * 2
            if t < end and t_next > start and os.path.getsize(p) > 0:
                sel.append((t, p))
        return sel

    def latest_closed(self):
        segs = self.list()
        if len(segs) >= 2:
            return segs[-2]
        return segs[-1] if segs and not self.writer_alive else None

    def cleanup(self, now):
        segs = self.list()
        keep_from = now - self.buffer_seconds
        with self.lock:
            if self.pins:
                keep_from = min(keep_from, min(self.pins.values()) - self.seg_seconds * 2)
        for i, (t, p) in enumerate(segs[:-1]):
            if segs[i + 1][0] < keep_from:
                try:
                    os.remove(p)
                except OSError:
                    pass

    def clear(self):
        for _, p in self.list():
            try:
                os.remove(p)
            except OSError:
                pass


class VideoCapture(threading.Thread):
    def __init__(self, cfg, store, on_motion, on_light):
        super().__init__(name="video", daemon=True)
        self.cfg = cfg
        self.store = store
        self.on_motion = on_motion
        self.on_light = on_light
        self.detector = MotionDetector(cfg)
        self.stop_evt = threading.Event()
        self.proc = None
        self.state = "iniciando"
        self.error = ""
        self.restarts = 0
        self.last_frame = 0.0
        self.proc_started = 0.0
        self.fps = 0.0
        self.light_changes = 0
        self.stderr_tail = collections.deque(maxlen=5)

    def command(self):
        c = self.cfg
        if c["input_args"]:
            inp = list(c["input_args"])
        else:
            inp = ["-f", "v4l2", "-thread_queue_size", "64", "-input_format", c["input_format"],
                   "-video_size", f"{c['width']}x{c['height']}", "-framerate", str(c["capture_fps"]),
                   "-i", c["device"]]
        fc = (f"[0:v]split=2[r][a];[r]fps={c['record_fps']}[rec];"
              f"[a]fps={c['analysis_fps']},scale={AW}:{AH},format=gray[ana]")
        return (["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"] + inp +
                ["-filter_complex", fc,
                 "-map", "[rec]", "-c:v", "libx264", "-preset", c["preset"], "-crf", str(c["crf"]),
                 "-pix_fmt", "yuv420p", "-g", str(c["record_fps"]), "-keyint_min", str(c["record_fps"]),
                 "-sc_threshold", "0", "-an",
                 "-f", "segment", "-segment_time", str(c["segment_seconds"]), "-segment_format", "mpegts",
                 "-reset_timestamps", "1", "-strftime", "1", os.path.join(self.store.dir, "seg_%s.ts"),
                 "-map", "[ana]", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"])

    def status(self):
        fresh = time.time() - self.last_frame < 5
        state = self.state
        if state == "ok" and not fresh:
            state = "error"
        d = self.detector
        return {
            "state": state,
            "error": self.error or (" | ".join(self.stderr_tail) if state != "ok" else ""),
            "fps_analisis": round(self.fps, 1),
            "brillo": round(d.brightness, 1),
            "oscuro": d.brightness < self.cfg["dark_brightness"],
            "movimiento_actual": round(d.ratio, 4),
            "movimiento_activo": d.active,
            "cambios_de_luz": self.light_changes,
            "restarts": self.restarts,
            "device": self.cfg["device"] if not self.cfg["input_args"] else "entrada personalizada",
        }

    def watchdog(self, now):
        """Si el subproceso sigue vivo pero no entrega datos, se lo mata para que el bucle lo reinicie."""
        p = self.proc
        if p is not None and p.poll() is None and self.state == "ok":
            if now - max(self.last_frame, self.proc_started) > 20:
                log.error("Sin datos hace más de 20 s: reiniciando el proceso de captura")
                p.kill()

    def stop(self):
        self.stop_evt.set()
        stop_proc(self.proc, timeout=8)

    def run(self):
        backoff = 1
        while not self.stop_evt.is_set():
            started = time.time()
            if not self.cfg["input_args"] and not os.path.exists(self.cfg["device"]):
                self.state, self.error = "error", f"No existe {self.cfg['device']}"
                log.error(self.error)
                self.stop_evt.wait(10)
                continue
            try:
                self.proc = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             start_new_session=True)
            except OSError as e:
                self.state, self.error = "error", f"No se pudo iniciar ffmpeg: {e}"
                log.error(self.error)
            else:
                self.proc_started = time.time()
                start_drain(self.proc, log, self.stderr_tail)
                self.store.writer_alive = True
                self.state, self.error = "ok", ""
                log.info("Captura de video iniciada (pid %d)", self.proc.pid)
                self._loop()
                stop_proc(self.proc, timeout=8)
                self.store.writer_alive = False
                if self.stop_evt.is_set():
                    break
                self.state = "error"
                self.error = f"ffmpeg de cámara terminó (código {self.proc.returncode}). ¿Cámara ocupada?"
                log.error(self.error)
            self.restarts += 1
            if time.time() - started > 60:
                backoff = 1
            self.stop_evt.wait(backoff)
            backoff = min(backoff * 2, 60)

    def _loop(self):
        size = AW * AH
        out = self.proc.stdout
        count, t0 = 0, time.time()
        while not self.stop_evt.is_set():
            raw = read_exact(out, size)
            if raw is None:
                return
            now = time.time()
            self.last_frame = now
            count += 1
            if now - t0 >= 5:
                self.fps, count, t0 = count / (now - t0), 0, now
            res = self.detector.process(raw)
            try:
                if res == "motion":
                    self.on_motion(now, self.detector.ratio)
                elif res == "light":
                    self.light_changes += 1
                    self.on_light(now)
            except Exception:
                log.exception("Error procesando movimiento")
