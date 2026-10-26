"""SQLite: схема, подключение, начальные пользователи."""
import json
import sqlite3

from . import config, security

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('client','officer')), full_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id), product TEXT NOT NULL,
  status TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', checks TEXT, score TEXT, decision TEXT,
  clarification TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
  at TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
  from_status TEXT, to_status TEXT, details TEXT
);
CREATE INDEX IF NOT EXISTS idx_app_owner ON applications(owner_id);
CREATE INDEX IF NOT EXISTS idx_events_app ON events(application_id);
"""

DEMO_USERS = [
    ("client", "client", "Иванов А. А. (ИП)"),
    ("client2", "client", "Смирнов Д. В. (ИП)"),
    ("client3", "client", "Петров И. С. (ИП)"),
    ("officer", "officer", "Сотрудник банка (демо)"),
]


def connect() -> sqlite3.Connection:
    d = config.data_dir()
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / "cabinet.db", timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            for username, role, name in DEMO_USERS:
                conn.execute("INSERT INTO users(username,password_hash,role,full_name) VALUES (?,?,?,?)",
                             (username, security.hash_password(config.demo_password()), role, name))
        conn.commit()
    finally:
        conn.close()


def reset_demo() -> None:
    conn = connect()
    try:
        conn.executescript("DELETE FROM events; DELETE FROM applications; DELETE FROM sessions;")
        conn.commit()
    finally:
        conn.close()


def loads(value: str | None):
    return json.loads(value) if value else None


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)
