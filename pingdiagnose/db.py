import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from werkzeug.security import generate_password_hash

from .config import data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'viewer')),
    must_change INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    ip TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'unknown',
    consecutive_fail INTEGER NOT NULL DEFAULT 0,
    last_check INTEGER,
    last_rtt REAL,
    last_change INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    ts INTEGER NOT NULL,
    sent INTEGER NOT NULL,
    received INTEGER NOT NULL,
    rtt_avg REAL
);
CREATE INDEX IF NOT EXISTS idx_checks_host_ts ON checks(host_id, ts);
CREATE INDEX IF NOT EXISTS idx_checks_ts ON checks(ts);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id INTEGER REFERENCES hosts(id) ON DELETE CASCADE,
    ts INTEGER NOT NULL,
    type TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_host ON events(host_id, ts);

DROP TABLE IF EXISTS push_subs;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "interval_seconds": "180",
    "ping_count": "2",
    "ping_timeout_ms": "1000",
    "fail_threshold": "3",
    "retention_days": "180",
}

_local = threading.local()


def db_path():
    return os.path.join(data_dir(), "pingdiagnose.db")


def connect():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(db_path(), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        _local.conn = conn
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def query(sql, params=()):
    return [dict(r) for r in connect().execute(sql, params).fetchall()]


def query_one(sql, params=()):
    row = connect().execute(sql, params).fetchone()
    return dict(row) if row else None


def execute(sql, params=()):
    with tx() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


def init_db():
    with tx() as conn:
        conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO users(username, password_hash, role, must_change, created_at) VALUES (?,?,?,?,?)",
                ("admin", generate_password_hash("admin"), "admin", 1, int(time.time())),
            )


def get_settings():
    s = dict(DEFAULT_SETTINGS)
    for r in query("SELECT key, value FROM settings"):
        if r["key"] in s:
            s[r["key"]] = r["value"]
    return {k: int(v) for k, v in s.items()}


def get_text(key, default=""):
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_settings(values):
    with tx() as conn:
        for k, v in values.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, str(v)),
            )


def reset_admin(password="admin"):
    init_db()
    with tx() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
        h = generate_password_hash(password)
        if row:
            conn.execute(
                "UPDATE users SET password_hash = ?, role = 'admin', must_change = 1 WHERE id = ?",
                (h, row[0]),
            )
        else:
            conn.execute(
                "INSERT INTO users(username, password_hash, role, must_change, created_at) VALUES (?,?,?,?,?)",
                ("admin", h, "admin", 1, int(time.time())),
            )
