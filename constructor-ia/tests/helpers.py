from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from constructor_ia import config


def bwrap_works() -> bool:
    if not shutil.which("bwrap"):
        return False
    try:
        r = subprocess.run(["bwrap", "--unshare-all", "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib",
                            "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/bin", "/bin", "--proc", "/proc",
                            "--dev", "/dev", "/usr/bin/true"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def make_cfg(ollama_url: str = "http://127.0.0.1:11435", **over) -> config.Config:
    d = Path(tempfile.mkdtemp(prefix="cia-test-"))
    cfg = config.load(d / "no-existe.toml", base_dir=d)
    cfg.ollama_url = ollama_url
    cfg.cookie_secure = False
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for(pred, timeout=30.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError("Condición no cumplida a tiempo")


def fn_code(body: str, name: str = "sumar") -> str:
    return f"```python\ndef {name}(a, b):\n    {body}\n```"
