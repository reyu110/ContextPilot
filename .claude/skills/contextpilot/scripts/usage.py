"""Project-local, numeric handoff estimates. Never reads Claude transcripts."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from scan import SECRET_CONTENT, safe_path

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
DB_NAME = ".contextpilot/usage.sqlite3"


def identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value) or SECRET_CONTENT.search(value):
        raise ValueError("Session and prompt IDs must be 1–128 letters, digits, dots, underscores or hyphens")
    return value


def database_path(root: Path) -> Path:
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = safe_path(root, DB_NAME + suffix)
        try:
            mode = path.stat(follow_symlinks=False).st_mode
        except FileNotFoundError:
            continue  # Another SQLite writer may have just removed its journal.
        if not stat.S_ISREG(mode):
            raise ValueError("Usage database paths must be regular files")
    return safe_path(root, DB_NAME)


def record(root: Path, baseline_bytes: int, handoff_bytes: int, cache_bytes: int,
           session_id: str = "unassigned", prompt_id: str | None = None) -> str:
    session_id = identifier(session_id)
    prompt_id = identifier(prompt_id or uuid.uuid4().hex)
    if any(type(n) is not int or not 0 <= n <= 10**12 for n in (baseline_bytes, handoff_bytes, cache_bytes)):
        raise ValueError("Usage byte counts must be nonnegative integers up to one trillion")
    path = database_path(root)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    # Precreate privately; SQLite's journal inherits the database's permissions.
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, session_id TEXT NOT NULL,
            prompt_id TEXT NOT NULL, baseline_tokens INTEGER NOT NULL,
            handoff_tokens INTEGER NOT NULL, cache_bytes INTEGER NOT NULL,
            UNIQUE(session_id, prompt_id))""")
        db.execute("CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id)")
        db.execute("CREATE INDEX IF NOT EXISTS events_time ON events(created_at)")
        db.execute("""INSERT OR IGNORE INTO events
            (created_at, session_id, prompt_id, baseline_tokens, handoff_tokens, cache_bytes)
            VALUES (?, ?, ?, ?, ?, ?)""", (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       session_id, prompt_id, (baseline_bytes + 3) // 4, (handoff_bytes + 3) // 4, cache_bytes))
    return prompt_id


TOTALS = """COUNT(*) AS prompts, COALESCE(SUM(baseline_tokens), 0) AS baseline_tokens,
    COALESCE(SUM(handoff_tokens), 0) AS handoff_tokens,
    COALESCE(SUM(baseline_tokens - handoff_tokens), 0) AS saved_tokens,
    COALESCE(SUM(cache_bytes), 0) AS cache_bytes"""


def summary(root: Path, session_id: str | None = None, offset: int = 0) -> dict:
    if session_id is not None:
        identifier(session_id)
    if type(offset) is not int or not 0 <= offset <= 10**9:
        raise ValueError("Invalid prompt page")
    result = {"project": root.name, "lifetime": {"prompts": 0, "baseline_tokens": 0,
              "handoff_tokens": 0, "saved_tokens": 0, "cache_bytes": 0},
              "sessions": [], "session_count": 0, "daily": [], "prompts": [],
              "selected_session": session_id, "offset": offset, "page_size": 50, "first_recorded": None}
    path = database_path(root)
    if not path.exists():
        result["selected"] = result["lifetime"].copy()
        return result
    # Dashboard reads must not create a database or modify its schema.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        def rows(sql: str, params: tuple = ()) -> list[dict]:
            return [dict(row) for row in db.execute(sql, params)]
        result["lifetime"] = rows("SELECT " + TOTALS + " FROM events")[0]
        result["first_recorded"] = db.execute("SELECT MIN(created_at) FROM events").fetchone()[0]
        result["session_count"] = db.execute("SELECT COUNT(DISTINCT session_id) FROM events").fetchone()[0]
        result["sessions"] = rows("SELECT session_id, " + TOTALS +
                                  " FROM events GROUP BY session_id ORDER BY MAX(id) DESC LIMIT 100")
        where, params = (" WHERE session_id = ?", (session_id,)) if session_id else ("", ())
        result["selected"] = rows("SELECT " + TOTALS + " FROM events" + where, params)[0]
        result["prompts"] = rows("""SELECT id, created_at, session_id, prompt_id, baseline_tokens,
            handoff_tokens, baseline_tokens - handoff_tokens AS saved_tokens, cache_bytes
            FROM events""" + where + " ORDER BY id DESC LIMIT 50 OFFSET ?", (*params, offset))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=29)).date().isoformat()
        result["daily"] = rows("SELECT substr(created_at, 1, 10) AS day, " + TOTALS +
            " FROM events" + (where + " AND " if where else " WHERE ") +
            "created_at >= ? GROUP BY day ORDER BY day", (*params, cutoff))
    return result
