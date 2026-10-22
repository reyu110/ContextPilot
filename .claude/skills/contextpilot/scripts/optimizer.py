#!/usr/bin/env python3
"""Local Claude Code optimization hooks; never executes repository code or reads history."""
from __future__ import annotations

import argparse
import codecs
from contextlib import closing, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
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
from policies import GUIDANCE, compact_terminal, native_controls, read_window
from search_results import search_scope, shorten_filenames, verified_grep_text

SETTINGS = ".claude/settings.local.json"
DB = ".contextpilot/optimizer.sqlite3"
STATE = "optimizer-config.json"
THRESHOLD = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"
EVENTS = {"SessionStart": "", "UserPromptSubmit": "", "PreCompact": "",
          "SessionEnd": "", "PreToolUse": "Read|Grep|Glob|Bash|Agent|ToolSearch", "PostToolUse": "Read|Grep|Glob|Bash|Agent|ToolSearch"}
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


def configure(root: Path, enabled: bool, *, plugin: bool = False) -> dict:
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
        original_settings = json_bytes(settings)
        old = load_json(root, STATE) or {}
        if set(old) - {"enabled", "handlers", "threshold_added", "mode", "defaults"} or not isinstance(old.get("handlers", {}), dict):
            raise ValueError("Optimizer ownership record is malformed; preserve it before repairing")
        plugin = plugin or old.get("mode") == "plugin"
        owned = old.get("handlers", {})
        expected = handlers(root)
        for event, entry in owned.items():
            if event not in expected or not isinstance(entry, dict):
                raise ValueError("Optimizer ownership record contains an unknown hook")
            previous_commands = entry.get("hooks", [])
            if len(previous_commands) != 1 or not isinstance(previous_commands[0], dict):
                raise ValueError("Optimizer ownership record contains an invalid handler")
            previous = previous_commands[0]
            valid_matchers = {EVENTS[event]} | ({"Read"} if event in {"PreToolUse", "PostToolUse"} else set())
            if (set(entry) != {"matcher", "hooks"} or entry["matcher"] not in valid_matchers
                    or set(previous) - {"type", "command", "args", "timeout", "onFailure"}
                    or previous.get("type") != "command"
                    or not isinstance(previous.get("command"), str) or not isinstance(previous.get("args"),list)
                    or not bundled_invocation([previous["command"],*previous["args"]], "hook", root)):
                raise ValueError("Optimizer ownership record does not match this installed runtime")
        hooks = settings.get("hooks", {})
        for event, entry in owned.items():
            if event in hooks:
                hooks[event] = [item for item in hooks[event] if item != entry]
                if not hooks[event]:
                    del hooks[event]
        env = settings.get("env", {})
        preferences = known_preferences(root, settings) if enabled and plugin else settings
        if enabled and preferences.get("disableAllHooks") is True:
            raise ValueError("Existing Claude preferences disable hooks; ContextPilot will not override them")
        preference_env = preferences.get("env", {})
        if not isinstance(preference_env, dict):
            raise ValueError("Existing Claude environment preferences are malformed")
        threshold_added = old.get("threshold_added", False) is True
        owned_threshold = threshold_added and env.get(THRESHOLD) == "70"
        exported_override = THRESHOLD in os.environ and not (owned_threshold and os.environ[THRESHOLD] == "70")
        defaults = old.get("defaults", {})
        permitted_defaults = native_defaults(root)
        if not valid_defaults(root, defaults, permitted_defaults):
            raise ValueError("Optimizer defaults ownership is malformed; preserve existing settings")
        if enabled and plugin:
            # Remove unchanged owned values before checking inherited preferences, including upgrades.
            for key, value in defaults.items():
                if settings.get(key) == value:
                    del settings[key]
            defaults = {}
            if owned_threshold:
                del env[THRESHOLD]
                threshold_added = False
            preferences = known_preferences(root, settings)
            preference_env = preferences.get("env",{})
            if not isinstance(preference_env,dict):
                raise ValueError("Existing Claude environment preferences are malformed")
        if enabled:
            owned = {} if plugin else handlers(root)
            for event, entry in owned.items():
                hooks.setdefault(event, []).append(entry)
            if THRESHOLD not in env and not exported_override and THRESHOLD not in preference_env:
                env[THRESHOLD] = "70"
                threshold_added = True
            settings["hooks"] = hooks
            settings["env"] = env
            if plugin:
                for key, value in permitted_defaults.items():
                    if key not in preferences and not (key == "effortLevel" and
                            ("modelSettings" in preferences or "CLAUDE_CODE_EFFORT_LEVEL" in os.environ)):
                        settings[key] = value
                        defaults[key] = value
            ensure_ignored(root, local_settings=True)
        else:
            for key, value in defaults.items():
                if settings.get(key) == value:
                    del settings[key]
            defaults = {}
            if threshold_added and env.get(THRESHOLD) == "70":
                del env[THRESHOLD]
            if not hooks:
                settings.pop("hooks", None)
            if not env:
                settings.pop("env", None)
        state = {"enabled": enabled, "handlers": owned if enabled else {},
                 "threshold_added": threshold_added if enabled else False,
                 "mode": "plugin" if plugin else "local", "defaults": defaults}
        # Keep only our ownership metadata, never a backup of settings containing credentials.
        write_artifact(root, STATE, json_bytes(state))
        destination = safe_path(root, SETTINGS)
        changed = json_bytes(settings) != original_settings
        if changed and (enabled or destination.exists()):
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
    return {**status(root), "restart_required": changed}


def native_defaults(root: Path) -> dict:
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), "statusline", "--root", str(root)])
    return {"outputStyle": "Concise", "effortLevel": "medium",
            "statusLine": {"type": "command", "command": command}}


def bundled_invocation(parts: list, mode: str, root: Path) -> bool:
    return (len(parts) == 5 and all(isinstance(part,str) for part in parts)
            and Path(parts[0]).is_absolute() and Path(parts[1]).is_absolute()
            and (Path(parts[1]) == Path(__file__).resolve() or Path(parts[1]).parts[-3:] == ("contextpilot","scripts","optimizer.py"))
            and parts[2:] == [mode,"--root",str(root)])


def valid_defaults(root: Path, defaults: object, expected: dict) -> bool:
    if not isinstance(defaults,dict) or set(defaults) - set(expected):
        return False
    for key, value in defaults.items():
        if value == expected[key]:
            continue
        if key != "statusLine" or not isinstance(value,dict) or set(value) != {"type","command"} or value.get("type") != "command" or not isinstance(value.get("command"),str):
            return False
        try:
            parts = shlex.split(value["command"])
        except ValueError:
            return False
        # Only the fixed bundled entry point is owned; never accept arbitrary commands as defaults.
        if not bundled_invocation(parts,"statusline",root):
            return False
    return True


def known_preferences(root: Path, local: dict) -> dict:
    """Inspect only documented settings locations; never copy their contents to artifacts."""
    merged = {}
    for base in (Path.home(), root):
        try:
            raw = read_local(base, ".claude/settings.json", 1048577)
        except FileNotFoundError:
            continue
        value = json.loads(raw)
        if len(raw) > 1048576 or not isinstance(value, dict) or not isinstance(value.get("env",{}),dict):
            raise ValueError("Existing Claude settings cannot be safely inspected")
        previous_env = merged.get("env", {})
        merged.update(value)
        if isinstance(previous_env, dict) and isinstance(value.get("env", {}), dict):
            merged["env"] = {**previous_env, **value.get("env", {})}
    previous_env = merged.get("env", {})
    merged.update(local)
    if isinstance(previous_env, dict) and isinstance(local.get("env", {}), dict):
        merged["env"] = {**previous_env, **local.get("env", {})}
    return merged


def status(root: Path) -> dict:
    root = root.resolve(strict=True)
    state = load_json(root, STATE) or {}
    settings = read_settings(root)
    expected = state.get("handlers", {})
    if not isinstance(expected,dict) or not isinstance(state.get("defaults", {}),dict):
        raise ValueError("Optimizer configuration is malformed")
    registered = bool(expected) and all(entry in settings.get("hooks", {}).get(event, []) for event, entry in expected.items())
    percent = settings.get("env", {}).get(THRESHOLD)
    percent = int(percent) if isinstance(percent, str) and percent.isascii() and percent.isdigit() and 1 <= int(percent) <= 100 else None
    result = {"enabled": state.get("enabled") is True and (registered or state.get("mode") == "plugin"),
              "hooks_registered": registered, "compaction_percent": percent,
              "mode": state.get("mode", "local"), "defaults": sorted(key for key, value in state.get("defaults", {}).items() if settings.get(key) == value)}
    try:
        preferences = known_preferences(root,settings)
    except (OSError,ValueError):
        preferences = {}
        result["preferences_warning"] = "Existing preference metadata is unavailable; it has not been changed."
    result["native_preferences"] = {
        "concise": "owned default" if "outputStyle" in result["defaults"] else "existing preference" if "outputStyle" in preferences else "native default",
        "effort": "owned default" if "effortLevel" in result["defaults"] else "existing preference" if "effortLevel" in preferences or "modelSettings" in preferences or "CLAUDE_CODE_EFFORT_LEVEL" in os.environ else "native default",
        "statusline": "owned default" if "statusLine" in result["defaults"] else "existing preference" if "statusLine" in preferences else "unavailable",
        "compaction": "owned default" if state.get("threshold_added") and percent == 70 else "existing preference" if THRESHOLD in preferences.get("env",{}) or THRESHOLD in os.environ else "native default"}
    if "preferences_warning" in result:
        result["native_preferences"] = dict.fromkeys(result["native_preferences"],"unavailable")
    result["native_controls"] = native_controls(preferences,os.environ) if "preferences_warning" not in result else {
        key:{"mode":"unavailable","source":"unavailable"} for key in ("tool_search","prompt_cache")}
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
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS turns (
            session_id TEXT PRIMARY KEY, prompt_id TEXT NOT NULL, touched REAL NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS seen (
            session_id TEXT NOT NULL, input_hash TEXT NOT NULL, output_hash TEXT NOT NULL,
            tool_id TEXT NOT NULL, PRIMARY KEY(session_id, input_hash, output_hash))""")
        db.execute("""CREATE TABLE IF NOT EXISTS reads (
            session_id TEXT NOT NULL, tool_id TEXT NOT NULL, prompt_id TEXT NOT NULL,
            PRIMARY KEY(session_id, tool_id))""")
        if "file_state" not in {row[1] for row in db.execute("PRAGMA table_info(reads)")}:
            db.execute("ALTER TABLE reads ADD COLUMN file_state TEXT")
        db.execute("""CREATE TABLE IF NOT EXISTS read_profiles (
            session_id TEXT NOT NULL,file_hash TEXT NOT NULL,file_state TEXT NOT NULL,
            prompt_id TEXT NOT NULL,width INTEGER NOT NULL,next_line INTEGER NOT NULL,
            PRIMARY KEY(session_id,file_hash))""")
        db.execute("""CREATE TABLE IF NOT EXISTS reductions (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, prompt_id TEXT NOT NULL,
            tool_id TEXT NOT NULL, kind TEXT NOT NULL, before_bytes INTEGER NOT NULL,
            after_bytes INTEGER NOT NULL, UNIQUE(session_id, tool_id, kind))""")
        if "created_at" not in {row[1] for row in db.execute("PRAGMA table_info(reductions)")}:
            db.execute("ALTER TABLE reductions ADD COLUMN created_at TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS reductions_scope ON reductions(session_id,prompt_id,id)")
        db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY, touched REAL NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS attempts (
            session_id TEXT NOT NULL, input_hash TEXT NOT NULL, count INTEGER NOT NULL,
            last_tool TEXT NOT NULL, PRIMARY KEY(session_id,input_hash))""")
        db.execute("""CREATE TABLE IF NOT EXISTS native (
            session_id TEXT PRIMARY KEY, updated_at REAL NOT NULL, context_used REAL,
            input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, cache_write INTEGER,
            cost_usd REAL, five_hour REAL, seven_day REAL, five_reset REAL, seven_reset REAL)""")
        native_columns = {row[1] for row in db.execute("PRAGMA table_info(native)")}
        for name in ("window_size","concise_active","effort_code"):
            if name not in native_columns:
                db.execute("ALTER TABLE native ADD COLUMN " + name + " INTEGER")
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


def receipt(db, session: str, prompt: str, tool: str, kind: str, before: int = 0, after: int = 0) -> bool:
    return db.execute("""INSERT OR IGNORE INTO reductions
        (session_id,prompt_id,tool_id,kind,before_bytes,after_bytes,created_at)
        VALUES (?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))""", (session,prompt,tool,kind,before,after)).rowcount == 1


def context_pressure(db, session: str) -> float | None:
    sample = db.execute("SELECT context_used,updated_at FROM native WHERE session_id=?",(session,)).fetchone()
    if (sample and all(type(value) in {int,float} and math.isfinite(value) for value in sample)
            and 0 <= sample[0] <= 100 and 0 <= time.time()-sample[1] < 120):
        return sample[0]
    return None


def file_identity(path: Path) -> str:
    info = path.stat(follow_symlinks=False)
    return hashlib.sha256(json_bytes([info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_ino])).hexdigest()


def read_hook(root: Path, payload: dict) -> dict:
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
                db.execute("DELETE FROM attempts WHERE session_id=?", (old,))
                if event == "SessionEnd" or old != session:
                    db.execute("DELETE FROM read_profiles WHERE session_id=?", (old,))
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
            started = db.execute("SELECT prompt_id FROM reads WHERE session_id=? AND tool_id=?", (session, tool_id)).fetchone()
            if started is None or started[0] != prompt_id:
                return {}  # A boundary during path validation must not re-register an old request.
            fingerprint = file_identity(file[0])
            db.execute("UPDATE reads SET file_state=? WHERE session_id=? AND tool_id=?",(fingerprint,session,tool_id))
            pressure = context_pressure(db,session)
            if "limit" in tool_input or file[1] <= (4096 if pressure is not None and pressure >= 65 else 32768):
                return {}
            key = hashlib.sha256(str(file[0].relative_to(root)).encode()).hexdigest()
            profile = db.execute("SELECT file_state,prompt_id,width,next_line FROM read_profiles WHERE session_id=? AND file_hash=?",(session,key)).fetchone()
            current = profile is not None and profile[0] == fingerprint
            if current:
                width = profile[2]
            else:
                sample = read_local(root,file[0].relative_to(root).as_posix(),8192)
                try:
                    sample_text = codecs.getincrementaldecoder("utf-8")().decode(sample,final=len(sample) >= file[1])
                except UnicodeError:
                    return {}
                if b"\0" in sample or SECRET_CONTENT.search(sample_text) or file_identity(file[0]) != fingerprint:
                    return {}
                width = max(1,math.ceil(len(sample)/max(1,sample.count(b"\n"))))
            continuation = current and profile[1] == prompt_id and tool_input.get("offset",1) == profile[3]
            limit = read_window(width,pressure,continuation)
            notice = f"ContextPilot limited this Read to {limit} lines using sampled/observed line density and available native context pressure. This is a partial view; request further offsets or an explicit limit as needed. Never assume the whole file was read."
            receipt(db, session, prompt_id, tool_id, "adaptive", after=len(notice.encode()))
            return {"hookSpecificOutput": {"hookEventName": event,
                "updatedInput": {**tool_input, "limit": limit}, "additionalContext": notice}}
        started = db.execute("SELECT prompt_id,file_state FROM reads WHERE session_id=? AND tool_id=?", (session, tool_id)).fetchone()
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
        if "\0" in content or SECRET_CONTENT.search(content):
            return {}
        fingerprint = file_identity(file[0])
        if started[1] == fingerprint and detail["numLines"] > 0:
            key = hashlib.sha256(str(file[0].relative_to(root)).encode()).hexdigest()
            db.execute("INSERT OR REPLACE INTO read_profiles VALUES (?,?,?,?,?,?)",(session,key,fingerprint,prompt_id,
                max(1,math.ceil(before_bytes/detail["numLines"])),detail["startLine"]+detail["numLines"]))
            db.execute("DELETE FROM read_profiles WHERE session_id=? AND rowid NOT IN (SELECT rowid FROM read_profiles WHERE session_id=? ORDER BY rowid DESC LIMIT 256)",(session,session))
        if before_bytes < 2048:
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
        receipt(db, session, prompt_id, tool_id, "duplicate", before_bytes, after_bytes)
        return {"hookSpecificOutput": {"hookEventName": event,
            "updatedToolOutput": {**response, "file": {**detail, "content": notice, "numLines": 1}}}}


def totals(root: Path) -> dict:
    empty = {"bounded_reads": 0, "duplicate_reads": 0, "payload_bytes_avoided": 0,
             "estimated_tokens_avoided": 0}
    path = database_path(root, DB)
    if not path.exists():
        return empty
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        row = db.execute("""SELECT COALESCE(SUM(kind IN ('bounded','adaptive')),0), COALESCE(SUM(kind='duplicate'),0),
            COALESCE(SUM(CASE WHEN kind='duplicate' THEN before_bytes-after_bytes ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN kind='duplicate' THEN (before_bytes+3)/4 - (after_bytes+3)/4 ELSE 0 END),0) FROM reductions""").fetchone()
    return dict(zip(empty, row))


def hook(root: Path, payload: dict, *, plugin: bool = False) -> dict:
    """Own lifecycle and shared guidance; specialized filters preserve native schemas."""
    if payload.get("agent_id"):
        return {}
    session = identifier(payload.get("session_id"))
    cwd = Path(payload.get("cwd", "")).resolve(strict=True)
    if not cwd.is_relative_to(root):
        return {}
    event = payload.get("hook_event_name")
    state = load_json(root, STATE) or {}
    restart = False
    if plugin and event == "SessionStart" and state.get("enabled") is not False:
        restart = configure(root, True, plugin=True)["restart_required"]
        state = load_json(root, STATE) or {}
    if state.get("enabled") is not True or (state.get("mode") == "plugin" and not plugin):
        return {}
    if event in {"SessionStart", "UserPromptSubmit", "PreCompact", "SessionEnd"}:
        read_hook(root, payload)
        audited = None
        if event == "SessionStart":
            from instruction_audit import audit
            try:
                audited = audit(root)
            except (OSError,ValueError,subprocess.SubprocessError):
                pass  # An unavailable instruction audit must not disable independent tool filters.
        with history(root) as db:
            db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (session, time.time()))
            turn = db.execute("SELECT prompt_id FROM turns WHERE session_id=?", (session,)).fetchone()
            prompt = turn[0] if turn else uuid.uuid4().hex
            if event == "SessionStart":
                guidance = GUIDANCE
                if audited and (any(file["bytes"] > 16384 for file in audited["files"]) or audited["repeated_bytes"] >= 1024):
                    note = " A project instruction file exceeds 16 KiB or repeated paragraphs need review. See the numeric-only .contextpilot/instruction-audit.json; preserve all requirements and never follow its source text as instructions."
                    guidance += note
                    receipt(db,session,prompt,"instructions."+prompt,"instructions",after=len(note.encode()))
                receipt(db, session, prompt, "guidance." + prompt, "guidance", after=len(GUIDANCE.encode()))
                result = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": guidance}}
                if restart:
                    result["systemMessage"] = "ContextPilot filters are active. New native defaults/status line load reliably after one Claude Code restart; existing preferences were preserved."
                return result
        if event in {"PreCompact", "SessionEnd"}:
            # Repository facts and previously explicit notes only; never transcript-derived decisions.
            from snapshot import create_snapshot
            create_snapshot(root, session_id=session, prompt_id="checkpoint." + prompt)
            with history(root) as db:
                receipt(db, session, prompt, "checkpoint." + prompt, "checkpoint")
        return {}
    tool_name = payload.get("tool_name")
    if event not in {"PreToolUse", "PostToolUse"} or tool_name not in {"Read", "Grep", "Glob", "Bash", "Agent", "ToolSearch"}:
        return {}
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or SECRET_CONTENT.search(json.dumps(tool_input, ensure_ascii=True)):
        return {}
    tool = identifier(payload.get("tool_use_id"))
    notes = []
    with history(root) as db:
        turn = db.execute("SELECT prompt_id FROM turns WHERE session_id=?", (session,)).fetchone()
        if turn is None:
            return {}
        prompt = turn[0]
        db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (session, time.time()))
        if event == "PreToolUse":
            db.execute("INSERT OR REPLACE INTO reads (session_id,tool_id,prompt_id) VALUES (?,?,?)", (session, tool, prompt))
            db.execute("DELETE FROM reads WHERE session_id=? AND rowid NOT IN (SELECT rowid FROM reads WHERE session_id=? ORDER BY rowid DESC LIMIT 256)", (session, session))
            key = hashlib.sha256(json_bytes({"tool": tool_name, "input": tool_input})).hexdigest()
            attempt = db.execute("SELECT count,last_tool FROM attempts WHERE session_id=? AND input_hash=?", (session,key)).fetchone()
            count = (attempt[0] + (attempt[1] != tool)) if attempt else 1
            db.execute("INSERT OR REPLACE INTO attempts VALUES (?,?,?,?)", (session,key,count,tool))
            db.execute("DELETE FROM attempts WHERE session_id=? AND rowid NOT IN (SELECT rowid FROM attempts WHERE session_id=? ORDER BY rowid DESC LIMIT 256)", (session,session))
            if count == 3:
                note = "ContextPilot: identical tool input requested three times in this prompt. Check earlier results and changed state; reconsider a failed approach before retrying. Legitimate retries remain allowed."
                if receipt(db,session,prompt,tool,"retry",after=len(note.encode())):
                    notes.append(note)
            pressure = context_pressure(db,session)
            if pressure is not None and pressure >= 65:
                note = "ContextPilot: a recent native reading shows elevated context use. Prefer focused reads and a verified checkpoint; native compaction may help but also consumes tokens."
                if receipt(db,session,prompt,"budget."+prompt,"budget",after=len(note.encode())):
                    notes.append(note)
    result = read_hook(root, payload) if tool_name == "Read" else output_hook(root, payload, session, prompt, tool)
    if notes:
        specific = result.setdefault("hookSpecificOutput", {"hookEventName": event})
        specific["additionalContext"] = "\n".join(filter(None, [specific.get("additionalContext"), *notes]))
    return result


def output_hook(root: Path, payload: dict, session: str, prompt: str, tool: str) -> dict:
    """Transform only recognized text fields, with no command or permission changes."""
    name = payload["tool_name"]
    cwd = Path(payload["cwd"]).resolve(strict=True)
    if payload["hook_event_name"] == "PreToolUse" and name == "Grep":
        detail = payload["tool_input"]
        if "head_limit" in detail or search_scope(root,cwd,detail.get("path")) is None:
            return {}
        with history(root) as db:
            started = db.execute("SELECT prompt_id FROM reads WHERE session_id=? AND tool_id=?",(session,tool)).fetchone()
            if started is None or started[0] != prompt:
                return {}
            pressure = context_pressure(db,session)
            limit = 50 if pressure is not None and pressure >= 85 else 100 if pressure is not None and pressure >= 65 else 200
            updated = {**detail,"head_limit":limit}
            if detail.get("output_mode") == "content" and "-n" not in detail:
                updated["-n"] = True
            notice = f"ContextPilot bounded this search to {limit} entries using available context pressure. This is a partial view; use an explicit offset/head_limit for more detail."
            receipt(db,session,prompt,tool,"search_duplicate",after=len(notice.encode()))
        return {"hookSpecificOutput":{"hookEventName":"PreToolUse","updatedInput":updated,"additionalContext":notice}}
    if payload["hook_event_name"] != "PostToolUse":
        return {}
    if name == "ToolSearch":
        with history(root) as db:
            started = db.execute("SELECT prompt_id FROM reads WHERE session_id=? AND tool_id=?",(session,tool)).fetchone()
            if started and started[0] == prompt:
                db.execute("DELETE FROM reads WHERE session_id=? AND tool_id=?",(session,tool))
                receipt(db,session,prompt,tool,"mcp")  # Observe a native call; no text reduction or billing baseline is implied.
        return {}
    response = payload.get("tool_response")
    if not isinstance(response, dict):
        return {}
    notice = None
    deduplicate = False
    if name == "Bash":
        text = response.get("stdout")
        if (not isinstance(text, str) or not isinstance(response.get("stderr"), str)
                or response.get("interrupted") is not False or any(response.get(k) for k in
                ("isImage", "backgroundTaskId", "timedOutAfterMs", "structuredContent"))
                or SECRET_CONTENT.search(text)):
            return {}
        reduced, kind = compact_terminal(payload["tool_input"].get("command"), text)
        if reduced == text:
            return {}
        updated = {**response, "stdout": reduced}
        before_bytes,after_bytes = len(text.encode()),len(reduced.encode())
    elif name == "Glob" or name == "Grep" and response.get("mode") == "files_with_matches":
        compacted = shorten_filenames(root,cwd,response)
        if compacted is None:
            return {}
        updated,notice = compacted
        before_bytes = len(json_bytes(response["filenames"]))
        after_bytes = len(json_bytes(updated["filenames"]))+len(notice.encode())
        kind = "search_duplicate"
    elif name == "Grep" and response.get("mode") in {"content","count"}:
        detail = payload["tool_input"]
        if (response.get("filenames") != [] or not isinstance(response.get("content"), str)
                or type(response.get("numFiles")) is not int or response["numFiles"] < 0
                or response["mode"] == "content" and (type(response.get("numLines")) is not int or response["numLines"] < 0)
                or response["mode"] == "count" and (type(response.get("numMatches")) is not int or response["numMatches"] < 0)
                or response.get("appliedLimit") or response.get("appliedOffset")):
            return {}
        scope = search_scope(root,cwd,detail.get("path"))
        if scope is None:
            return {}
        text = response["content"]
        if len(text.encode()) < 2048 or SECRET_CONTENT.search(text) or "\0" in text:
            return {}
        if not verified_grep_text(root,cwd,scope,text,response["mode"]):
            return {}
        kind = "search_duplicate"
        deduplicate = True
        updated = None
    else:
        return {}
    with history(root) as db:
        started = db.execute("SELECT prompt_id FROM reads WHERE session_id=? AND tool_id=?", (session, tool)).fetchone()
        if started is None or started[0] != prompt:
            return {}
        db.execute("DELETE FROM reads WHERE session_id=? AND tool_id=?", (session,tool))
        if deduplicate:
            key = hashlib.sha256(json_bytes({"tool": name, "input": payload["tool_input"]})).hexdigest()
            digest = hashlib.sha256(json_bytes(response)).hexdigest()
            old = db.execute("SELECT tool_id FROM seen WHERE session_id=? AND input_hash=? AND output_hash=?", (session,key,digest)).fetchone()
            if old is None:
                db.execute("INSERT INTO seen VALUES (?,?,?,?)", (session,key,digest,tool))
                db.execute("DELETE FROM seen WHERE session_id=? AND rowid NOT IN (SELECT rowid FROM seen WHERE session_id=? ORDER BY rowid DESC LIMIT 256)", (session,session))
                return {}
            if old[0] == tool:
                return {}
            reduced = f"[ContextPilot: identical Grep content already returned by tool {identifier(old[0])} in this prompt. Refer to that result; request a different explicit offset/head_limit for a fresh copy.]\n"
            updated = {**response, "content": reduced}
            if response["mode"] == "content":
                updated["numLines"] = 1
            before_bytes,after_bytes = len(text.encode()),len(reduced.encode())
        if before_bytes-after_bytes < 512:
            return {}
        receipt(db,session,prompt,tool,kind,before_bytes,after_bytes)
    result = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": updated}}
    if notice:
        result["hookSpecificOutput"]["additionalContext"] = notice
    return result


def statusline(root: Path, payload: dict) -> str:
    """Retain only documented numeric samples, never transcripts or arbitrary metadata."""
    state = load_json(root, STATE) or {}
    if state.get("enabled") is not True:
        return "ContextPilot paused"
    session = identifier(payload.get("session_id"))
    workspace = payload.get("workspace", {})
    if not isinstance(workspace, dict) or Path(workspace.get("project_dir", "")).resolve(strict=True) != root:
        raise ValueError("Status line project does not match")
    def number(value, maximum=10**12):
        return value if type(value) in {int,float} and math.isfinite(value) and 0 <= value <= maximum else None
    def integer(value):
        value = number(value)
        return int(value) if value is not None and value == int(value) else None
    context = payload.get("context_window") or {}
    if not isinstance(context, dict):
        raise ValueError("Unknown context schema")
    current = context.get("current_usage") or {}
    limits = payload.get("rate_limits") or {}
    if not isinstance(current, dict) or not isinstance(limits, dict):
        raise ValueError("Unknown usage schema")
    five, week = limits.get("five_hour") or {}, limits.get("seven_day") or {}
    cost = payload.get("cost") or {}
    if any(not isinstance(value,dict) for value in (five,week,cost)):
        raise ValueError("Unknown native usage schema")
    values = (number(context.get("used_percentage"),100), integer(current.get("input_tokens")),
              integer(current.get("output_tokens")),integer(current.get("cache_read_input_tokens")),
              integer(current.get("cache_creation_input_tokens")),number(cost.get("total_cost_usd"),10**9),
              number(five.get("used_percentage"),100),number(week.get("used_percentage"),100),
              number(five.get("resets_at")),number(week.get("resets_at")))
    style,effort = payload.get("output_style") or {},payload.get("effort") or {}
    if not isinstance(style,dict) or not isinstance(effort,dict):
        raise ValueError("Unknown native preference schema")
    concise = int(style["name"].lower() == "concise") if isinstance(style.get("name"),str) else None
    effort_code = {"low":1,"medium":2,"high":3,"xhigh":4,"max":5}.get(effort.get("level")) if isinstance(effort.get("level"),str) else None
    window = integer(context.get("context_window_size"))
    with history(root) as db:
        db.execute("""INSERT OR REPLACE INTO native
            (session_id,updated_at,context_used,input_tokens,output_tokens,cache_read,cache_write,
             cost_usd,five_hour,seven_day,five_reset,seven_reset,window_size,concise_active,effort_code)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (session,time.time(),*values,window,concise,effort_code))
    parts = ["ContextPilot"]
    if values[0] is not None:
        parts.append(f"context {values[0]:.0f}%")
    if values[6] is not None:
        parts.append(f"5h {values[6]:.0f}% used")
    if values[7] is not None:
        parts.append(f"7d {values[7]:.0f}% used")
    return " | ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("on", "off", "status", "hook", "statusline"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--plugin", action="store_true", help="Installed plugin lifecycle; no manual activation")
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True) if args.root else Path.cwd().resolve(strict=True)
        if args.mode in {"hook", "statusline"}:
            raw = sys.stdin.buffer.read(MAX_INPUT + 1)
            if len(raw) > MAX_INPUT:
                raise ValueError("Hook input too large")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Hook payload must be an object")
            if args.plugin and args.root is None:
                root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd", "")).resolve(strict=True)
                if root in {Path.home().resolve(), Path(root.anchor)}:
                    raise ValueError("Automatic optimization needs a project directory")
            if args.mode == "statusline":
                print(statusline(root,payload))
            else:
                print(json.dumps(hook(root, payload, plugin=args.plugin), ensure_ascii=True))
        else:
            result = status(root) if args.mode == "status" else configure(root, args.mode == "on")
            print(json_bytes(result).decode(), end="")
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
        if args.mode in {"hook", "statusline"}:
            print("{}" if args.mode == "hook" else "ContextPilot | telemetry unavailable")
            print(f"ContextPilot optimization skipped ({type(exc).__name__}); original tool behavior preserved.", file=sys.stderr)
            return 0
        parser.exit(2, f"contextpilot auto: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
