"""Carga de configuración (TOML) con valores por defecto."""
import copy
import os
import tomllib

DEFAULTS = {
    "general": {
        "data_dir": "~/vigilancia-data",
        "retention_days": 7,
        "min_free_gb": 20,
        "max_storage_gb": 100,
        "log_level": "INFO",
        "webhook_url": "",
    },
    "audio": {
        "enabled": True,
        "source": "alsa_input.pci-0000_00_1f.3.analog-stereo",
        "command": [],
        "vad_aggressiveness": 2,
        "energy_margin_db": 12.0,
        "voice_margin_db": 6.0,
        "voice_min_ratio": 0.5,
        "sound_min_ratio": 0.3,
        "window_s": 0.5,
        "impulse_jump_db": 20.0,
        "ring_seconds": 20,
    },
    "video": {
        "enabled": True,
        "device": "/dev/video0",
        "input_format": "yuyv422",
        "width": 640,
        "height": 480,
        "capture_fps": 30,
        "record_fps": 15,
        "crf": 28,
        "preset": "veryfast",
        "segment_seconds": 4,
        "buffer_seconds": 60,
        "analysis_fps": 5,
        "pixel_threshold": 22,
        "min_area": 0.008,
        "min_frames": 3,
        "light_change_brightness": 18,
        "dark_brightness": 18,
        "ignore_zones": [],
        "input_args": [],
    },
    "events": {
        "pre_roll_s": 5,
        "post_roll_s": 6,
        "merge_gap_s": 8,
        "max_event_s": 300,
        "video_on_audio": True,
        "min_voice_s": 0.6,
        "min_voice_ratio": 0.25,
    },
    "web": {
        "bind": ["127.0.0.1"],
        "port": 8765,
        "user": "diego",
        "password_hash": "",
        "session_hours": 72,
        "secure_cookie": False,
        "api_token": "",
    },
    "transcription": {
        "enabled": True,
        "python": "",
        "model": "small",
        "compute_type": "int8",
        "cpu_threads": 2,
        "language": "es",
        "min_mem_available_mb": 1200,
        "unload_after_s": 600,
        "nice": 15,
    },
}


def _merge(base, extra, path=""):
    for k, v in extra.items():
        if k not in base:
            raise ValueError(f"Opción desconocida en la configuración: {path}{k}")
        if isinstance(base[k], dict):
            if not isinstance(v, dict):
                raise ValueError(f"{path}{k} debe ser una sección")
            _merge(base[k], v, f"{path}{k}.")
        else:
            base[k] = v


def load(path=None):
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        with open(path, "rb") as f:
            _merge(cfg, tomllib.load(f))
    cfg["general"]["data_dir"] = os.path.realpath(os.path.expanduser(cfg["general"]["data_dir"]))
    cfg["_path"] = os.path.realpath(path) if path else None
    if cfg["events"]["post_roll_s"] > cfg["audio"]["ring_seconds"] - 2:
        raise ValueError("audio.ring_seconds debe superar events.post_roll_s")
    if cfg["events"]["pre_roll_s"] > cfg["audio"]["ring_seconds"] - 2:
        raise ValueError("audio.ring_seconds debe superar events.pre_roll_s")
    return cfg


class Paths:
    """Rutas del directorio de datos. Toda ruta servida pasa por abs()."""

    SUBDIRS = ("db", "audio", "video", "thumbs", "segments", "logs", "tmp", "config")

    def __init__(self, data_dir):
        self.root = os.path.realpath(data_dir)
        self.db = os.path.join(self.root, "db", "vigilancia.db")
        self.segments = os.path.join(self.root, "segments")
        self.tmp = os.path.join(self.root, "tmp")
        self.logs = os.path.join(self.root, "logs")
        self.secret = os.path.join(self.root, "config", "session.secret")

    def ensure(self):
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        for d in self.SUBDIRS:
            os.makedirs(os.path.join(self.root, d), mode=0o700, exist_ok=True)

    def rel(self, kind, ts, uid, ext):
        import time
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        return f"{kind}/{day}/{uid}.{ext}"

    def abs(self, rel):
        p = os.path.realpath(os.path.join(self.root, rel))
        if not p.startswith(self.root + os.sep):
            raise ValueError(f"Ruta fuera del directorio de datos: {rel!r}")
        return p
