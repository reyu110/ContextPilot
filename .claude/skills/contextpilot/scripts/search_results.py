"""Privacy-verified search/list transforms. Never search contents or execute a tool."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile

from scan import allowed_path, ignored_paths, prepare_ignore_engine, safe_path


def approved_paths(root: Path, cwd: Path, names: list[str]) -> list[Path] | None:
    if len(names) > 2048:
        return None
    paths = []
    relatives = []
    for name in names:
        if not isinstance(name,str) or not name or "\0" in name or "\n" in name or "\r" in name:
            return None
        path = Path(name)
        path = path if path.is_absolute() else cwd / path
        try:
            relative = path.relative_to(root)
        except ValueError:
            return None
        if not allowed_path(relative):
            return None
        path = safe_path(root,relative.as_posix())
        try:
            if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                return None
        except FileNotFoundError:
            return None
        paths.append(path)
        relatives.append(relative.as_posix())
    if relatives:
        with tempfile.TemporaryDirectory(prefix="contextpilot-ignore-") as temporary:
            engine = prepare_ignore_engine(root,temporary)
            if ignored_paths(root,relatives,engine):
                return None
    return paths


def search_scope(root: Path, cwd: Path, raw: object) -> Path | None:
    if raw is None:
        raw = str(cwd)
    if not isinstance(raw,str):
        return None
    path = Path(raw)
    path = path if path.is_absolute() else cwd / path
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    if not allowed_path(relative):
        return None
    path = safe_path(root,relative.as_posix())
    if not path.exists():
        return None
    with tempfile.TemporaryDirectory(prefix="contextpilot-ignore-") as temporary:
        engine = prepare_ignore_engine(root,temporary)
        name = relative.as_posix() + ("/" if path.is_dir() else "")
        if ignored_paths(root,[name],engine):
            return None
    return path


def verified_grep_text(root: Path, cwd: Path, scope: Path, text: str, mode: str) -> bool:
    if scope.is_file():
        return approved_paths(root,cwd,[str(scope)]) is not None
    lines = text.splitlines()
    if len(lines) > 2048:
        return False
    names = []
    for line in lines:
        if not line or line == "--":
            continue
        if mode == "count":
            path, separator, count = line.rpartition(":")
            if not separator or not count.isdigit():
                return False
            names.append(path)
        else:
            matches = list(re.finditer(r"([:-])[1-9]\d*\1",line))
            if not matches or len(matches) > 8:
                return False
            candidates = []
            for match in matches:
                prefix = line[:match.start()]
                path = Path(prefix)
                path = path if path.is_absolute() else cwd / path
                try:
                    relative = path.relative_to(root)
                except ValueError:
                    return False
                # Reject ambiguous private prefixes before probing whether they exist.
                if not allowed_path(relative):
                    return False
                path = safe_path(root,relative.as_posix())
                try:
                    if path.is_file():
                        candidates.append(str(path))
                except OSError:
                    return False
            if len(candidates) != 1:
                return False
            names.extend(candidates)
    return bool(names) and approved_paths(root,cwd,names) is not None


def shorten_filenames(root: Path, cwd: Path, response: dict) -> tuple[dict,str] | None:
    names = response.get("filenames")
    if (not isinstance(names,list) or not names or type(response.get("numFiles")) is not int
            or response["numFiles"] < len(names)):
        return None
    paths = approved_paths(root,cwd,names)
    if paths is None:
        return None
    shortened = [os.path.relpath(path,cwd) if Path(name).is_absolute() else name for name,path in zip(names,paths)]
    if shortened == names:
        return None
    notice = f"ContextPilot: filenames are relative to working directory {json.dumps(str(cwd))}. Every match, its order, counts and truncation flags are preserved."
    return {**response,"filenames":shortened},notice
