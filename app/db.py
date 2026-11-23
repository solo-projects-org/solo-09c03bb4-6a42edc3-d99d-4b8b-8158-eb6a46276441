"""SQLite storage.

Two report tables hold the latest complete upload of each CSV; a third table
stores per-exhibit verdicts.  A separate ``config`` table keeps the uniform
illuminance cap and the dose threshold first supplied at initial upload.

The database is the single source of truth: each replacement report and its
recomputed verdicts are committed in one transaction, so a failed report leaves
both data and assessment list untouched.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS placements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    exhibit_id  TEXT NOT NULL,
    cabinet_id  TEXT NOT NULL,
    start_min   INTEGER NOT NULL,
    end_min     INTEGER NOT NULL,
    CHECK (end_min > start_min),
    UNIQUE (exhibit_id, cabinet_id, start_min, end_min)
);

CREATE INDEX IF NOT EXISTS idx_placements_exhibit ON placements(exhibit_id);
CREATE INDEX IF NOT EXISTS idx_placements_cabinet_time
    ON placements(cabinet_id, start_min, end_min);

CREATE TABLE IF NOT EXISTS hourly_doses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cabinet_id  TEXT NOT NULL,
    hour_min    INTEGER NOT NULL,
    dose        REAL NOT NULL,
    UNIQUE (cabinet_id, hour_min)
);

CREATE INDEX IF NOT EXISTS idx_hourly_cabinet ON hourly_doses(cabinet_id, hour_min);

CREATE TABLE IF NOT EXISTS assessments (
    exhibit_id     TEXT PRIMARY KEY,
    total_minutes  INTEGER NOT NULL,
    total_hours    INTEGER NOT NULL,
    dose_low       REAL NOT NULL,
    dose_high      REAL NOT NULL,
    dose_threshold REAL NOT NULL,
    status         TEXT NOT NULL,
    detail_json    TEXT NOT NULL,
    computed_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_meta (
    kind        TEXT PRIMARY KEY,
    row_count   INTEGER NOT NULL,
    reported_at TEXT NOT NULL,
    source_name TEXT NOT NULL,
    seq         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sequence (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    value INTEGER NOT NULL
);

INSERT OR IGNORE INTO sequence (id, value) VALUES (1, 0);
"""


class Store:
    """Thin SQLite wrapper; one connection per operation, WAL mode."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def init_schema(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Begin an IMMEDIATE transaction so concurrent writers queue up.

        SQLite serialises writers; IMMEDIATE acquires the write lock up front
        and therefore commits full reports strictly in submission order.
        """

        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
