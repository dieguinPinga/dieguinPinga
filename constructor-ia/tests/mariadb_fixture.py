"""Servidor MariaDB privado y desechable para las pruebas (datadir y socket temporales, sin red)."""
from __future__ import annotations

import atexit
import os
import shutil
import signal
import subprocess
import tempfile
import time

_SERVER = None


def available() -> bool:
    return bool(shutil.which("mariadbd") and shutil.which("mariadb-install-db") and shutil.which("mariadb"))


class Server:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="cia-mariadb-")
        self.socket = os.path.join(self.dir, "s.sock")
        data = os.path.join(self.dir, "data")
        user = ["--user=root"] if os.getuid() == 0 else []
        subprocess.run(["mariadb-install-db", "--no-defaults", f"--datadir={data}", "--skip-test-db",
                        "--auth-root-authentication-method=normal", *user],
                       check=True, capture_output=True, timeout=120)
        self.proc = subprocess.Popen(["mariadbd", "--no-defaults", f"--datadir={data}", f"--socket={self.socket}",
                                      "--skip-networking", f"--pid-file={self.dir}/p.pid",
                                      f"--log-error={self.dir}/err.log", "--innodb-buffer-pool-size=32M", *user],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if os.path.exists(self.socket) and self.sql("SELECT 1", check=False).returncode == 0:
                break
            time.sleep(0.3)
        else:
            raise RuntimeError("MariaDB de prueba no arrancó")
        atexit.register(self.stop)

    def sql(self, statements: str, database: str | None = None, check: bool = True):
        cmd = ["mariadb", "--no-defaults", "-S", self.socket, "-uroot", "--default-character-set=utf8mb4", "-N", "-B"]
        if database:
            cmd.append(database)
        return subprocess.run(cmd, input=statements, text=True, capture_output=True, check=check, timeout=60)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


def server() -> Server:
    global _SERVER
    if _SERVER is None:
        _SERVER = Server()
    return _SERVER
