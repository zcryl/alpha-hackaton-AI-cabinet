"""Пароли и сессии. Только стандартная библиотека."""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from . import config


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_password(password: str, iterations: int | None = None) -> str:
    iterations = iterations or config.pbkdf2_iterations()
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    """В базе хранится только хэш токена."""
    return hashlib.sha256(token.encode()).hexdigest()


def expiry_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=config.session_ttl_hours())).isoformat(timespec="seconds")
