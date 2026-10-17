"""Captura continua de micrófono (parec), buffer circular y detección de actividad.

- Voz: WebRTC VAD + nivel por encima del piso de ruido.
- Sonido: energía adaptativa (percentil 20 de los últimos 10 s como piso).
- Impulsivo: subida brusca de nivel (posible golpe/puerta/caída; NO se confirma qué fue).
"""
import collections
import logging
import math
import os
import subprocess
import threading
import time

import numpy as np

from .util import read_exact, start_drain, stop_proc

try:
    import webrtcvad
except ImportError:  # el sistema sigue funcionando sólo con energía
    webrtcvad = None

log = logging.getLogger("audio")

RATE = 16000
FRAME_S = 0.03
FRAME_BYTES = int(RATE * FRAME_S) * 2  # s16le mono


def rms_db(frame):
    a = np.frombuffer(frame, dtype="<i2").astype(np.float32)
    if a.size == 0:
        return -120.0
    r = math.sqrt(float(np.mean(a * a))) / 32768.0
    return 20 * math.log10(r) if r > 1e-6 else -120.0


class AudioDetector:
    """Sin hilos ni E/S: recibe tramas de 30 ms y devuelve un disparador o None."""

    def __init__(self, cfg, vad=None):
        self.cfg = cfg
        self.vad = vad
        self.win_n = max(3, int(cfg["window_s"] / FRAME_S))
        self.win = collections.deque(maxlen=self.win_n)
        self.hist = collections.deque(maxlen=int(10 / FRAME_S))
        self.recent = collections.deque(maxlen=7)
        self.floor = None
        self.level = -120.0
        self.n = 0

    def process(self, frame):
        c = self.cfg
        db = rms_db(frame)
        self.level = db
        self.n += 1
        self.hist.append(db)
        if len(self.hist) >= 33 and (self.floor is None or self.n % 33 == 0):
            self.floor = max(-90.0, float(np.percentile(self.hist, 20)))
        if self.floor is None:
            self.recent.append(db)
            return None
        above = db > self.floor + c["energy_margin_db"]
        voice = False
        if self.vad is not None and db > self.floor + c["voice_margin_db"]:
            try:
                voice = self.vad.is_speech(frame, RATE)
            except Exception:
                voice = False
        prev = sum(self.recent) / len(self.recent) if self.recent else db
        impulsive = db - prev >= c["impulse_jump_db"] and db > self.floor + c["energy_margin_db"] + 6
        self.recent.append(db)
        self.win.append((voice, above))
        if len(self.win) < self.win_n and not impulsive:
            return None
        vr = sum(v for v, _ in self.win) / len(self.win)
        ar = sum(a for _, a in self.win) / len(self.win)
        if vr >= c["voice_min_ratio"]:
            kind = "voz"
        elif ar >= c["sound_min_ratio"] or impulsive:
            kind = "sonido"
        else:
            return None
        return {"kind": kind, "db": db, "impulsive": impulsive, "floor": self.floor}


class Recorder:
    def __init__(self, start):
        self.start = start
        self.first_t = None
        self.chunks = []

    def append(self, t, frame):
        if t + FRAME_S < self.start:
            return
        if self.first_t is None:
            self.first_t = t
        self.chunks.append(frame)


class AudioCapture(threading.Thread):
    def __init__(self, cfg, on_trigger):
        super().__init__(name="audio", daemon=True)
        self.cfg = cfg
        self.on_trigger = on_trigger
        self.lock = threading.Lock()
        self.ring = collections.deque(maxlen=int(cfg["ring_seconds"] / FRAME_S))
        self.recorders = []
        self.stop_evt = threading.Event()
        self.proc = None
        self.state = "iniciando"
        self.error = ""
        self.restarts = 0
        self.last_frame = 0.0
        self.proc_started = 0.0
        self.stderr_tail = collections.deque(maxlen=5)
        vad = webrtcvad.Vad(int(cfg["vad_aggressiveness"])) if webrtcvad else None
        if vad is None:
            log.warning("webrtcvad no disponible: sólo detección por energía (sin categoría 'voz')")
        self.detector = AudioDetector(cfg, vad)

    def command(self):
        if self.cfg["command"]:
            return list(self.cfg["command"])
        return ["parec", f"--device={self.cfg['source']}", "--format=s16le", f"--rate={RATE}",
                "--channels=1", "--raw", "--latency-msec=100"]

    def start_recording(self, start):
        with self.lock:
            r = Recorder(start)
            for t, f in self.ring:
                r.append(t, f)
            self.recorders.append(r)
            return r

    def stop_recording(self, rec, end):
        with self.lock:
            if rec in self.recorders:
                self.recorders.remove(rec)
        if rec.first_t is None:
            return None, b""
        n = max(0, int((end - rec.first_t) / FRAME_S))
        return rec.first_t, b"".join(rec.chunks[:n])

    def status(self):
        fresh = time.time() - self.last_frame < 3
        return {
            "state": "ok" if (self.state == "ok" and fresh) else ("error" if self.state == "ok" else self.state),
            "level_db": round(self.detector.level, 1),
            "floor_db": None if self.detector.floor is None else round(self.detector.floor, 1),
            "vad": self.detector.vad is not None,
            "error": self.error or (" | ".join(self.stderr_tail) if self.state != "ok" else ""),
            "restarts": self.restarts,
            "source": self.cfg["source"] if not self.cfg["command"] else "comando personalizado",
        }

    def watchdog(self, now):
        """Si el subproceso sigue vivo pero no entrega datos, se lo mata para que el bucle lo reinicie."""
        p = self.proc
        if p is not None and p.poll() is None and self.state == "ok":
            if now - max(self.last_frame, self.proc_started) > 10:
                log.error("Sin datos hace más de 10 s: reiniciando el proceso de captura")
                p.kill()

    def stop(self):
        self.stop_evt.set()
        stop_proc(self.proc)

    def run(self):
        backoff = 1
        while not self.stop_evt.is_set():
            started = time.time()
            try:
                self.proc = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             start_new_session=True)
            except OSError as e:
                self.state, self.error = "error", f"No se pudo iniciar la captura de audio: {e}"
                log.error(self.error)
            else:
                self.proc_started = time.time()
                start_drain(self.proc, log, self.stderr_tail)
                self.state, self.error = "ok", ""
                log.info("Captura de audio iniciada (pid %d)", self.proc.pid)
                self._loop()
                stop_proc(self.proc)
                if self.stop_evt.is_set():
                    break
                self.state = "error"
                self.error = f"La captura de audio terminó (código {self.proc.returncode})"
                log.error(self.error)
            self.restarts += 1
            if time.time() - started > 60:
                backoff = 1
            self.stop_evt.wait(backoff)
            backoff = min(backoff * 2, 30)

    def _loop(self):
        anchor, n = time.time(), 0
        out = self.proc.stdout
        while not self.stop_evt.is_set():
            frame = read_exact(out, FRAME_BYTES)
            if frame is None:
                return
            now = time.time()
            t = anchor + n * FRAME_S
            if abs((now - FRAME_S) - t) > 1.0:  # deriva o pausa: re-anclar al reloj
                anchor, n = now - FRAME_S, 0
                t = anchor
            n += 1
            self.last_frame = now
            with self.lock:
                self.ring.append((t, frame))
                for r in self.recorders:
                    r.append(t, frame)
            trig = self.detector.process(frame)
            if trig:
                try:
                    self.on_trigger(t, trig)
                except Exception:
                    log.exception("Error procesando disparador de audio")


def pcm_stats(pcm):
    """(pico_dB, media_dB) sobre ventanas de 30 ms."""
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    n = a.size // (FRAME_BYTES // 2)
    if n == 0:
        return None, None
    frames = a[: n * (FRAME_BYTES // 2)].reshape(n, -1) / 32768.0
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    peak = float(rms.max())
    mean = float(np.sqrt(np.mean(rms * rms)))
    to_db = lambda x: round(20 * math.log10(x), 1) if x > 1e-6 else -120.0
    return to_db(peak), to_db(mean)


def write_pcm(path, pcm):
    with open(path, "wb") as f:
        f.write(pcm)
    os.chmod(path, 0o600)
