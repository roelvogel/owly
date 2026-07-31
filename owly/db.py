"""SQLite persistence layer."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Iterable, Optional

from owly.config import get_settings


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK(type IN ('rss', 'stock', 'topic')),
    value TEXT NOT NULL,
    label TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    use_morss INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(type, value)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_slot TEXT NOT NULL CHECK(edition_slot IN ('morning', 'evening')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS editions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    edition_key TEXT NOT NULL,
    file_path TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url_hash TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    title TEXT,
    first_seen_at TEXT NOT NULL,
    run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_seen_items_hash ON seen_items(url_hash);
CREATE INDEX IF NOT EXISTS idx_editions_run ON editions(run_id);
CREATE INDEX IF NOT EXISTS idx_sources_type ON sources(type);
"""


@dataclass
class Source:
    id: int
    type: str
    value: str
    label: Optional[str]
    enabled: bool
    use_morss: bool
    created_at: str


@dataclass
class Run:
    id: int
    edition_slot: str
    started_at: str
    finished_at: Optional[str]
    status: str
    error: Optional[str]
    input_tokens: int
    output_tokens: int
    duration_ms: int


@dataclass
class Edition:
    id: int
    run_id: int
    edition_key: str
    file_path: str
    title: str
    created_at: str


def init_db(db_path: Optional[Path] = None) -> None:
    path = db_path or get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()
        _seed_defaults(conn)


def _seed_defaults(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    if count > 0:
        return
    now = _utcnow()
    defaults = [
        ("rss", "https://feeds.arstechnica.com/arstechnica/index", "Ars Technica", 0),
        ("rss", "https://www.theverge.com/rss/index.xml", "The Verge", 0),
        ("rss", "https://hnrss.org/frontpage", "Hacker News", 0),
        ("topic", "AI agents", "AI Agents", 0),
        ("topic", "mobile architecture", "Mobile Architecture", 0),
    ]
    conn.executemany(
        "INSERT INTO sources (type, value, label, use_morss, created_at) VALUES (?, ?, ?, ?, ?)",
        [(t, v, label, morss, now) for t, v, label, morss in defaults],
    )
    conn.commit()


@contextmanager
def get_db(db_path: Optional[Path] = None) -> Generator[sqlite3.Connection, None, None]:
    path = db_path or get_settings().db_path
    init_db(path)
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        id=row["id"],
        type=row["type"],
        value=row["value"],
        label=row["label"],
        enabled=bool(row["enabled"]),
        use_morss=bool(row["use_morss"]),
        created_at=row["created_at"],
    )


def list_sources(
    conn: sqlite3.Connection,
    source_type: Optional[str] = None,
    enabled_only: bool = False,
) -> list[Source]:
    query = "SELECT * FROM sources WHERE 1=1"
    params: list = []
    if source_type:
        query += " AND type = ?"
        params.append(source_type)
    if enabled_only:
        query += " AND enabled = 1"
    query += " ORDER BY type, value"
    rows = conn.execute(query, params).fetchall()
    return [_row_to_source(r) for r in rows]


def add_source(
    conn: sqlite3.Connection,
    source_type: str,
    value: str,
    label: Optional[str] = None,
    use_morss: bool = False,
) -> Source:
    now = _utcnow()
    value = value.strip()
    label = (label or value).strip()
    conn.execute(
        """
        INSERT INTO sources (type, value, label, use_morss, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(type, value) DO UPDATE SET
            label = excluded.label,
            use_morss = excluded.use_morss,
            enabled = 1
        """,
        (source_type, value, label, int(use_morss), now),
    )
    row = conn.execute(
        "SELECT * FROM sources WHERE type = ? AND value = ?",
        (source_type, value),
    ).fetchone()
    return _row_to_source(row)


def delete_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


def toggle_source(conn: sqlite3.Connection, source_id: int, enabled: bool) -> None:
    conn.execute(
        "UPDATE sources SET enabled = ? WHERE id = ?",
        (int(enabled), source_id),
    )


def create_run(conn: sqlite3.Connection, edition_slot: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (edition_slot, started_at, status) VALUES (?, ?, 'running')",
        (edition_slot, _utcnow()),
    )
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    error: Optional[str] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    duration_ms: int = 0,
) -> None:
    conn.execute(
        """
        UPDATE runs SET
            finished_at = ?,
            status = ?,
            error = ?,
            input_tokens = ?,
            output_tokens = ?,
            duration_ms = ?
        WHERE id = ?
        """,
        (_utcnow(), status, error, input_tokens, output_tokens, duration_ms, run_id),
    )


def add_edition(
    conn: sqlite3.Connection,
    run_id: int,
    edition_key: str,
    file_path: str,
    title: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO editions (run_id, edition_key, file_path, title, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, edition_key, file_path, title, _utcnow()),
    )
    return cur.lastrowid


def list_editions(conn: sqlite3.Connection, limit: int = 50) -> list[Edition]:
    rows = conn.execute(
        """
        SELECT * FROM editions
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        Edition(
            id=r["id"],
            run_id=r["run_id"],
            edition_key=r["edition_key"],
            file_path=r["file_path"],
            title=r["title"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def list_runs(conn: sqlite3.Connection, limit: int = 20) -> list[Run]:
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        Run(
            id=r["id"],
            edition_slot=r["edition_slot"],
            started_at=r["started_at"],
            finished_at=r["finished_at"],
            status=r["status"],
            error=r["error"],
            input_tokens=r["input_tokens"] or 0,
            output_tokens=r["output_tokens"] or 0,
            duration_ms=r["duration_ms"] or 0,
        )
        for r in rows
    ]


def url_hash(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def is_seen(conn: sqlite3.Connection, item_url: str) -> bool:
    h = url_hash(item_url)
    row = conn.execute(
        "SELECT 1 FROM seen_items WHERE url_hash = ?",
        (h,),
    ).fetchone()
    return row is not None


def mark_seen(
    conn: sqlite3.Connection,
    items: Iterable[tuple[str, Optional[str]]],
    run_id: Optional[int] = None,
) -> None:
    now = _utcnow()
    for item_url, title in items:
        h = url_hash(item_url)
        conn.execute(
            """
            INSERT OR IGNORE INTO seen_items (url_hash, url, title, first_seen_at, run_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (h, item_url, title, now, run_id),
        )


def prune_seen_items(conn: sqlite3.Connection, keep_days: int = 14) -> int:
    """Remove seen items older than keep_days to avoid unbounded growth."""
    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    cur = conn.execute(
        "DELETE FROM seen_items WHERE first_seen_at < ?",
        (cutoff_iso,),
    )
    return cur.rowcount
