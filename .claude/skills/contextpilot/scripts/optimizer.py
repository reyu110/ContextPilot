#!/usr/bin/env python3
"""Opt-in Claude Code hooks for bounded Reads and prompt-local duplicate results."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from scan import (CONFIGS, SECRET_CONTENT, TEXT_SUFFIXES, allowed_path, ensure_ignored,
                  ignored_paths, json_bytes, load_json, prepare_ignore_engine,
                  read_local, safe_path, write_artifact)
from usage import database_path, identifier

SETTINGS = ".claude/settings.local.json"
DB = ".contextpilot/optimizer.sqlite3"
STATE = "optimizer-config.json"
THRESHOLD = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"
EVENTS = {"SessionStart": "", "UserPromptSubmit": "", "PreCompact": "",
          "SessionEnd": "", "PreToolUse": "Read", "PostToolUse": "Read"}
MAX_INPUT = 8 * 1024 * 1024


def read_settings(root: Path) -> dict:
    try:
        raw = read_local(root, SETTINGS, 1048577)
    except FileNotFoundError:
        return {}
    if len(raw) > 1048576:
        raise ValueError("Local settings exceed the 1 MiB edit limit")
    settings = json.loads(raw)
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict) or not isinstance(settings.get("env", {}), dict):
        raise ValueError("Local settings, hooks and env must be JSON objects")
    for entries in settings.get("hooks", {}).values():
        if not isinstance(entries, list) or any(not isinstance(entry, dict) or
                not isinstance(entry.get("hooks"), list) for entry in entries):
            raise ValueError("Existing hook configuration is malformed; preserve it and fix it first")
    return settings


def check_version() -> None:
    result = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10, check=True)
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout)
    if not match or tuple(map(int, match.groups())) < (2, 1, 303):
        raise ValueError("Live optimization requires Claude Code 2.1.303 or newer")


def handlers(root: Path) -> dict:
    command = {"type": "command", "command": sys.executable,
               "args": [str(Path(__file__).resolve()), "hook", "--root", str(root)],
               "timeout": 5, "onFailure": "continue"}
    return {event: {"matcher": matcher, "hooks": [command]} for event, matcher in EVENTS.items()}


@contextmanager
def config_lock(root: Path):
    path = safe_path(root, ".contextpilot/optimizer-config.lock")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.close(fd)
        yield
    finally:
        path.unlink()


def configure(root: Path, enabled: bool) -> dict:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Project root must be a directory")
    safe_path(root, SETTINGS)
    if enabled:
        database_path(root, DB)
    settings = read_settings(root)  # Validate before creating any artifacts.
    if not enabled and load_json(root, STATE) is None:
        return status(root)
    if enabled:
        if settings.get("disableAllHooks") is True:
            raise ValueError("Local Claude settings disable hooks; ContextPilot will not override that setting")
        check_version()
    with config_lock(root):
        settings = read_settings(root)
        old = load_json(root, STATE) or {}
        if set(old) - {"enabled", "handlers", "threshold_added"} or not isinstance(old.get("handlers", {}), dict):
            raise ValueError("Optimizer ownership record is malformed; preserve it before repairing")
        owned = old.get("handlers", {})
        expected = handlers(root)
        for event, entry in owned.items():
            if event not in expected or not isinstance(entry, dict):
                raise ValueError("Optimizer ownership record contains an unknown hook")
            previous_commands = entry.get("hooks", [])
            if len(previous_commands) != 1 or not isinstance(previous_commands[0], dict):
                raise ValueError("Optimizer ownership record contains an invalid handler")
            previous = previous_commands[0]
            expected_command = expected[event]["hooks"][0]
            if (set(entry) != {"matcher", "hooks"} or entry["matcher"] != EVENTS[event]
                    or set(previous) - {"type", "command", "args", "timeout", "onFailure"}
                    or previous.get("type") != "command" or previous.get("args") != expected_command["args"]
                    or not isinstance(previous.get("command"), str) or not Path(previous["command"]).is_absolute()):
                raise ValueError("Optimizer ownership record does not match this installed runtime")
        hooks = settings.get("hooks", {})
        for event, entry in owned.items():
            if event in hooks:
                hooks[event] = [item for item in hooks[event] if item != entry]
                if not hooks[event]:
                    del hooks[event]
        env = settings.get("env", {})
        threshold_added = old.get("threshold_added", False) is True
        if enabled:
            owned = handlers(root)
            for event, entry in owned.items():
                hooks.setdefault(event, []).append(entry)
            if THRESHOLD not in env and THRESHOLD not in os.environ:
                env[THRESHOLD] = "70"
                threshold_added = True
            settings["hooks"] = hooks
            settings["env"] = env
            ensure_ignored(root, local_settings=True)
        else:
            if threshold_added and env.get(THRESHOLD) == "70":
                del env[THRESHOLD]
            if not hooks:
                settings.pop("hooks", None)
            if not env:
                settings.pop("env", None)
        state = {"enabled": enabled, "handlers": owned if enabled else {},
                 "threshold_added": threshold_added if enabled else False}
        # Keep only our ownership metadata, never a backup of settings containing credentials.
        write_artifact(root, STATE, json_bytes(state))
        destination = safe_path(root, SETTINGS)
        if enabled or destination.exists():
            destination.parent.mkdir(exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".optimizer-settings-", dir=root / ".contextpilot")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(json_bytes(settings))
                    stream.flush()
                    os.fsync(stream.fileno())
                safe_path(root, SETTINGS)
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    return {**status(root), "restart_required": True}


def status(root: Path) -> dict:
    root = root.resolve(strict=True)
    state = load_json(root, STATE) or {}
    settings = read_settings(root)
    expected = state.get("handlers", {})
    registered = bool(expected) and all(entry in settings.get("hooks", {}).get(event, []) for event, entry in expected.items())
    percent = settings.get("env", {}).get(THRESHOLD)
    percent = int(percent) if isinstance(percent, str) and percent.isascii() and percent.isdigit() and 1 <= int(percent) <= 100 else None
    result = {"enabled": state.get("enabled") is True and registered,
              "hooks_registered": registered, "compaction_percent": percent}
    try:
        result.update(totals(root))
    except (OSError, ValueError, sqlite3.Error):
        result["history_warning"] = "Optimizer history is unreadable; it has been preserved."
    return result


@contextmanager
def history(root: Path):
    path = database_path(root, DB)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("""CREATE TABLE IF NOT EXISTS turns (
            session_id TEXT PRIMARY KEY, prompt_id TEXT NOT NULL, touched REAL NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS seen (
            session_id TEXT NOT NULL, input_hash TEXT NOT NULL, output_hash TEXT NOT NULL,
            tool_id TEXT NOT NULL, PRIMARY KEY(session_id, input_hash, output_hash))""")
        db.execute("""CREATE TABLE IF NOT EXISTS reads (
            session_id TEXT NOT NULL, tool_id TEXT NOT NULL, prompt_id TEXT NOT NULL,
            PRIMARY KEY(session_id, tool_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS reductions (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, prompt_id TEXT NOT NULL,
            tool_id TEXT NOT NULL, kind TEXT NOT NULL, before_bytes INTEGER NOT NULL,
            after_bytes INTEGER NOT NULL, UNIQUE(session_id, tool_id, kind))""")
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        yield db


def eligible_file(root: Path, raw: object) -> tuple[Path, int] | None:
    if not isinstance(raw, str):
        return None
    path = Path(raw)
    if not path.is_absolute():
        return None
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    if not allowed_path(relative) or (path.suffix.lower() not in TEXT_SUFFIXES and path.name.lower() not in CONFIGS):
        return None
    path = safe_path(root, relative.as_posix())
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        return None
    with tempfile.TemporaryDirectory(prefix="contextpilot-ignore-") as temporary:
        engine = prepare_ignore_engine(root, temporary)
        if relative.as_posix() in ignored_paths(root, [relative.as_posix()], engine):
            return None
    return path, info.st_size


def hook(root: Path, payload: dict) -> dict:
    state = load_json(root, STATE) or {}
    if state.get("enabled") is not True or payload.get("agent_id"):
        return {}
    session = identifier(payload.get("session_id"))
    cwd = Path(payload.get("cwd", "")).resolve(strict=True)
    if not cwd.is_relative_to(root):
        return {}
    event = payload.get("hook_event_name")
    if event in {"SessionStart", "UserPromptSubmit", "PreCompact", "SessionEnd"}:
        with history(root) as db:
            stale = [row[0] for row in db.execute("SELECT session_id FROM turns WHERE touched < ?", (time.time() - 86400,))]
            for old in [session, *stale]:
                db.execute("DELETE FROM seen WHERE session_id=?", (old,))
                db.execute("DELETE FROM reads WHERE session_id=?", (old,))
                db.execute("DELETE FROM turns WHERE session_id=?", (old,))
            if event != "SessionEnd":
                db.execute("INSERT INTO turns VALUES (?, ?, ?)", (session, uuid.uuid4().hex, time.time()))
        return {}
    if event not in {"PreToolUse", "PostToolUse"} or payload.get("tool_name") != "Read":
        return {}
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or set(tool_input) - {"file_path", "offset", "limit", "pages", "allow_large"}:
        return {}
    if "pages" in tool_input or tool_input.get("allow_large"):
        return {}
    for name in ("offset", "limit"):
        if name in tool_input and (type(tool_input[name]) is not int or tool_input[name] < 1):
            return {}
    file = eligible_file(root, tool_input.get("file_path"))
    if file is None:
        return {}
    tool_id = identifier(payload.get("tool_use_id"))
    with history(root) as db:
        turn = db.execute("SELECT prompt_id FROM turns WHERE session_id=?", (session,)).fetchone()
        if turn is None:
            return {}  # Wait for a known prompt boundary rather than guessing context membership.
        prompt_id = turn[0]
        if event == "PreToolUse":
            db.execute("INSERT OR REPLACE INTO reads VALUES (?, ?, ?)", (session, tool_id, prompt_id))
            db.execute("DELETE FROM reads WHERE session_id=? AND rowid NOT IN (SELECT rowid FROM reads WHERE session_id=? ORDER BY rowid DESC LIMIT 256)", (session, session))
            if "limit" in tool_input or file[1] <= 32768:
                return {}
            db.execute("INSERT OR IGNORE INTO reductions (session_id, prompt_id, tool_id, kind, before_bytes, after_bytes) VALUES (?, ?, ?, 'bounded', 0, 0)", (session, prompt_id, tool_id))
            return {"hookSpecificOutput": {"hookEventName": event,
                "updatedInput": {**tool_input, "limit": 400},
                "additionalContext": "ContextPilot limited this large Read to 400 lines. This is a partial view; request further offsets or an explicit limit as needed. Never assume the whole file was read."}}
        started = db.execute("SELECT prompt_id FROM reads WHERE session_id=? AND tool_id=?", (session, tool_id)).fetchone()
        if started is None or started[0] != prompt_id:
            return {}  # A result started before a prompt/compaction boundary is not a current-context anchor.
        db.execute("DELETE FROM reads WHERE session_id=? AND tool_id=?", (session, tool_id))
        response = payload.get("tool_response")
        if not isinstance(response, dict) or response.get("type") != "text" or not isinstance(response.get("file"), dict):
            return {}
        detail = response["file"]
        content = detail.get("content")
        if detail.get("filePath") != str(file[0]) or not isinstance(content, str) or detail.get("truncatedByTokenCap"):
            return {}
        if any(type(detail.get(name)) is not int or detail[name] < 0 for name in ("numLines", "startLine", "totalLines")):
            return {}
        before_bytes = len(content.encode("utf-8"))
        if before_bytes < 2048 or "\0" in content or SECRET_CONTENT.search(content):
            return {}
        input_hash = hashlib.sha256(json_bytes(tool_input)).hexdigest()
        output_hash = hashlib.sha256(json_bytes(response)).hexdigest()
        previous = db.execute("SELECT tool_id FROM seen WHERE session_id=? AND input_hash=? AND output_hash=?", (session, input_hash, output_hash)).fetchone()
        if previous is None:
            db.execute("INSERT INTO seen VALUES (?, ?, ?, ?)", (session, input_hash, output_hash, tool_id))
            db.execute("DELETE FROM seen WHERE session_id=? AND rowid NOT IN (SELECT rowid FROM seen WHERE session_id=? ORDER BY rowid DESC LIMIT 256)", (session, session))
            return {}
        if previous[0] == tool_id:
            return {}
        previous_tool = identifier(previous[0])
        notice = (f"[ContextPilot: identical Read result already returned by tool {previous_tool} in this prompt. "
                  "Refer to that result if still visible. For a fresh copy, request a different explicit offset/limit. Content not repeated.]\n")
        after_bytes = len(notice.encode())
        db.execute("INSERT OR IGNORE INTO reductions (session_id, prompt_id, tool_id, kind, before_bytes, after_bytes) VALUES (?, ?, ?, 'duplicate', ?, ?)", (session, prompt_id, tool_id, before_bytes, after_bytes))
        return {"hookSpecificOutput": {"hookEventName": event,
            "updatedToolOutput": {**response, "file": {**detail, "content": notice, "numLines": 1}}}}


def totals(root: Path) -> dict:
    empty = {"bounded_reads": 0, "duplicate_reads": 0, "payload_bytes_avoided": 0,
             "estimated_tokens_avoided": 0}
    path = database_path(root, DB)
    if not path.exists():
        return empty
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        row = db.execute("""SELECT COALESCE(SUM(kind='bounded'),0), COALESCE(SUM(kind='duplicate'),0),
            COALESCE(SUM(before_bytes-after_bytes),0),
            COALESCE(SUM((before_bytes+3)/4 - (after_bytes+3)/4),0) FROM reductions""").fetchone()
    return dict(zip(empty, row))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("on", "off", "status", "hook"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True)
        if args.mode == "hook":
            raw = sys.stdin.buffer.read(MAX_INPUT + 1)
            if len(raw) > MAX_INPUT:
                raise ValueError("Hook input too large")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Hook payload must be an object")
            print(json.dumps(hook(root, payload), ensure_ascii=True))
        else:
            result = status(root) if args.mode == "status" else configure(root, args.mode == "on")
            print(json_bytes(result).decode(), end="")
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
        if args.mode == "hook":
            print("{}")
            print(f"ContextPilot optimization skipped ({type(exc).__name__}); original tool behavior preserved.", file=sys.stderr)
            return 0
        parser.exit(2, f"contextpilot auto: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
