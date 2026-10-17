"""Contraseñas (PBKDF2-SHA256) y sesiones firmadas con HMAC. Sólo biblioteca estándar."""
import base64
import hashlib
import hmac
import os
import secrets
import time

ITERATIONS = 300_000


def hash_password(password, iterations=ITERATIONS):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"pbkdf2_sha256${iterations}${b64(salt)}${b64(dk)}"


def verify_password(password, stored):
    try:
        algo, it, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def load_secret(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        if len(data) >= 32:
            return data
    except FileNotFoundError:
        pass
    data = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return data


def make_token(secret, user, hours):
    payload = f"{user}|{int(time.time() + hours * 3600)}"
    p64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(secret, p64.encode(), hashlib.sha256).hexdigest()
    return f"{p64}.{sig}"


def check_token(secret, token, user):
    try:
        p64, sig = token.split(".")
        good = hmac.new(secret, p64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return False
        tok_user, exp = base64.urlsafe_b64decode(p64 + "=" * (-len(p64) % 4)).decode().split("|")
        return tok_user == user and int(exp) > time.time()
    except (ValueError, TypeError, UnicodeDecodeError):
        return False
