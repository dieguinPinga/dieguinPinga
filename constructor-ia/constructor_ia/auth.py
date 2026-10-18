"""Usuarios, contraseñas (scrypt), sesiones y limitación de intentos de acceso."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass

from .db import Database

USERNAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,31}$")
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
MIN_PASSWORD = 10
MAX_PASSWORD = 256


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=64 * 1024 * 1024, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(dk)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(expected))
        return hmac.compare_digest(got, expected)
    except (ValueError, TypeError):
        return False


# Hash ficticio para igualar el tiempo de respuesta cuando el usuario no existe.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def validate_new_password(password: str) -> None:
    if not isinstance(password, str) or not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise ValueError(f"La contraseña debe tener entre {MIN_PASSWORD} y {MAX_PASSWORD} caracteres")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Session:
    id: int
    user_id: int
    username: str
    csrf: str


class LoginLimiter:
    """Retraso exponencial por IP y bloqueo global ante ráfagas de fallos.

    Detrás de Tailscale Serve todas las peticiones llegan desde 127.0.0.1, por
    eso existe además un contador global.
    """

    def __init__(self, per_ip_free: int = 5, global_window: int = 300, global_max: int = 30):
        self._lock = threading.Lock()
        self._ip: dict[str, tuple[int, float]] = {}
        self._global: list[float] = []
        self.per_ip_free = per_ip_free
        self.global_window = global_window
        self.global_max = global_max

    def check(self, ip: str) -> float:
        """Devuelve los segundos que faltan para poder intentar (0 = permitido)."""
        now = time.monotonic()
        with self._lock:
            self._global = [t for t in self._global if now - t < self.global_window]
            if len(self._global) >= self.global_max:
                return self.global_window - (now - self._global[0])
            fails, last = self._ip.get(ip, (0, 0.0))
            if fails >= self.per_ip_free:
                wait = min(2 ** (fails - self.per_ip_free) * 2, 900)
                remaining = wait - (now - last)
                if remaining > 0:
                    return remaining
        return 0.0

    def failure(self, ip: str) -> None:
        now = time.monotonic()
        with self._lock:
            fails, _ = self._ip.get(ip, (0, 0.0))
            self._ip[ip] = (fails + 1, now)
            self._global.append(now)
            if len(self._ip) > 1000:
                self._ip.clear()

    def success(self, ip: str) -> None:
        with self._lock:
            self._ip.pop(ip, None)


class Auth:
    def __init__(self, db: Database, idle_hours: int, max_days: int):
        self.db = db
        self.idle = idle_hours * 3600
        self.max_age = max_days * 86400
        self.limiter = LoginLimiter()

    # Usuarios -----------------------------------------------------------
    def create_user(self, username: str, password: str) -> None:
        if not USERNAME_RE.match(username or ""):
            raise ValueError("Nombre de usuario inválido (minúsculas, 2-32 caracteres)")
        validate_new_password(password)
        self.db.execute("INSERT INTO users(username, pw_hash, created_at) VALUES(?,?,?)",
                        (username, hash_password(password), time.time()))

    def set_password(self, username: str, password: str) -> None:
        validate_new_password(password)
        cur = self.db.execute("UPDATE users SET pw_hash=? WHERE username=?", (hash_password(password), username))
        if cur.rowcount != 1:
            raise ValueError("Usuario inexistente")
        # Cambiar la contraseña invalida todas las sesiones abiertas.
        self.db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)", (username,))

    def user_count(self) -> int:
        return self.db.one("SELECT COUNT(*) FROM users")[0]

    def authenticate(self, username: str, password: str) -> int | None:
        row = self.db.one("SELECT id, pw_hash, disabled FROM users WHERE username=?", (username,))
        if row is None:
            verify_password(password, _DUMMY_HASH)
            return None
        if not verify_password(password, row["pw_hash"]) or row["disabled"]:
            return None
        return row["id"]

    # Sesiones -----------------------------------------------------------
    def create_session(self, user_id: int, ip: str, user_agent: str) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = time.time()
        self.db.execute(
            "INSERT INTO sessions(token_hash, user_id, csrf, created_at, last_seen, expires_at, ip, user_agent)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (token_hash(token), user_id, csrf, now, now, now + self.max_age, ip, (user_agent or "")[:200]))
        self.db.execute("DELETE FROM sessions WHERE expires_at < ? OR last_seen < ?", (now, now - self.idle))
        return token, csrf

    def get_session(self, token: str | None) -> Session | None:
        if not token or len(token) > 100:
            return None
        row = self.db.one(
            "SELECT s.id, s.user_id, s.csrf, s.last_seen, s.expires_at, u.username, u.disabled"
            " FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=?", (token_hash(token),))
        if row is None:
            return None
        now = time.time()
        if row["expires_at"] < now or row["last_seen"] < now - self.idle or row["disabled"]:
            self.db.execute("DELETE FROM sessions WHERE id=?", (row["id"],))
            return None
        if now - row["last_seen"] > 60:
            self.db.execute("UPDATE sessions SET last_seen=? WHERE id=?", (now, row["id"]))
        return Session(row["id"], row["user_id"], row["username"], row["csrf"])

    def destroy_session(self, session_id: int) -> None:
        self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
