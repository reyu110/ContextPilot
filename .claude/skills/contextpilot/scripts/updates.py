#!/usr/bin/env python3
"""Bounded public-version checks; never downloads or executes an installer."""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

from scan import ensure_ignored, json_bytes, load_json, read_local, write_artifact

BUNDLE = Path(__file__).resolve().parents[1]
URL = "https://raw.githubusercontent.com/reyu110/ContextPilot/main/.claude/skills/contextpilot/version.json"
CACHE = "update-check.json"
INTERVAL = 86400
AGENTS = {"claude-code","codex"}


def version(value: object) -> tuple[int,int,int]:
    if not isinstance(value,str) or not re.fullmatch(r"(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})",value):
        raise ValueError("Invalid stable version")
    return tuple(map(int,value.split(".")))


def decode_version(raw: bytes) -> str:
    if len(raw) > 512:
        raise ValueError("Version metadata too large")
    data = json.loads(raw)
    if not isinstance(data,dict) or set(data) != {"version"}:
        raise ValueError("Unknown version metadata")
    version(data["version"])
    return data["version"]


def installed_version() -> str:
    return decode_version(read_local(BUNDLE,"version.json",513))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_latest() -> str:
    request = urllib.request.Request(URL,headers={"User-Agent":"ContextPilot-update-check","Accept":"application/json"})
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request,timeout=2) as response:
        if response.status != 200 or response.geturl() != URL:
            raise ValueError("Unexpected update response")
        return decode_version(response.read(513))


def cache(root: Path) -> dict | None:
    value = load_json(root,CACHE,8192)
    if value is None:
        return None
    if (set(value) != {"version","attempted_at","checked_at","latest","error","notified"}
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["error"]) is not bool or not isinstance(value["notified"],dict)
            or set(value["notified"]) - AGENTS):
        raise ValueError("Invalid update cache")
    for key in ("attempted_at","checked_at"):
        number = value[key]
        if key == "checked_at" and number is None:
            continue
        # Keep cached timestamps in a displayable calendar range (through year 9999).
        if type(number) not in {int,float} or not 0 <= number <= 253402300799:
            raise ValueError("Invalid update timestamp")
    if (value["latest"] is None) != (value["checked_at"] is None):
        raise ValueError("Incomplete update cache")
    if not value["error"] and value["latest"] is None:
        raise ValueError("Unverified update cache")
    if value["latest"] is not None:
        version(value["latest"])
    for notified in value["notified"].values():
        version(notified)
    return value


def fresh(timestamp) -> bool:
    return timestamp is not None and 0 <= time.time()-timestamp < INTERVAL


def commands(agent: str) -> dict:
    if agent not in AGENTS:
        raise ValueError("Unknown update agent")
    base = "npx skills@latest add reyu110/ContextPilot --skill contextpilot --skill pilotboard --agent "+agent
    return {
        "skills-global":{"label":"Skills · global","command":base+" --global --yes","note":"Reinstalls both skills from GitHub. Installed-file edits may be overwritten; project history is preserved. Restart the agent afterward."},
        "skills-project":{"label":"Skills · this project","command":base+" --yes","note":"Run in the project where the skills are installed. Installed-file edits may be overwritten; restart afterward."},
        "plugin":{"label":"Plugin","command":"claude plugin update contextpilot@contextpilot" if agent == "claude-code" else "codex plugin marketplace upgrade contextpilot",
                  "note":"Restart Claude Code or use /reload-plugins. Native install scope is auto-detected." if agent == "claude-code" else "Refresh the installed plugin in /plugins, restart, then review changed hooks in /hooks. This command refreshes Git marketplace metadata; local-directory installs need their source copy updated first."}}


def status(root: Path, *, agent: str = "claude-code") -> dict:
    result = {"installed":installed_version(),"latest":None,"checked_at":None,"stale":True,
              "state":"unchecked","automatic_checks":os.environ.get("CONTEXTPILOT_UPDATE_CHECK") != "0",
              "commands":commands(agent)}
    try:
        saved = cache(root)
    except (OSError,ValueError):
        result["warning"] = "Saved update information is invalid or unreadable; it has been preserved."
        result["state"] = "unavailable"
        return result
    if saved is None:
        return result
    result.update(latest=saved["latest"],checked_at=saved["checked_at"],stale=not fresh(saved["checked_at"]))
    if saved["error"]:
        result["warning"] = "The last update check failed. Any displayed version is the last successful observation."
    if saved["latest"] is not None and version(saved["latest"]) > version(result["installed"]):
        result["state"] = "available"
    elif not saved["error"] and not result["stale"]:
        result["state"] = "current" if version(saved["latest"]) == version(result["installed"]) else "ahead"
    else:
        result["state"] = "unavailable" if saved["latest"] is None else "stale"
    return result


def check(root: Path, *, agent: str = "claude-code", force: bool = False) -> dict:
    installed_version()
    commands(agent)
    cache(root)  # Preserve malformed state before making any writes or requests.
    from optimizer import config_lock
    ensure_ignored(root)
    with config_lock(root,name="update-check.lock"):
        saved = cache(root)
        if force or saved is None or not fresh(saved["attempted_at"]):
            saved = saved or {"version":1,"attempted_at":0,"checked_at":None,"latest":None,"error":False,"notified":{}}
            saved["attempted_at"] = time.time()
            saved["error"] = True
            write_artifact(root,CACHE,json_bytes(saved))
            try:
                latest = fetch_latest()
                version(latest)
                saved.update(latest=latest,checked_at=time.time(),error=False)
            except (OSError,ValueError,urllib.error.URLError,http.client.HTTPException):
                saved["error"] = True
            write_artifact(root,CACHE,json_bytes(saved))
    return status(root,agent=agent)


def session_notice(root: Path, *, agent: str) -> str:
    if os.environ.get("CONTEXTPILOT_UPDATE_CHECK") == "0":
        return ""
    try:
        result = check(root,agent=agent)
        if result["state"] != "available":
            return ""
        from optimizer import config_lock
        with config_lock(root,name="update-check.lock"):
            saved = cache(root)
            if saved is None or saved["notified"].get(agent) == result["latest"]:
                return ""
            saved["notified"][agent] = result["latest"]
            write_artifact(root,CACHE,json_bytes(saved))
        command = "$contextpilot update" if agent == "codex" else "/contextpilot:contextpilot update or /contextpilot update"
        return f"ContextPilot update available: {result['latest']} (installed {result['installed']}). Run {command}. Pilotboard shows update instructions; restart the agent after updating."
    except (OSError,ValueError):
        return ""  # Update availability must never disrupt context optimization.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=("check","status"))
    parser.add_argument("--root",type=Path,default=Path.cwd())
    parser.add_argument("--agent",choices=sorted(AGENTS),default="claude-code")
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Root must be a directory")
        result = check(root,agent=args.agent,force=True) if args.mode == "check" else status(root,agent=args.agent)
        print(json_bytes(result).decode(),end="")
        return 0
    except (OSError,ValueError) as exc:
        parser.exit(2,f"contextpilot updates: {type(exc).__name__}; existing data preserved\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
