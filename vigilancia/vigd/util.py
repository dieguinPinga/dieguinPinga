import logging
import os
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler


def setup_logging(cfg, name):
    logdir = os.path.join(cfg["general"]["data_dir"], "logs")
    os.makedirs(logdir, mode=0o700, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(cfg["general"]["log_level"])
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    fh = RotatingFileHandler(os.path.join(logdir, f"{name}.log"), maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)


def mem_available_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def read_exact(stream, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def drain(stream, logger, keep):
    """Lee stderr de un subproceso para que no se bloquee; guarda las últimas líneas."""
    for raw in iter(stream.readline, b""):
        line = raw.decode(errors="replace").rstrip()
        if line:
            logger.warning("%s", line)
            keep.append(line)


def start_drain(proc, logger, keep):
    t = threading.Thread(target=drain, args=(proc.stderr, logger, keep), daemon=True)
    t.start()
    return t


def stop_proc(proc, timeout=5):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(2)
