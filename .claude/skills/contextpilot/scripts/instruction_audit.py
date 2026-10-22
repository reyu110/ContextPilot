"""Bounded, non-destructive instruction audit. Artifacts contain no instruction text."""
from __future__ import annotations

import codecs
import hashlib
import math
import os
from pathlib import Path
import re
import tempfile
import time

from scan import (SECRET_CONTENT, allowed_path, ignored_paths, json_bytes,
                  load_json, prepare_ignore_engine, read_local, safe_path, write_artifact)


def audit(root: Path, *, agent: str = "claude-code") -> dict:
    if agent not in {"claude-code","codex"}:
        raise ValueError("Unknown instruction audit agent")
    result = {"version":1,"created_at":time.time(),"files":[],"bytes":0,"repeated_bytes":0,
              "truncated":False,"skipped":0}
    names = ["AGENTS.md","AGENTS.override.md"] if agent == "codex" else ["CLAUDE.md","AGENTS.md",".claude/CLAUDE.md"]
    with tempfile.TemporaryDirectory(prefix="contextpilot-ignore-") as temporary:
        engine = prepare_ignore_engine(root,temporary)
        try:
            rules = safe_path(root,".claude/rules") if agent == "claude-code" else None
        except ValueError:
            rules = None
            result["skipped"] += 1
        if rules is not None and rules.is_dir() and ".claude/rules/" not in ignored_paths(root,[".claude/rules/"],engine):
            candidates = 0
            for directory, dirs, files in os.walk(rules,followlinks=False):
                candidates += 1
                if candidates > 256:
                    result["truncated"] = True
                    break
                base = Path(directory)
                dirs[:] = sorted(d for d in dirs if allowed_path((base/d).relative_to(root)) and not (base/d).is_symlink())
                excluded = ignored_paths(root,[(base/d).relative_to(root).as_posix()+"/" for d in dirs],engine)
                dirs[:] = [d for d in dirs if (base/d).relative_to(root).as_posix()+"/" not in excluded]
                for filename in sorted(files):
                    candidates += 1
                    if candidates > 256:
                        result["truncated"] = True
                        break
                    name = (base/filename).relative_to(root)
                    if filename.endswith(".md") and allowed_path(name) and not (base/filename).is_symlink():
                        names.append(name.as_posix())
                if result["truncated"]:
                    break
        names = sorted(set(n for n in names if allowed_path(Path(n))))
        excluded = ignored_paths(root,names,engine)
        signatures: set[str] = set()
        remaining = 262144
        examined = 0
        for name in names:
            if name in excluded:
                continue
            if examined >= 32 or remaining <= 0:
                result["truncated"] = True
                break
            try:
                path = safe_path(root,name)
                if not path.is_file():
                    continue
                info = path.stat(follow_symlinks=False)
                examined += 1
                limit = min(65536,remaining)
                raw = read_local(root,name,limit+1)
                truncated = len(raw) > limit
                raw = raw[:limit]
                remaining -= len(raw)
                text = codecs.getincrementaldecoder("utf-8")().decode(raw,final=not truncated)
                if "\0" in text or SECRET_CONTENT.search(text):
                    result["skipped"] += 1
                    continue
                repeated = 0
                for paragraph in re.split(r"\n[ \t]*\n",text):
                    paragraph = paragraph.strip()
                    if len(paragraph.encode()) < 128:
                        continue
                    digest = hashlib.sha256(paragraph.encode()).hexdigest()
                    if digest in signatures:
                        repeated += len(paragraph.encode())
                    else:
                        signatures.add(digest)
                result["files"].append({"path":name,"bytes":len(raw),"lines":len(text.splitlines()),
                    "repeated_bytes":repeated,"truncated":truncated,"mtime_ns":info.st_mtime_ns,"size":info.st_size})
                result["bytes"] += len(raw)
                result["repeated_bytes"] += repeated
                result["truncated"] |= truncated
            except (ValueError,OSError,UnicodeError):
                result["skipped"] += 1
    write_artifact(root,"codex-instruction-audit.json" if agent == "codex" else "instruction-audit.json",json_bytes(result))
    return result


def saved_audit(root: Path, *, agent: str = "claude-code") -> dict | None:
    if agent not in {"claude-code","codex"}:
        raise ValueError("Unknown instruction audit agent")
    result = load_json(root,"codex-instruction-audit.json" if agent == "codex" else "instruction-audit.json",65536)
    if result is None:
        return None
    if (set(result) != {"version","created_at","files","bytes","repeated_bytes","truncated","skipped"}
            or type(result["version"]) is not int or result["version"] != 1
            or not isinstance(result["files"],list) or len(result["files"]) > 32
            or type(result["truncated"]) is not bool or type(result["created_at"]) not in {int,float}
            or not math.isfinite(result["created_at"]) or result["created_at"] < 0):
        raise ValueError("Invalid saved audit schema")
    for key in ("bytes","repeated_bytes","skipped"):
        if type(result[key]) is not int or not 0 <= result[key] <= 262144:
            raise ValueError("Invalid audit totals")
    for entry in result["files"]:
        if (not isinstance(entry,dict) or set(entry) != {"path","bytes","lines","repeated_bytes","truncated","mtime_ns","size"}
                or not isinstance(entry["path"],str) or not allowed_path(Path(entry["path"]))
                or type(entry["truncated"]) is not bool or any(type(entry[key]) is not int or entry[key] < 0 for key in ("bytes","lines","repeated_bytes","mtime_ns","size"))
                or entry["bytes"] > 65536 or entry["lines"] > entry["bytes"] or entry["repeated_bytes"] > entry["bytes"]):
            raise ValueError("Invalid audit file metadata")
    from search_results import approved_paths
    paths = approved_paths(root,root,[entry["path"] for entry in result["files"]])
    if paths is None:
        raise ValueError("Saved audit paths are no longer eligible")
    result["stale"] = any((path.stat().st_size,path.stat().st_mtime_ns) != (entry["size"],entry["mtime_ns"]) for path,entry in zip(paths,result["files"]))
    if any(sum(entry[key] for entry in result["files"]) != result[key] for key in ("bytes","repeated_bytes")):
        raise ValueError("Invalid audit aggregation")
    return result
