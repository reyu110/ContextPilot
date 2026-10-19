#!/usr/bin/env python3
"""Bounded, local repository inspection. Repository files are data, never code."""
from __future__ import annotations

import argparse
import codecs
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

VERSION = 1
IGNORED_DIRS = {
    ".git", ".contextpilot", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", "dist", "build",
    "coverage", ".next", ".nuxt", "vendor", "target", ".ssh", ".aws", ".gnupg",
}
LANGUAGES = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".rb": "Ruby", ".php": "PHP", ".c": "C", ".h": "C", ".cpp": "C++",
    ".cs": "C#", ".swift": "Swift", ".sh": "Shell", ".sql": "SQL", ".vue": "Vue",
}
TEXT_SUFFIXES = set(LANGUAGES) | {".md", ".rst", ".txt", ".toml", ".json", ".yaml", ".yml", ".ini", ".cfg", ".xml", ".html", ".css"}
CONFIGS = {"pyproject.toml", "package.json", "cargo.toml", "go.mod", "gemfile", "requirements.txt", "dockerfile", "makefile", "setup.cfg", "setup.py", "composer.json"}
ENTRY_NAMES = {"main.py", "app.py", "__main__.py", "manage.py", "main.go", "main.rs", "index.js", "index.ts", "server.js", "cli.py"}
FRAMEWORKS = {"django": "Django", "flask": "Flask", "fastapi": "FastAPI", "react": "React", "next": "Next.js", "vue": "Vue", "svelte": "Svelte", "express": "Express", "pytest": "pytest", "tokio": "Tokio"}
SECRET_NAME = re.compile(r"(^|[._-])(secrets?|credentials?|passwords?|tokens?)([._-]|$)", re.I)
SECRET_CONTENT = re.compile(r"-----BEGIN .*PRIVATE KEY-----|(?:AKIA|ASIA)[A-Z0-9]{16}|(?:gh[pousr]_|sk-(?:ant-)?)[A-Za-z0-9_-]{16,}|(?:password|api[_-]?key|secret|access[_-]?token)\s*[=:]\s*['\"]?[^\s'\"]{8,}", re.I)


@dataclass(frozen=True)
class Limits:
    max_files: int = 120
    max_bytes: int = 262144
    max_file_bytes: int = 16384
    max_output_bytes: int = 65536
    max_candidates: int = 20000

    def __post_init__(self) -> None:
        if any(value <= 0 for value in asdict(self).values()):
            raise ValueError("All limits must be positive")
        if self.max_output_bytes < 4096:
            raise ValueError("max_output_bytes must be at least 4096")


def allowed_path(path: Path) -> bool:
    """Apply privacy exclusions before reading contents or exporting path names."""
    if path.name.lower() == "settings.local.json" and any(part.lower() == ".claude" for part in path.parts[:-1]):
        return False
    for part in path.parts:
        name = part.lower()
        if name in IGNORED_DIRS or name.startswith(".env") or SECRET_NAME.search(name):
            return False
        if name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".npmrc", ".pypirc", ".git-credentials"}:
            return False
        if name.endswith((".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".crt")):
            return False
    return True


def safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Path must stay inside the project")
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlink paths are not allowed")
    return path


def read_local(root: Path, relative: str, limit: int) -> bytes:
    path = safe_path(root, relative)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        return stream.read(limit)


def git(root: Path, *args: str, git_dir: str | None = None) -> bytes:
    # Disable hooks, fsmonitor and optional index writes; never invoke project scripts.
    command = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
    if git_dir:
        command += [f"--git-dir={git_dir}", f"--work-tree={root}"]
    command += ["-C", str(root), *args]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    return subprocess.run(command, capture_output=True, check=True, timeout=15, env=env).stdout


def git_state(root: Path, limits: Limits) -> dict[str, Any]:
    try:
        top = Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel")).strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "changes": [], "commits": [], "note": "No usable Git work tree (or Git unavailable)."}
    try:
        prefix = root.relative_to(top).as_posix()
        prefix = "" if prefix == "." else prefix + "/"
        raw = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".")
        records = raw.split(b"\0")
        changes = []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            status = record[:2].decode("ascii", errors="replace")
            name = os.fsdecode(record[3:])
            if "R" in status or "C" in status:
                index += 1  # Porcelain -z puts the source path in the next record.
            if prefix and not name.startswith(prefix):
                continue
            name = name[len(prefix):]
            if allowed_path(Path(name)):
                changes.append({"status": status, "path": name})
        changes.sort(key=lambda item: (item["path"], item["status"]))
        excluded = ignored_paths(root, [item["path"] for item in changes])
        changes = [item for item in changes if item["path"] not in excluded]
        try:
            commits = git(root, "log", "-5", "--format=%h %cs").decode("ascii").splitlines()
        except subprocess.CalledProcessError:
            commits = []  # An unborn branch has no commits.
        head = commits[0].split()[0] if commits else None
        return {"available": True, "head": head, "commits": commits, "changes": changes[:limits.max_files], "changes_truncated": len(changes) > limits.max_files}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"available": True, "changes": [], "commits": [], "note": f"Git inspection incomplete: {type(exc).__name__}"}


def ignored_paths(root: Path, names: list[str], git_dir: str | None = None) -> set[str]:
    if not names:
        return set()
    command = ["git", "-c", "core.fsmonitor=false"]
    if git_dir:
        command += [f"--git-dir={git_dir}", f"--work-tree={root}"]
    command += ["-C", str(root), "check-ignore", "--no-index", "-z", "--stdin"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(command, input=b"\0".join(os.fsencode(n) for n in names) + b"\0", capture_output=True, timeout=15, env=env)
    if result.returncode not in (0, 1):
        raise OSError("Git ignore check failed")
    return {os.fsdecode(n) for n in result.stdout.split(b"\0") if n}


def priority(name: str, changed: set[str] | None = None) -> int:
    path = Path(name)
    base = path.name.lower()
    score = 0
    if base.startswith(("readme", "architecture", "contributing")):
        score += 100
    if base in CONFIGS:
        score += 90
    if base in ENTRY_NAMES:
        score += 70
    if changed and name in changed:
        score += 50
    if path.suffix.lower() in LANGUAGES:
        score += 20
    if "test" in base or "tests" in path.parts:
        score += 10
    return score - len(path.parts)


def prepare_ignore_engine(root: Path, temporary: str) -> str | None:
    """Share Git's ignore semantics with bounded live-tool inspection."""
    try:
        git(root, "rev-parse", "--git-dir")
        return None
    except (OSError, subprocess.SubprocessError):
        git(root, "init", "--bare", temporary)
        return temporary


def discover(root: Path, limits: Limits) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Use Git's ignore engine even for projects that have not run git init."""
    files: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen = 0
    truncated = False
    with tempfile.TemporaryDirectory(prefix="contextpilot-ignore-") as temporary:
        try:
            ignore_dir = prepare_ignore_engine(root, temporary)
        except (OSError, subprocess.SubprocessError):
            # Fail closed: a missing ignore engine must not expose ignored files.
            return [], ["Git is required to safely respect .gitignore; no files inspected."], False
        def walk_error(error: OSError) -> None:
            warnings.append(f"Directory unavailable: {type(error).__name__}")
        try:
            for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
                relative = Path(directory).relative_to(root)
                dirs[:] = sorted(d for d in dirs if allowed_path(relative / d) and not (Path(directory) / d).is_symlink())
                ignored_dirs = ignored_paths(root, [(relative / d).as_posix() + "/" for d in dirs], ignore_dir)
                dirs[:] = [d for d in dirs if (relative / d).as_posix() + "/" not in ignored_dirs]
                names = sorted(n for n in names if allowed_path(relative / n) and not (Path(directory) / n).is_symlink())
                excluded = ignored_paths(root, [(relative / n).as_posix() for n in names], ignore_dir)
                for name in names:
                    rel = (relative / name).as_posix()
                    if rel in excluded:
                        continue
                    seen += 1
                    if seen > limits.max_candidates:
                        truncated = True
                        break
                    path = Path(directory) / name
                    if path.suffix.lower() not in TEXT_SUFFIXES and name.lower() not in CONFIGS:
                        continue
                    try:
                        stat = path.stat(follow_symlinks=False)
                        if path.is_file():
                            files.append({"path": rel, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
                    except OSError:
                        warnings.append("A file disappeared or became unreadable during discovery.")
                if truncated:
                    break
        except (OSError, subprocess.SubprocessError):
            return [], ["Ignore verification failed; no files inspected."], False
    return files, sorted(set(warnings)), truncated


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")


def fit_scan(result: dict[str, Any], limit: int) -> dict[str, Any]:
    """Drop lowest-ranked detail while retaining explicit truncation statistics."""
    result["stats"].setdefault("output_truncated", False)
    while len(json_bytes(result)) > limit:
        result["stats"]["output_truncated"] = True
        if result["files"]:
            result["files"].pop()
        elif result["git"]["changes"]:
            result["git"]["changes"].pop()
            result["git"]["changes_truncated"] = True
        else:
            raise ValueError("Output budget too small for scan metadata")
    return result


def scan_repository(root: Path, limits: Limits | None = None, cached: dict[str, Any] | None = None) -> dict[str, Any]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Project root must be a directory")
    limits = limits or Limits()
    state = git_state(root, limits)
    candidates, warnings, truncated = discover(root, limits)
    changed = {item["path"] for item in state["changes"]}
    candidates.sort(key=lambda item: (-priority(item["path"], changed), item["path"]))
    fingerprint = hashlib.sha256(json_bytes([str(root), asdict(limits), candidates, state, warnings, truncated])).hexdigest()
    if cached and cached.get("version") == VERSION and cached.get("fingerprint") == fingerprint:
        result = json.loads(json.dumps(cached))
        result["stats"]["cache_hit"] = True
        result["stats"]["bytes_read_this_run"] = 0
        return fit_scan(result, limits.max_output_bytes)
    files = []
    bytes_read = 0
    inspected = 0
    binary_skipped = 0
    sensitive_skipped = 0
    for item in candidates[:limits.max_files]:
        if bytes_read >= limits.max_bytes:
            break
        inspected += 1
        try:
            data = read_local(root, item["path"], min(limits.max_file_bytes, limits.max_bytes - bytes_read))
            bytes_read += len(data)
            if b"\0" in data:
                binary_skipped += 1
                continue
            content = codecs.getincrementaldecoder("utf-8")().decode(data, final=len(data) >= item["size"])
        except UnicodeDecodeError:
            binary_skipped += 1
            continue
        except (OSError, ValueError):
            warnings.append("A selected file disappeared or became unsafe/unreadable.")
            continue
        if SECRET_CONTENT.search(content):
            sensitive_skipped += 1
            continue
        name = Path(item["path"]).name.lower()
        frameworks = []
        if name in CONFIGS:
            frameworks = sorted({label for word, label in FRAMEWORKS.items() if re.search(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])", content, re.I)})
        roles = []
        if name in CONFIGS:
            roles.append("configuration")
        if name.startswith(("readme", "architecture")):
            roles.append("documentation")
        if name in ENTRY_NAMES:
            roles.append("entry-point candidate (filename heuristic)")
        files.append({**item, "language": LANGUAGES.get(Path(name).suffix), "roles": roles, "framework_signals": frameworks, "bytes_read": len(data), "partial": len(data) < item["size"], "sample_sha256": hashlib.sha256(data).hexdigest()})
    result = {
        "version": VERSION, "fingerprint": fingerprint, "project": root.name, "limits": asdict(limits),
        "files": files, "git": state, "warnings": sorted(set(warnings)),
        "languages": dict(sorted(Counter(f["language"] for f in files if f["language"]).items())),
        "framework_signals": sorted({signal for f in files for signal in f["framework_signals"]}),
        "stats": {"candidates": len(candidates), "discovery_truncated": truncated, "files_inspected": inspected,
                  "selection_truncated": inspected < len(candidates), "bytes_read": bytes_read,
                  "bytes_read_this_run": bytes_read, "cache_hit": False, "binary_skipped": binary_skipped,
                  "sensitive_skipped": sensitive_skipped, "approx_input_tokens": (bytes_read + 3) // 4},
    }
    return fit_scan(result, limits.max_output_bytes)


def load_json(root: Path, name: str, limit: int = 1048576) -> dict[str, Any] | None:
    try:
        data = read_local(root, ".contextpilot/" + name, limit + 1)
    except FileNotFoundError:
        return None
    if len(data) > limit:
        raise ValueError(f"{name} exceeds the read limit")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return value


def write_artifact(root: Path, name: str, data: bytes) -> None:
    """All generated artifacts have a fixed, confined destination and atomic replacement."""
    if Path(name).name != name:
        raise ValueError("Artifact name must be a basename")
    destination = safe_path(root, ".contextpilot/" + name)
    destination.parent.mkdir(exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        safe_path(root, ".contextpilot/" + name)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_ignored(root: Path, *, local_settings: bool = False) -> None:
    path = safe_path(root, ".gitignore")
    try:
        existing = read_local(root, ".gitignore", 1048577)
    except FileNotFoundError:
        existing = b""
    if len(existing) > 1048576:
        raise ValueError(".gitignore exceeds safe edit limit")
    rules = [b"/.contextpilot/"] + ([b"/.claude/settings.local.json"] if local_settings else [])
    missing = [rule for rule in rules if rule not in existing.splitlines()]
    if missing:
        with path.open("ab") as stream:
            stream.write((b"\n" if existing and not existing.endswith(b"\n") else b"") + b"\n".join(missing) + b"\n")


def add_limits(parser: argparse.ArgumentParser) -> None:
    for name in asdict(Limits()):
        parser.add_argument("--" + name.replace("_", "-"), type=int)


def limits_from_args(args: argparse.Namespace, base: Limits | None = None) -> Limits:
    return Limits(**{name: getattr(args, name) if getattr(args, name) is not None else default
                     for name, default in asdict(base or Limits()).items()})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    add_limits(parser)
    args = parser.parse_args()
    try:
        result = scan_repository(args.root, limits_from_args(args))
        print(json_bytes(result).decode(), end="")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"contextpilot: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
