"""SQLite storage. One connection per thread; the dashboard reads while the engine writes."""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at REAL,
    trigger TEXT NOT NULL,
    outcome TEXT,
    summary TEXT,
    kind TEXT NOT NULL DEFAULT 'decision'   -- decision (can change things) or discussion (can't)
);

CREATE TABLE IF NOT EXISTS research_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id INTEGER,
    speaker TEXT NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    ts REAL NOT NULL,
    speaker TEXT NOT NULL,          -- strategist, risk or system
    text TEXT NOT NULL,
    reasoning TEXT,
    orders TEXT,                    -- JSON
    verdict TEXT,
    tools TEXT,                     -- JSON
    model TEXT,
    cost_usd REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id INTEGER,
    source TEXT NOT NULL,           -- agents, plan, stop, target or flatten
    asset TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,              -- Money is stored as decimal text, never float.
    price TEXT NOT NULL,
    gross TEXT NOT NULL,
    fee TEXT NOT NULL,
    realized TEXT NOT NULL,
    venue_id TEXT,
    reason TEXT,
    liquidity TEXT                  -- maker (rested in the book) or taker (filled immediately)
);

CREATE TABLE IF NOT EXISTS snapshots (
    ts REAL PRIMARY KEY,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    benchmark REAL NOT NULL,
    realized REAL NOT NULL,
    unrealized REAL NOT NULL,
    fees REAL NOT NULL,
    api_cost REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS stops (
    asset TEXT PRIMARY KEY,
    stop_price TEXT NOT NULL,
    target_price TEXT,
    set_at REAL NOT NULL,
    session_id INTEGER
);

CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    session_id INTEGER,
    asset TEXT NOT NULL,
    quote_amount TEXT NOT NULL,
    trigger_price TEXT NOT NULL,
    direction TEXT NOT NULL,        -- below or above
    execution TEXT NOT NULL,
    stop_price TEXT NOT NULL,
    target_price TEXT NOT NULL,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL,           -- active, working, filled, expired or cancelled
    reason TEXT,
    note TEXT,
    closed_at REAL
);

CREATE TABLE IF NOT EXISTS resting_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id TEXT NOT NULL,
    placed_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    session_id INTEGER,
    plan_id INTEGER,
    source TEXT NOT NULL,
    asset TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    limit_price TEXT NOT NULL,
    stop_price TEXT,
    target_price TEXT,
    reason TEXT,
    booked_qty TEXT NOT NULL DEFAULT '0',
    booked_gross TEXT NOT NULL DEFAULT '0',
    booked_fee TEXT NOT NULL DEFAULT '0',
    status TEXT NOT NULL,           -- open, filled, partial or missed
    closed_at REAL
);

CREATE TABLE IF NOT EXISTS strategy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id INTEGER,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id INTEGER,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,            -- info, warn or critical
    kind TEXT NOT NULL,
    text TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON api_usage(ts);
CREATE INDEX IF NOT EXISTS idx_resting_status ON resting_orders(status);
CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status);
"""

# Columns added after the first release, for databases that already exist.
ADDED_COLUMNS = (
    ("fills", "liquidity", "TEXT"),
    ("stops", "target_price", "TEXT"),
    ("sessions", "kind", "TEXT NOT NULL DEFAULT 'decision'"),
)


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.conn.executescript(SCHEMA)
        for table, column, kind in ADDED_COLUMNS:
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    @contextmanager
    def tx(self):
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM state WHERE key = ?", (key,))
        return row["value"] if row else default

    def set(self, key: str, value) -> None:
        self.execute(
            "INSERT INTO state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def clear(self, key: str) -> None:
        self.execute("DELETE FROM state WHERE key = ?", (key,))

    def event(self, level: str, kind: str, text: str) -> None:
        self.execute(
            "INSERT INTO events(ts, level, kind, text) VALUES(?, ?, ?, ?)",
            (time.time(), level, kind, text),
        )
