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
    grp TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'unknown',
    consecutive_fail INTEGER NOT NULL DEFAULT 0,
    last_check INTEGER,
    last_rtt REAL,
    last_change INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
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

CREATE TABLE IF NOT EXISTS checks_hourly (
    host_id INTEGER NOT NULL,
    hour INTEGER NOT NULL,
    n INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    sent INTEGER NOT NULL,
    recv INTEGER NOT NULL,
    rtt_sum REAL NOT NULL DEFAULT 0,
    rtt_cnt INTEGER NOT NULL DEFAULT 0,
    rtt_min REAL,
    rtt_max REAL,
    PRIMARY KEY (host_id, hour)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_hourly_hour ON checks_hourly(hour);

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
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -8000")
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
        cols = [r[1] for r in conn.execute("PRAGMA table_info(hosts)")]
        if "grp" not in cols:
            conn.execute("ALTER TABLE hosts ADD COLUMN grp TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hosts_grp ON hosts(grp)")
        conn.execute("INSERT OR IGNORE INTO groups(name) SELECT DISTINCT grp FROM hosts WHERE grp != ''")
        if conn.execute("SELECT 1 FROM settings WHERE key = 'hourly_from'").fetchone() is None:
            cutoff = int(time.time()) // 3600 * 3600
            conn.execute("INSERT INTO settings(key, value) VALUES ('hourly_from', ?)", (str(cutoff),))
            _rollup(conn, cutoff, None)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts)")
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO users(username, password_hash, role, must_change, created_at) VALUES (?,?,?,?,?)",
                ("admin", generate_password_hash("admin"), "admin", 1, int(time.time())),
            )


ROLLUP_SQL = (
    "INSERT OR IGNORE INTO checks_hourly(host_id, hour, n, ok, sent, recv, rtt_sum, rtt_cnt, rtt_min, rtt_max) "
    "SELECT host_id, ts / 3600 * 3600, COUNT(*), SUM(received > 0), SUM(sent), SUM(received), "
    "COALESCE(SUM(rtt_avg), 0), COUNT(rtt_avg), MIN(rtt_avg), MAX(rtt_avg) FROM checks "
    "WHERE ts >= ? {end} GROUP BY host_id, ts / 3600")


def _rollup(conn, start, end):
    if end is None:
        conn.execute(ROLLUP_SQL.format(end=""), (start,))
    else:
        conn.execute(ROLLUP_SQL.format(end="AND ts < ?"), (start, end))


def backfill_hourly(stop=None):
    if get_text("hourly_done") == "1":
        return 0
    cutoff = int(get_text("hourly_from") or 0)
    first = query_one("SELECT MIN(ts) m FROM checks")["m"]
    done = 0
    if first is not None:
        end = cutoff
        while end > first and not (stop and stop.is_set()):
            start = max(first // 3600 * 3600, end - 86400)
            with tx() as conn:
                _rollup(conn, start, end)
            end = start
            done += 1
        if stop and stop.is_set():
            return done
    set_settings({"hourly_done": "1"})
    return done


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
