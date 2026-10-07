"""SQLite storage."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY,
    url        TEXT NOT NULL UNIQUE,
    source     TEXT NOT NULL,
    section    TEXT NOT NULL,
    weekly     INTEGER NOT NULL DEFAULT 0,
    title      TEXT NOT NULL,
    summary    TEXT NOT NULL DEFAULT '',
    published  TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    body       TEXT
);
CREATE INDEX IF NOT EXISTS items_published ON items(published);

CREATE TABLE IF NOT EXISTS digests (
    date       TEXT PRIMARY KEY,
    json       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- items that made it into a digest (novelty filter)
CREATE TABLE IF NOT EXISTS used_items (
    item_id INTEGER NOT NULL REFERENCES items(id),
    date    TEXT NOT NULL,
    PRIMARY KEY (item_id, date)
);

CREATE TABLE IF NOT EXISTS concepts (
    name        TEXT PRIMARY KEY COLLATE NOCASE,
    explanation TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    times_seen  INTEGER NOT NULL DEFAULT 1,
    understood  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS threads (
    slug       TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    summary    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- reader feedback: clicks from email links, comments from replies / web form / CLI
CREATE TABLE IF NOT EXISTS feedback (
    id        INTEGER PRIMARY KEY,
    at        TEXT NOT NULL,
    date      TEXT NOT NULL DEFAULT '',  -- digest date the feedback refers to
    ref       TEXT NOT NULL DEFAULT '',  -- story ref within that digest
    kind      TEXT NOT NULL,             -- up | down | more | got | comment
    concept   TEXT NOT NULL DEFAULT '',
    text      TEXT NOT NULL DEFAULT '',
    channel   TEXT NOT NULL,             -- web | email | cli
    processed INTEGER NOT NULL DEFAULT 0
);

-- standing preferences distilled from comments, passed to every prompt
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS source_runs (
    source  TEXT NOT NULL,
    ran_at  TEXT NOT NULL,
    ok      INTEGER NOT NULL,
    n_items INTEGER NOT NULL,
    error   TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
