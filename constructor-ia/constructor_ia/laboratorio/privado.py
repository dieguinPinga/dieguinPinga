"""MariaDB PRIVADO y desechable para el laboratorio.

No es el MariaDB instalado: usa sus binarios con --no-defaults (no lee my.cnf), un datadir propio dentro del
directorio del laboratorio, un socket propio y --skip-networking (sin puerto TCP). No toca el servicio del sistema,
su configuración ni sus bases. Al terminar se detiene y, salvo que se pida conservarlo, se borra.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

BINARIOS = ("mariadbd", "mariadb-install-db", "mariadb")


class ServidorNoDisponible(RuntimeError):
    pass


def disponible() -> list[str]:
    """Binarios que faltan (vacío si se puede crear un servidor privado)."""
    return [b for b in BINARIOS if not shutil.which(b)]


class ServidorPrivado:
    def __init__(self, directorio: Path, *, buffer_mb: int = 64, conservar: bool = False):
        faltan = disponible()
        if faltan:
            raise ServidorNoDisponible(f"faltan los binarios {', '.join(faltan)}")
        self.dir = Path(directorio)
        if self.dir.exists() and any(self.dir.iterdir()):
            raise ServidorNoDisponible(f"{self.dir} no está vacío: el laboratorio no reutiliza datos ajenos")
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conservar = conservar
        self.socket = str(self.dir / "s.sock")
        data = self.dir / "data"
        user = ["--user=root"] if os.getuid() == 0 else []
        r = subprocess.run(["mariadb-install-db", "--no-defaults", f"--datadir={data}", "--skip-test-db",
                            "--auth-root-authentication-method=normal", *user], capture_output=True, text=True,
                           timeout=180)
        if r.returncode:
            raise ServidorNoDisponible(f"mariadb-install-db falló: {(r.stderr or r.stdout)[-500:]}")
        self.proc = subprocess.Popen(["mariadbd", "--no-defaults", f"--datadir={data}", f"--socket={self.socket}",
                                      "--skip-networking", f"--pid-file={self.dir}/p.pid",
                                      f"--log-error={self.dir}/err.log", f"--innodb-buffer-pool-size={buffer_mb}M",
                                      "--performance-schema=OFF", *user],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            if os.path.exists(self.socket) and self.sql("SELECT 1", check=False).returncode == 0:
                return
            time.sleep(0.3)
        log = (self.dir / "err.log").read_text(errors="replace")[-800:] if (self.dir / "err.log").exists() else ""
        self.detener()
        raise ServidorNoDisponible(f"el MariaDB privado no arrancó: {log}")

    def sql(self, statements: str, database: str | None = None, check: bool = True):
        cmd = ["mariadb", "--no-defaults", "-S", self.socket, "-uroot", "--default-character-set=utf8mb4", "-N", "-B"]
        if database:
            cmd.append(database)
        return subprocess.run(cmd, input=statements, text=True, capture_output=True, check=check, timeout=300)

    def detener(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        if not self.conservar:
            shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.detener()
