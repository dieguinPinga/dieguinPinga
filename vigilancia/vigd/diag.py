"""Diagnóstico: dependencias, dispositivos, base de datos y estado de los procesos."""
import json
import os
import shutil
import subprocess
import sys
import time

from . import db
from .config import Paths

OK, WARN, ERR = "OK ", "AVISO", "ERROR"


def line(level, msg):
    print(f"[{level:5}] {msg}")


def run(cfg, args):
    paths = Paths(cfg["general"]["data_dir"])
    line(OK, f"Configuración: {cfg['_path']}")
    line(OK, f"Python {sys.version.split()[0]} ({sys.executable})")
    if cfg["_path"] and os.stat(cfg["_path"]).st_mode & 0o077:
        line(WARN, "El archivo de configuración es legible por otros usuarios: chmod 600")

    for mod in ("numpy", "webrtcvad"):
        try:
            m = __import__(mod)
            line(OK, f"{mod} {getattr(m, '__version__', '')}")
        except ImportError:
            line(ERR if mod == "numpy" else WARN, f"{mod} no instalado")
    wp = cfg["transcription"]["python"] or sys.executable
    r = subprocess.run([wp, "-c", "import faster_whisper; print(faster_whisper.__version__)"],
                       capture_output=True, text=True)
    line(OK if r.returncode == 0 else WARN, f"faster-whisper en {wp}: {r.stdout.strip() or 'no disponible'}")

    for tool in ("ffmpeg", "parec"):
        line(OK if shutil.which(tool) else ERR, f"{tool}: {shutil.which(tool) or 'no encontrado'}")
    if shutil.which("ffmpeg"):
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        line(OK if "libx264" in enc else ERR, "codificador libx264 " + ("disponible" if "libx264" in enc else "ausente"))

    dev = cfg["video"]["device"]
    if cfg["video"]["enabled"]:
        if os.path.exists(dev):
            line(OK if os.access(dev, os.R_OK | os.W_OK) else ERR, f"{dev} accesible: {os.access(dev, os.R_OK | os.W_OK)}")
            if shutil.which("fuser"):
                r = subprocess.run(["fuser", dev], capture_output=True, text=True)
                pids = r.stdout.split()
                line(WARN if pids else OK, f"procesos usando la cámara: {' '.join(pids) or 'ninguno'}")
        else:
            line(ERR, f"{dev} no existe")

    if os.path.isdir(paths.root):
        du = shutil.disk_usage(paths.root)
        free = du.free / 1e9
        line(OK if free > cfg["general"]["min_free_gb"] else WARN, f"Disco libre: {free:.1f} GB en {paths.root}")
    else:
        line(WARN, f"{paths.root} todavía no existe (se crea al iniciar)")

    if os.path.exists(paths.db):
        conn = db.connect(paths.db)
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        line(OK if ic == "ok" else ERR, f"Base de datos: integrity_check = {ic}")
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM events GROUP BY status").fetchall())
        line(OK, f"Eventos por estado: {json.dumps(counts)}")
        size = conn.execute("SELECT COALESCE(SUM(bytes),0) FROM media").fetchone()[0]
        line(OK, f"Multimedia: {size / 1e6:.1f} MB")
        missing = 0
        for m in conn.execute("SELECT relpath FROM media"):
            try:
                if not os.path.exists(paths.abs(m["relpath"])):
                    missing += 1
            except ValueError:
                missing += 1
        line(OK if not missing else WARN, f"Archivos referenciados faltantes: {missing}")
        now = time.time()
        for r in conn.execute("SELECT * FROM status ORDER BY component"):
            age = now - r["updated_at"]
            lvl = OK if (r["state"] == "ok" and age < 15) or r["state"] in ("deshabilitado", "detenido") else WARN
            line(lvl, f"Proceso {r['component']}: {r['state']} (hace {age:.0f} s) {r['detail'][:160]}")

    if args.mic:
        _mic(cfg, args.mic)
    if args.cam:
        _cam(cfg, paths)


def _mic(cfg, seconds):
    from .audio import AudioCapture, AudioDetector, FRAME_BYTES, rms_db
    cap = AudioCapture(cfg["audio"], lambda *a: None)
    print(f"Midiendo micrófono {seconds} s (hablá o hacé ruido)...")
    p = subprocess.Popen(cap.command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    det = AudioDetector(cfg["audio"], cap.detector.vad)
    levels, trig = [], {"voz": 0, "sonido": 0}
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            fr = p.stdout.read(FRAME_BYTES)
            if len(fr) < FRAME_BYTES:
                break
            levels.append(rms_db(fr))
            t = det.process(fr)
            if t:
                trig[t["kind"]] += 1
    finally:
        p.terminate()
    if not levels:
        return line(ERR, "No llegaron datos del micrófono")
    levels.sort()
    line(OK, f"Nivel dBFS: mín {levels[0]:.1f}, mediana {levels[len(levels)//2]:.1f}, máx {levels[-1]:.1f}; "
             f"piso {det.floor}; tramas voz {trig['voz']}, sonido {trig['sonido']}")


def _cam(cfg, paths):
    out = os.path.join(paths.tmp, "diag-cam.jpg")
    v = cfg["video"]
    os.makedirs(paths.tmp, exist_ok=True)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "v4l2", "-input_format",
                        v["input_format"], "-video_size", f"{v['width']}x{v['height']}", "-i", v["device"],
                        "-frames:v", "1", out], capture_output=True, text=True, timeout=20)
    if r.returncode == 0:
        line(OK, f"Cuadro de prueba guardado en {out}")
    else:
        line(ERR, f"No se pudo capturar (¿la captura ya usa la cámara?): {r.stderr.strip()[-200:]}")
