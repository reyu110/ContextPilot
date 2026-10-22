"""Read-only optimizer history, including old ledgers. No schema changes on reads."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import subprocess

from policies import FEATURES, PLATFORM_LIMITS
from usage import database_path, identifier
from instruction_audit import saved_audit

METRICS = """COUNT(*) AS actions, COALESCE(SUM(before_bytes),0) AS before_bytes,
 COALESCE(SUM(after_bytes),0) AS after_bytes,
 COALESCE(SUM(before_bytes-after_bytes),0) AS net_bytes,
 COALESCE(SUM((before_bytes+3)/4-(after_bytes+3)/4),0) AS net_tokens"""


def empty_totals() -> dict:
    return {"actions": 0, "before_bytes": 0, "after_bytes": 0, "net_bytes": 0, "net_tokens": 0}


def report(root: Path, session_id: str | None = None, offset: int = 0, *, agent: str = "claude-code") -> dict:
    if agent not in {"claude-code","codex"}:
        raise ValueError("Unknown optimizer agent")
    if session_id is not None:
        identifier(session_id)
    if type(offset) is not int or not 0 <= offset <= 10**9:
        raise ValueError("Invalid optimizer page")
    result = {"lifetime": empty_totals(), "selected": empty_totals(), "sessions": [],
              "prompts": [], "prompt_count": 0, "daily": [], "features": [],
              "last_activity": None, "native": None, "offset": offset, "page_size": 50,
              "legacy_actions": 0,
              "limitations": [{"label":label,"reason":reason} for label,reason in PLATFORM_LIMITS]}
    try:
        result["instruction_audit"] = saved_audit(root,agent=agent)
    except (OSError,ValueError,subprocess.SubprocessError):
        result["instruction_audit"] = None
        result["audit_warning"] = "Instruction audit is unavailable or its paths are no longer eligible; artifacts were preserved."
    path = database_path(root, ".contextpilot/codex.sqlite3" if agent == "codex" else ".contextpilot/optimizer.sqlite3")
    if path.exists():
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {r[1] for r in db.execute("PRAGMA table_info(reductions)")}
            def rows(sql: str, values: tuple = ()) -> list[dict]:
                return [dict(r) for r in db.execute(sql, values)]
            where, values = (" WHERE session_id=?", (session_id,)) if session_id else ("", ())
            result["lifetime"] = rows("SELECT " + METRICS + " FROM reductions")[0]
            result["selected"] = rows("SELECT " + METRICS + " FROM reductions" + where, values)[0]
            result["sessions"] = rows("SELECT session_id, " + METRICS + " FROM reductions GROUP BY session_id ORDER BY MAX(id) DESC LIMIT 100")
            result["prompt_count"] = db.execute("SELECT COUNT(*) FROM (SELECT session_id,prompt_id FROM reductions" + where + " GROUP BY session_id,prompt_id)", values).fetchone()[0]
            stamp = "MAX(created_at)" if "created_at" in columns else "NULL"
            result["prompts"] = rows("SELECT session_id,prompt_id," + stamp + " AS created_at," + METRICS + " FROM reductions" + where + " GROUP BY session_id,prompt_id ORDER BY MAX(id) DESC LIMIT 50 OFFSET ?", (*values, offset))
            for prompt in result["prompts"]:
                prompt["events"] = rows("SELECT tool_id,kind,before_bytes,after_bytes FROM reductions WHERE session_id=? AND prompt_id=? ORDER BY id DESC LIMIT 20", (prompt["session_id"],prompt["prompt_id"]))
                prompt["events_truncated"] = prompt["actions"] > 20
            feature_rows = rows("SELECT kind, " + METRICS + " FROM reductions" + where + " GROUP BY kind", values)
            if "created_at" in columns:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=29)).date().isoformat()
                result["daily"] = rows("SELECT substr(created_at,1,10) AS day," + METRICS + " FROM reductions" + (where + " AND " if where else " WHERE ") + "created_at>=? GROUP BY day ORDER BY day", (*values, cutoff))
                result["legacy_actions"] = db.execute("SELECT COUNT(*) FROM reductions WHERE created_at IS NULL").fetchone()[0]
            else:
                result["legacy_actions"] = result["lifetime"]["actions"]
            if "sessions" in tables:
                result["last_activity"] = db.execute("SELECT MAX(touched) FROM sessions").fetchone()[0]
            if "native" in tables:
                native_where = " WHERE session_id=?" if session_id else ""
                native = rows("SELECT * FROM native" + native_where + " ORDER BY updated_at DESC LIMIT 1", values)
                result["native"] = native[0] if native else None
    else:
        feature_rows = []
    observed = {r["kind"]: r for r in feature_rows}
    for feature_id, label, mode, detail in FEATURES:
        if agent == "codex":
            if feature_id in {"bounded","duplicate","terminal","adaptive","errors","search_duplicate","budget","native","usage","concise","effort","compaction","mcp","cache"}:
                mode,detail = "unavailable","Not implemented by the Codex adapter; no tool-output replacement or native settings changes."
            elif feature_id == "checkpoint":
                detail = "Bounded repository checkpoint before compaction; no session-end scan within the short shutdown deadline."
            elif feature_id == "instructions":
                detail = "Bounded root AGENTS.md/AGENTS.override.md audit; no instruction rewriting."
        result["features"].append({"id": feature_id, "label": label, "mode": mode, "detail": detail,
                                   **observed.get(feature_id, empty_totals())})
    # Session instructions are real added context, not a savings claim for each guided feature.
    result["guidance"] = observed.get("guidance", empty_totals())
    if agent == "codex":
        result["native"] = None
        result["limitations"].append({"label":"Claude-only automatic filters and native readings","reason":"Codex uses Bash hooks and a different output contract. This adapter never grants permissions, replaces tool output, or reads private usage history."})
    return result
