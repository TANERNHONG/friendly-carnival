"""SQLite storage. All timestamps are epoch seconds (UTC)."""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Iterable

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword       TEXT NOT NULL,
    target_sgd    REAL NOT NULL,
    marketplace   TEXT,
    category_ids  TEXT,
    auto_track    INTEGER NOT NULL DEFAULT 1,
    active        INTEGER NOT NULL DEFAULT 1,
    notes         TEXT,
    created_at    REAL NOT NULL,
    last_scan_at  REAL
);

CREATE TABLE IF NOT EXISTS listings (
    item_id         TEXT PRIMARY KEY,
    legacy_id       TEXT,
    watchlist_id    INTEGER,
    title           TEXT,
    url             TEXT,
    image           TEXT,
    seller          TEXT,
    seller_feedback INTEGER,
    price           REAL,
    currency        TEXT,
    shipping        REAL,
    price_sgd       REAL,
    shipping_sgd    REAL,
    landed_sgd      REAL,
    bid_count       INTEGER,
    end_time        REAL,
    condition       TEXT,
    location        TEXT,
    verdict         TEXT,
    delta_pct       REAL,
    pred_landed_sgd REAL,
    prob_under      REAL,
    first_seen      REAL,
    last_seen       REAL,
    signalled_at    REAL,
    ignored         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_listings_end ON listings(end_time);

CREATE TABLE IF NOT EXISTS tracked (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         TEXT,
    legacy_id       TEXT UNIQUE,
    url             TEXT,
    title           TEXT,
    image           TEXT,
    seller          TEXT,
    seller_feedback INTEGER,
    keyword         TEXT,
    watchlist_id    INTEGER,
    target_sgd      REAL,
    currency        TEXT,
    start_price     REAL,
    current_price   REAL,
    current_sgd     REAL,
    shipping_sgd    REAL,
    bid_count       INTEGER,
    end_time        REAL,
    status          TEXT NOT NULL DEFAULT 'active',
    final_price     REAL,
    final_sgd       REAL,
    source          TEXT,
    added_at        REAL,
    ended_at        REAL,
    last_polled_at  REAL,
    alerts_sent     TEXT NOT NULL DEFAULT '[]',
    error           TEXT,
    demo            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_tracked_status ON tracked(status);

CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tracked_id    INTEGER NOT NULL,
    ts            REAL NOT NULL,
    price         REAL,
    price_sgd     REAL,
    bid_count     INTEGER,
    seconds_left  REAL
);
CREATE INDEX IF NOT EXISTS ix_snapshots ON snapshots(tracked_id, ts);

CREATE TABLE IF NOT EXISTS scan_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   REAL NOT NULL,
    finished_at  REAL,
    trigger      TEXT,
    keywords     INTEGER,
    items        INTEGER,
    deals        INTEGER,
    near         INTEGER,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL,
    ref        TEXT,
    title      TEXT,
    body       TEXT,
    delivered  INTEGER NOT NULL DEFAULT 0,
    error      TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
    return _conn


def _params(args: Iterable[Any] | dict) -> tuple | dict:
    return args if isinstance(args, dict) else tuple(args)


def query(sql: str, args: Iterable[Any] | dict = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in connect().execute(sql, _params(args)).fetchall()]


def one(sql: str, args: Iterable[Any] | dict = ()) -> dict | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: Iterable[Any] | dict = ()) -> int:
    """Run a write; returns lastrowid."""
    with _lock:
        cur = connect().execute(sql, _params(args))
        return cur.lastrowid


def executemany(sql: str, rows: Iterable[Iterable[Any]]) -> None:
    with _lock:
        conn = connect()
        conn.execute("BEGIN")
        try:
            conn.executemany(sql, [tuple(r) for r in rows])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def upsert(table: str, data: dict, key: str) -> None:
    cols = list(data)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != key)
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
        f"ON CONFLICT({key}) DO UPDATE SET {updates}"
    )
    execute(sql, [data[c] for c in cols])


def update(table: str, row_id: Any, data: dict, key: str = "id") -> None:
    if not data:
        return
    sets = ", ".join(f"{c}=?" for c in data)
    execute(f"UPDATE {table} SET {sets} WHERE {key}=?", [*data.values(), row_id])


def kv_get(key: str, default: Any = None) -> Any:
    row = one("SELECT value FROM kv WHERE key=?", [key])
    return json.loads(row["value"]) if row else default


def kv_set(key: str, value: Any) -> None:
    execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [key, json.dumps(value)],
    )
