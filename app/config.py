"""Настройки читаются из переменных окружения в момент вызова (удобно для тестов)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def fixtures_dir() -> Path:
    return Path(os.environ.get("FIXTURES_DIR", BASE_DIR / "fixtures"))


def data_dir() -> Path:
    """Каталог с базой SQLite. В Docker это том /data."""
    return Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))


def demo_mode() -> bool:
    """В демо-режиме доступны сброс данных и загрузка демо-заявок."""
    return os.environ.get("DEMO_MODE", "1") == "1"


def demo_password() -> str:
    return os.environ.get("DEMO_PASSWORD", "demo123")


def session_ttl_hours() -> int:
    return int(os.environ.get("SESSION_TTL_HOURS", "12"))


def pbkdf2_iterations() -> int:
    return int(os.environ.get("PBKDF2_ITERATIONS", "120000"))
