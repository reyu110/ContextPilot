import json
from pathlib import Path
import re
import subprocess

import pytest

import scan
import snapshot


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.py").write_text("print('hello')\n")
    (tmp_path / "README.md").write_text("# Example\nA local tool.\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\ndependencies = ["fastapi"]\n')
    return tmp_path


def run_git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def git_project(project: Path) -> Path:
    run_git(project, "init")
    run_git(project, "config", "user.name", "Test")
    run_git(project, "config", "user.email", "test@example.invalid")
    run_git(project, "add", ".")
    run_git(project, "commit", "-m", "Initial commit with no exported message")
    return project


def paths(result: dict) -> list[str]:
    return [item["path"] for item in result["files"]]


def test_discovery_frameworks_entry_points(project: Path) -> None:
    result = scan.scan_repository(project)
    assert paths(result) == ["README.md", "pyproject.toml", "src/main.py"]
    assert result["languages"] == {"Python": 1}
    assert result["framework_signals"] == ["FastAPI"]
    assert "entry-point candidate" in result["files"][2]["roles"][0]
    assert "print('hello')" not in json.dumps(result)


def test_priority_deterministic(project: Path) -> None:
    (project / "z.py").write_text("pass")
    (project / "a.py").write_text("pass")
    first = scan.scan_repository(project, scan.Limits(max_files=2))
    assert paths(first) == ["README.md", "pyproject.toml"]
    assert first == scan.scan_repository(project, scan.Limits(max_files=2))
    assert scan.priority("z.py", {"z.py"}) > scan.priority("a.py")


def test_git_changes(git_project: Path) -> None:
    (git_project / "README.md").write_text("Changed")
    (git_project / "new.py").write_text("pass")
    (git_project / "staged.py").write_text("pass")
    run_git(git_project, "add", "staged.py")
    result = scan.scan_repository(git_project)
    changes = {item["path"]: item["status"] for item in result["git"]["changes"]}
    assert changes == {"README.md": " M", "new.py": "??", "staged.py": "A "}
    assert len(result["git"]["commits"]) == 1
    assert "Initial commit" not in json.dumps(result)


def test_git_rename_and_deletion(git_project: Path) -> None:
    run_git(git_project, "mv", "src/main.py", "src/renamed.py")
    (git_project / "README.md").unlink()
    changes = scan.scan_repository(git_project)["git"]["changes"]
    assert {item["path"] for item in changes} == {"README.md", "src/renamed.py"}


def test_git_subdirectory(git_project: Path) -> None:
    (git_project / "src/main.py").write_text("changed")
    result = scan.scan_repository(git_project / "src")
    assert result["git"]["changes"] == [{"status": " M", "path": "main.py"}]


@pytest.mark.parametrize("use_git", [False, True])
def test_empty_repository(tmp_path: Path, use_git: bool) -> None:
    if use_git:
        run_git(tmp_path, "init")
    result = scan.scan_repository(tmp_path)
    assert result["files"] == []
    assert result["git"]["available"] is use_git
    assert result["git"]["commits"] == []
    assert "No eligible" in snapshot.render_handoff(result, {}, 65536).decode()


def test_non_git_does_not_initialize_project(project: Path) -> None:
    assert scan.scan_repository(project)["git"]["available"] is False
    assert not (project / ".git").exists()


@pytest.mark.parametrize("use_git", [False, True])
def test_gitignore_rules(project: Path, use_git: bool) -> None:
    if use_git:
        run_git(project, "init")
    (project / ".gitignore").write_text("*.txt\n!keep.txt\nignored/\n")
    (project / "keep.txt").write_text("yes")
    (project / "omit.txt").write_text("no")
    (project / "ignored").mkdir()
    (project / "ignored/file.py").write_text("no")
    (project / "src/.gitignore").write_text("hidden.py\n")
    (project / "src/hidden.py").write_text("no")
    result = paths(scan.scan_repository(project))
    assert "keep.txt" in result
    assert not {"omit.txt", "ignored/file.py", "src/hidden.py"} & set(result)


@pytest.mark.parametrize("name", [".env", ".env.local", "credentials.json", "secrets.yaml", "private.key", "id_rsa", ".npmrc", ".ssh/config", ".aws/config", "node_modules/mod.py", ".venv/main.py", "dist/index.js", ".git/config"])
def test_secret_and_dependency_exclusion(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("DO_NOT_EXPORT")
    result = scan.scan_repository(tmp_path)
    assert name not in paths(result)
    assert "DO_NOT_EXPORT" not in json.dumps(result)


def test_secret_content_not_exported(project: Path) -> None:
    (project / "settings.py").write_text('api_key = "supersecretvalue123456"')
    result = scan.scan_repository(project)
    assert "settings.py" not in paths(result)
    assert result["stats"]["sensitive_skipped"] == 1
    assert "supersecret" not in json.dumps(result)


def test_binary_and_invalid_utf8(project: Path) -> None:
    (project / "blob.py").write_bytes(b"abc\x00def")
    (project / "encoded.txt").write_bytes(b"\xff\xfe")
    (project / "photo.png").write_bytes(b"image")
    result = scan.scan_repository(project)
    assert result["stats"]["binary_skipped"] == 2
    assert not {"blob.py", "encoded.txt", "photo.png"} & set(paths(result))


def test_partial_unicode_character_is_not_binary(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("雪" * 20)
    result = scan.scan_repository(tmp_path, scan.Limits(max_file_bytes=2))
    assert paths(result) == ["README.md"]
    assert result["stats"]["binary_skipped"] == 0
    assert result["files"][0]["partial"]


def test_long_confirmed_note_is_preserved(project: Path) -> None:
    text = "A" * 500 + " because correctness requires it."
    output = snapshot.render_handoff(scan.scan_repository(project), {"decisions": [{"text": text, "source": "confirmed"}]}, 65536)
    assert text in output.decode()


def test_symlinks_skipped_inside_and_outside(project: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / (tmp_path.name + "-outside.txt")
    outside.write_text("outside")
    try:
        (project / "link.txt").symlink_to(outside)
        (project / "internal.py").symlink_to(project / "src/main.py")
        (project / "linked-dir").symlink_to(project / "src", target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable")
    result = paths(scan.scan_repository(project))
    assert not {"link.txt", "internal.py", "linked-dir/main.py"} & set(result)


def test_unicode_and_markdown_names(project: Path) -> None:
    (project / "雪[link].py").write_text("pass")
    result = scan.scan_repository(project)
    assert "雪[link].py" in paths(result)
    assert "雪\\[link\\].py" in snapshot.render_handoff(result, {}, 65536).decode()


def test_missing_root(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        scan.scan_repository(tmp_path / "missing")


def test_file_disappears_between_discovery_and_read(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = scan.read_local
    def missing(root: Path, name: str, limit: int) -> bytes:
        if name == "README.md":
            raise FileNotFoundError(name)
        return original(root, name, limit)
    monkeypatch.setattr(scan, "read_local", missing)
    result = scan.scan_repository(project)
    assert "README.md" not in paths(result)
    assert result["warnings"]


def test_large_repo_and_read_limits(tmp_path: Path) -> None:
    for index in range(150):
        (tmp_path / f"file{index:03}.py").write_text("x" * 200)
    limits = scan.Limits(max_files=10, max_bytes=99, max_file_bytes=20, max_candidates=100)
    result = scan.scan_repository(tmp_path, limits)
    assert result["stats"]["bytes_read"] == 99
    assert len(result["files"]) == 5
    assert result["stats"]["discovery_truncated"]
    assert result["stats"]["selection_truncated"]


def test_snapshot_notes_backup_and_no_source_changes(project: Path) -> None:
    (project / ".contextpilot").mkdir()
    notes = {"objectives": [{"text": "Ship MVP", "source": "user"}],
             "decisions": [{"text": "Use JSON", "source": "confirmed"}],
             "tasks": [{"text": "Add CLI docs", "source": "user"}]}
    (project / ".contextpilot/notes.json").write_text(json.dumps(notes))
    before = (project / "src/main.py").read_bytes()
    result = snapshot.create_snapshot(project)
    handoff = (project / ".contextpilot/HANDOFF.md").read_text()
    for heading in snapshot.NOTE_SECTIONS.values():
        assert "## " + heading in handoff
    assert "[Explicitly confirmed] Use JSON" in handoff
    assert "[User-provided] Add CLI docs" in handoff
    assert "[Unverified assumption]" in handoff
    assert result["scan_bytes"] > 0
    assert (project / "src/main.py").read_bytes() == before
    assert (project / ".gitignore").read_text() == "/.contextpilot/\n"
    snapshot.create_snapshot(project)
    assert (project / ".contextpilot/HANDOFF.previous.md").read_text() == handoff
    assert (project / ".gitignore").read_text().count("/.contextpilot/") == 1


@pytest.mark.parametrize("notes", [{"decisions": [{"text": "Invented", "source": "assumption"}]}, {"tasks": ["bad"]}, {"tasks": [{"text": "x", "source": "model"}]}, {"unknown": []}, {"tasks": [{"text": "api_key=supersecret123", "source": "user"}]}])
def test_invalid_notes_fail_without_writes(project: Path, notes: dict) -> None:
    (project / ".contextpilot").mkdir()
    (project / ".contextpilot/notes.json").write_text(json.dumps(notes))
    with pytest.raises(ValueError):
        snapshot.create_snapshot(project)
    assert not (project / ".contextpilot/HANDOFF.md").exists()
    assert not (project / ".gitignore").exists()


def test_resume_verifies_and_never_writes(project: Path) -> None:
    snapshot.create_snapshot(project)
    saved = {path: path.read_bytes() for path in (project / ".contextpilot").iterdir()}
    (project / "src/main.py").write_text("new implementation")
    output = snapshot.resume(project)
    assert "may be outdated" in output
    assert "src/main.py" in output
    assert "No next task explicitly documented" in output
    assert saved == {path: path.read_bytes() for path in (project / ".contextpilot").iterdir()}


def test_resume_deleted_file_and_changed_handoff(project: Path) -> None:
    snapshot.create_snapshot(project)
    (project / "src/main.py").unlink()
    handoff = project / ".contextpilot/HANDOFF.md"
    handoff.write_text(handoff.read_text() + "\nAn unverified edit\n")
    output = snapshot.resume(project)
    assert "src/main.py" in output
    assert "Handoff was edited" in output


def test_resume_next_task_and_cache(project: Path) -> None:
    (project / ".contextpilot").mkdir()
    (project / ".contextpilot/notes.json").write_text(json.dumps({"tasks": [{"source": "user", "text": "Finish tests"}]}))
    snapshot.create_snapshot(project)
    output = snapshot.resume(project)
    assert "Finish tests" in output
    assert "metadata matches" in output
    assert "cache hit: True" in output


def test_cache_invalidates_and_refreshes(project: Path) -> None:
    first = scan.scan_repository(project)
    cached = scan.scan_repository(project, cached=first)
    assert cached["stats"]["cache_hit"]
    assert cached["stats"]["bytes_read_this_run"] == 0
    (project / "README.md").write_text("different")
    assert not scan.scan_repository(project, cached=first)["stats"]["cache_hit"]
    assert not scan.scan_repository(project)["stats"]["cache_hit"]


def test_status_is_read_only_and_missing_resume(project: Path) -> None:
    assert "No snapshot" in snapshot.status(project)
    with pytest.raises(ValueError, match="snapshot first"):
        snapshot.resume(project)
    assert not (project / ".contextpilot").exists()


@pytest.mark.parametrize("target", [".contextpilot", ".contextpilot/HANDOFF.md", ".gitignore"])
def test_output_symlinks_rejected(project: Path, target: str, tmp_path: Path) -> None:
    outside = tmp_path.parent / (tmp_path.name + "-target")
    if target == ".contextpilot":
        outside.mkdir()
    else:
        outside.write_text("do not change")
        (project / ".contextpilot").mkdir(exist_ok=True)
    (project / target).symlink_to(outside, target_is_directory=target == ".contextpilot")
    with pytest.raises(ValueError, match="Symlink"):
        snapshot.create_snapshot(project)
    if outside.is_file():
        assert outside.read_text() == "do not change"


def test_artifact_traversal_rejected(project: Path) -> None:
    with pytest.raises(ValueError):
        scan.write_artifact(project, "../escaped.txt", b"no")
    with pytest.raises(ValueError):
        scan.safe_path(project, "../escaped.txt")


def test_output_size_limits(tmp_path: Path) -> None:
    for index in range(40):
        (tmp_path / (f"{index:02}-" + "雪" * 40 + ".py")).write_text("pass")
    limits = scan.Limits(max_output_bytes=4096)
    result = scan.scan_repository(tmp_path, limits)
    assert len(scan.json_bytes(result)) <= 4096
    assert result["stats"]["output_truncated"]
    cached = scan.scan_repository(tmp_path, limits, result)
    assert cached["stats"]["cache_hit"]
    assert cached["stats"]["output_truncated"]
    notes = {"tasks": [{"text": "雪" * 300, "source": "user"} for _ in range(20)]}
    output = snapshot.render_handoff(result, notes, 4096)
    assert len(output) <= 4096
    assert "## Unfinished tasks" in output.decode()
    assert "Output limit omitted" in output.decode()


def test_invalid_limits() -> None:
    with pytest.raises(ValueError):
        scan.Limits(max_bytes=0)
    with pytest.raises(ValueError):
        scan.Limits(max_output_bytes=100)


def test_no_git_fails_closed(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args: object, **kwargs: object) -> bytes:
        raise FileNotFoundError("git")
    monkeypatch.setattr(scan, "git", unavailable)
    result = scan.scan_repository(project)
    assert not result["files"]
    assert "Git is required" in result["warnings"][0]


def test_ignored_artifacts_not_in_git_changes(git_project: Path) -> None:
    snapshot.create_snapshot(git_project)
    output = run_git(git_project, "status", "--porcelain", "--untracked-files=all")
    assert ".contextpilot" not in output


def test_tracked_ignored_file_is_not_exported(git_project: Path) -> None:
    (git_project / ".gitignore").write_text("README.md\n")
    (git_project / "README.md").write_text("changed")
    result = scan.scan_repository(git_project)
    assert "README.md" not in paths(result)
    assert "README.md" not in {item["path"] for item in result["git"]["changes"]}


def test_first_git_snapshot_cache_and_readonly_resume(git_project: Path) -> None:
    snapshot.create_snapshot(git_project)
    assert "cache hit: True" in snapshot.resume(git_project)
    second = snapshot.create_snapshot(git_project)
    assert second["cache_hit"]
    assert second["bytes_read_this_run"] == 0
    assert not snapshot.create_snapshot(git_project, refresh=True)["cache_hit"]


def test_mixed_snapshot_generation_is_flagged(project: Path) -> None:
    snapshot.create_snapshot(project)
    manifest = project / ".contextpilot/snapshot.json"
    value = json.loads(manifest.read_text())
    value["fingerprint"] = "different generation"
    manifest.write_text(json.dumps(value))
    assert "generation records do not match" in snapshot.resume(project)


def test_cli_all_modes_and_saved_resume_limits(project: Path) -> None:
    import sys
    script = Path(snapshot.__file__)
    def cli(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(script), *args, "--root", str(project)], capture_output=True, text=True)
    assert cli("status").returncode == 0
    created = cli("snapshot", "--max-files", "2")
    assert created.returncode == 0, created.stderr
    assert json.loads(created.stdout)["handoff_bytes"] > 0
    continued = cli("resume")
    assert continued.returncode == 0, continued.stderr
    assert "cache hit: True" in continued.stdout
    bad = cli("snapshot", "--max-files", "0")
    assert bad.returncode == 2
    assert "positive" in bad.stderr


def test_frontmatter_and_skill_installation_contract() -> None:
    skill = Path(__file__).resolve().parents[1] / ".claude/skills/contextpilot/SKILL.md"
    front, body = skill.read_text().removeprefix("---\n").split("\n---\n", 1)
    fields = dict(line.split(": ", 1) for line in front.splitlines())
    assert fields["name"] == "contextpilot"
    assert fields["disable-model-invocation"] == "true"
    assert fields["description"]
    # Both agents resolve bundled commands from the catalog's installed path.
    commands = re.findall(r'"/installed/contextpilot/(scripts/[^"\s]+)"', body)
    assert commands and all((skill.parent / command).is_file() for command in commands)
    assert all(word in body for word in ("snapshot", "resume", "status", "$ARGUMENTS"))
