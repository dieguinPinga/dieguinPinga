"""Registro a archivo rotativo + stderr (journald) con filtro de secretos."""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
from pathlib import Path

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(password|contraseña|passwd|token|csrf|cookie|authorization|secret)(\"?\s*[:=]\s*\"?)[^\s\",;]+"), r"\1\2[oculto]"),
    (re.compile(r"cia_session=[^\s;]+"), "cia_session=[oculto]"),
]


def redact(text: str) -> str:
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup(log_dir: Path, max_bytes: int, backups: int, level: int = logging.INFO) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(log_dir, 0o700)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(log_dir / "constructor-ia.log", maxBytes=max_bytes,
                                              backupCount=backups, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(sh)
    try:
        os.chmod(log_dir / "constructor-ia.log", 0o600)
    except OSError:
        pass
