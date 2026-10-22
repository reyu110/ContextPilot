#!/usr/bin/env python3
"""Trusted Codex plugin lifecycle; no tool rewriting or agent configuration writes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from instruction_audit import audit
from optimizer import MAX_INPUT, config_lock, history, receipt
from policies import GUIDANCE
from scan import SECRET_CONTENT, ensure_ignored, git, json_bytes, load_json, write_artifact
from usage import identifier

STATE = "codex-config.json"
DB = ".contextpilot/codex.sqlite3"


def status(root: Path) -> dict:
    state = load_json(root,STATE)
    if state is not None and (set(state) != {"version","enabled"} or type(state["version"]) is not int
                              or state["version"] != 1 or type(state["enabled"]) is not bool):
        raise ValueError("Malformed Codex activation state; preserve it for inspection")
    return {"enabled":state is not None and state["enabled"],"mode":"codex-plugin",
            "hooks_registered":False,"defaults":[],"compaction_percent":None,
            "activation_note":"Requires an installed plugin and explicit Codex hook trust; configuration alone does not prove execution."}


def configure(root: Path, enabled: bool) -> dict:
    status(root)  # Reject corrupt state before any writes.
    ensure_ignored(root)
    with config_lock(root):
        status(root)
        write_artifact(root,STATE,json_bytes({"version":1,"enabled":enabled}))
    return status(root)


def hook(root: Path, payload: dict) -> dict:
    if payload.get("agent_id"):
        return {}
    event = payload.get("hook_event_name")
    if event not in {"SessionStart","UserPromptSubmit","PreCompact","SessionEnd","PreToolUse"}:
        return {}
    cwd = Path(payload.get("cwd","")).resolve(strict=True)
    if not cwd.is_relative_to(root):
        return {}
    session = identifier(payload.get("session_id"))
    state = status(root)
    if event == "SessionStart" and load_json(root,STATE) is None:
        state = configure(root,True)
    if not state["enabled"]:
        return {}
    if event in {"SessionStart","UserPromptSubmit","SessionEnd"}:
        prompt = identifier(payload.get("turn_id")) if event == "UserPromptSubmit" else "setup."+uuid.uuid4().hex
        with history(root,DB) as db:
            db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)",(session,time.time()))
            stale = [row[0] for row in db.execute("SELECT session_id FROM turns WHERE touched<?",(time.time()-86400,))]
            for old in {session,*stale}:
                db.execute("DELETE FROM attempts WHERE session_id=?",(old,))
                db.execute("DELETE FROM turns WHERE session_id=?",(old,))
            if event != "SessionEnd":
                db.execute("INSERT INTO turns VALUES (?,?,?)",(session,prompt,time.time()))
        if event == "SessionStart":
            try:
                audit(root,agent="codex")
            except (OSError,ValueError,subprocess.SubprocessError):
                pass  # Audit failure must not prevent independent lifecycle support.
            with history(root,DB) as db:
                receipt(db,session,prompt,"guidance."+prompt,"guidance",after=len(GUIDANCE.encode()))
            result = {"hookSpecificOutput":{"hookEventName":event,"additionalContext":GUIDANCE}}
            from updates import session_notice
            notice = session_notice(root,agent="codex")
            if notice:
                result["systemMessage"] = notice
            return result
        return {}
    turn = identifier(payload.get("turn_id"))
    if event == "PreCompact":
        from snapshot import create_snapshot
        checkpoint = "checkpoint."+hashlib.sha256(turn.encode()).hexdigest()
        with config_lock(root):
            with history(root,DB) as db:
                current = db.execute("SELECT prompt_id FROM turns WHERE session_id=?",(session,)).fetchone()
                done = db.execute("SELECT 1 FROM reductions WHERE session_id=? AND tool_id=? AND kind='checkpoint'",(session,checkpoint)).fetchone()
            if current is None or current[0] != turn or done:
                return {}
            create_snapshot(root,session_id=session,prompt_id="codex-"+checkpoint)
            with history(root,DB) as db:
                receipt(db,session,turn,checkpoint,"checkpoint")
                db.execute("DELETE FROM attempts WHERE session_id=?",(session,))
        return {}
    name,detail = payload.get("tool_name"),payload.get("tool_input")
    if name not in {"Bash","apply_patch"} or not isinstance(detail,dict) or SECRET_CONTENT.search(json.dumps(detail,ensure_ascii=True)):
        return {}
    tool = identifier(payload.get("tool_use_id"))
    key = hashlib.sha256(json_bytes({"tool":name,"input":detail})).hexdigest()
    with history(root,DB) as db:
        current = db.execute("SELECT prompt_id FROM turns WHERE session_id=?",(session,)).fetchone()
        if current is None or current[0] != turn:
            return {}
        db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)",(session,time.time()))
        previous = db.execute("SELECT count,last_tool FROM attempts WHERE session_id=? AND input_hash=?",(session,key)).fetchone()
        count = previous[0]+(previous[1] != tool) if previous else 1
        db.execute("INSERT OR REPLACE INTO attempts VALUES (?,?,?,?)",(session,key,count,tool))
        db.execute("DELETE FROM attempts WHERE session_id=? AND rowid NOT IN (SELECT rowid FROM attempts WHERE session_id=? ORDER BY rowid DESC LIMIT 256)",(session,session))
        if count == 3:
            note = "ContextPilot: this identical tool request has repeated three times in this turn. Check earlier results and changed state before retrying; legitimate retries remain allowed."
            if receipt(db,session,turn,tool,"retry",after=len(note.encode())):
                return {"hookSpecificOutput":{"hookEventName":event,"additionalContext":note}}
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=("on","off","status","hook"))
    parser.add_argument("--root",type=Path)
    args = parser.parse_args()
    try:
        root = (args.root or Path.cwd()).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Root must be a directory")
        if args.mode == "hook":
            raw = sys.stdin.buffer.read(MAX_INPUT+1)
            if len(raw) > MAX_INPUT:
                raise ValueError("Hook input too large")
            payload = json.loads(raw)
            if not isinstance(payload,dict) or not isinstance(payload.get("cwd"),str) or not Path(payload["cwd"]).is_absolute():
                raise ValueError("Unknown hook schema")
            if args.root is None:
                root = Path(payload["cwd"]).resolve(strict=True)
                try:
                    root = Path(git(root,"rev-parse","--show-toplevel").decode().strip()).resolve(strict=True)
                except subprocess.CalledProcessError:
                    pass
            if root in {Path.home().resolve(),Path(root.anchor)}:
                raise ValueError("Automatic support requires a project directory")
            result = hook(root,payload)
        else:
            result = status(root) if args.mode == "status" else configure(root,args.mode == "on")
        print(json.dumps(result,ensure_ascii=True))
        return 0
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error,subprocess.SubprocessError) as exc:
        if args.mode == "hook":
            print("{}")
            print(f"ContextPilot Codex hook skipped ({type(exc).__name__}); original behavior preserved.",file=sys.stderr)
            return 0
        parser.exit(2,f"contextpilot codex: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
