from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import optimizer
from scan import json_bytes


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(optimizer, "check_version", lambda: None)
    (tmp_path / "main.py").write_text("# sample source line with enough text for bounded reads\n" * 3000)
    return tmp_path


def event(root: Path, name: str, tool: str = "toolu_1", **extra) -> dict:
    content = "# sample source line with enough text for bounded reads\n" * 400
    return {"cwd": str(root), "session_id": "session-1", "hook_event_name": name,
            "tool_name": "Read", "tool_use_id": tool,
            "tool_input": {"file_path": str(root / "main.py")},
            "tool_response": {"type": "text", "file": {"filePath": str(root / "main.py"),
                "content": content, "numLines": 400, "startLine": 1, "totalLines": 3000}}, **extra}


def activate(root: Path) -> None:
    optimizer.configure(root, True)
    optimizer.hook(root, event(root, "UserPromptSubmit", prompt="Private prompt: do not persist this text"))


def complete(root: Path, payload: dict) -> dict:
    optimizer.hook(root, {**payload, "hook_event_name": "PreToolUse"})
    return optimizer.hook(root, payload)


def test_configuration_merge_repeat_enable_and_safe_disable(project: Path) -> None:
    (project / ".claude").mkdir()
    unrelated = {"matcher": "Write", "hooks": [{"type": "command", "command": "my-formatter"}]}
    original = {"permissions": {"deny": ["Bash(rm *)"]}, "env": {"ANTHROPIC_API_KEY": "synthetic-private-value"},
                "hooks": {"PostToolUse": [unrelated]}}
    settings = project / optimizer.SETTINGS
    settings.write_text(json.dumps(original))
    assert optimizer.configure(project, True)["enabled"]
    assert optimizer.configure(project, True)["enabled"]
    active = json.loads(settings.read_text())
    assert active["permissions"] == original["permissions"]
    assert active["env"]["ANTHROPIC_API_KEY"] == "synthetic-private-value"
    assert active["env"][optimizer.THRESHOLD] == "70"
    assert len(active["hooks"]["PostToolUse"]) == 2
    handler = active["hooks"]["PostToolUse"][1]["hooks"][0]
    assert handler["command"] == sys.executable and handler["args"][1] == "hook"
    assert "permissionDecision" not in json.dumps(active)
    assert "synthetic-private-value" not in (project / ".contextpilot/optimizer-config.json").read_text()
    assert not optimizer.configure(project, False)["enabled"]
    assert json.loads(settings.read_text()) == original


def test_existing_threshold_and_later_user_edits_survive(project: Path) -> None:
    (project / ".claude").mkdir()
    path = project / optimizer.SETTINGS
    path.write_text(json.dumps({"env": {optimizer.THRESHOLD: "85", "DISABLE_AUTO_COMPACT": "1"}}))
    optimizer.configure(project, True)
    assert optimizer.status(project)["compaction_percent"] == 85
    optimizer.configure(project, False)
    assert json.loads(path.read_text())["env"] == {optimizer.THRESHOLD: "85", "DISABLE_AUTO_COMPACT": "1"}
    path.write_text("{}")
    optimizer.configure(project, True)
    value = json.loads(path.read_text())
    value["env"][optimizer.THRESHOLD] = "60"
    value["custom"] = {"keep": True}
    path.write_text(json.dumps(value))
    optimizer.configure(project, False)
    assert json.loads(path.read_text()) == {"env": {optimizer.THRESHOLD: "60"}, "custom": {"keep": True}}


def test_status_and_off_on_unused_project_are_readonly(project: Path) -> None:
    before = {p.name: p.read_bytes() for p in project.iterdir()}
    assert not optimizer.status(project)["enabled"]
    assert not optimizer.configure(project, False)["enabled"]
    assert before == {p.name: p.read_bytes() for p in project.iterdir()}


def test_large_unbounded_read_is_explicitly_chunked_without_permissions_change(project: Path) -> None:
    activate(project)
    original = event(project, "PreToolUse")
    result = optimizer.hook(project, original)["hookSpecificOutput"]
    assert result["updatedInput"] == {"file_path": str(project / "main.py"), "limit": 400}
    assert "partial" in result["additionalContext"]
    assert "permissionDecision" not in result
    assert original["tool_input"] == {"file_path": str(project / "main.py")}
    assert optimizer.totals(project)["bounded_reads"] == 1
    for update in ({"limit": 900}, {"allow_large": True}, {"pages": "1-3"}):
        payload = event(project, "PreToolUse", "toolu_explicit")
        payload["tool_input"].update(update)
        assert optimizer.hook(project, payload) == {}


def test_duplicates_are_exact_prompt_local_and_recoverable(project: Path) -> None:
    activate(project)
    first = event(project, "PostToolUse")
    before = copy.deepcopy(first)
    assert complete(project, first) == {}
    duplicate = complete(project, event(project, "PostToolUse", "toolu_2"))["hookSpecificOutput"]["updatedToolOutput"]
    assert duplicate["type"] == "text" and duplicate["file"]["numLines"] == 1
    assert "toolu_1" in duplicate["file"]["content"]
    assert first == before
    assert "sample source" not in duplicate["file"]["content"]
    # An explicit range is an escape hatch even when its content happens to match.
    fresh = event(project, "PostToolUse", "toolu_3")
    fresh["tool_input"]["limit"] = 400
    assert complete(project, fresh) == {}
    changed = event(project, "PostToolUse", "toolu_4")
    changed["tool_response"]["file"]["content"] += "# changed\n"
    assert complete(project, changed) == {}
    for boundary in ("UserPromptSubmit", "PreCompact", "SessionStart"):
        optimizer.hook(project, event(project, boundary))
        assert complete(project, event(project, "PostToolUse", "toolu_new_" + boundary)) == {}
    optimizer.hook(project, event(project, "SessionEnd"))
    assert optimizer.hook(project, event(project, "PostToolUse", "toolu_ended")) == {}
    stats = optimizer.totals(project)
    assert stats["duplicate_reads"] == 1
    assert stats["payload_bytes_avoided"] == len(first["tool_response"]["file"]["content"].encode()) - len(duplicate["file"]["content"].encode())
    raw = (project / optimizer.DB).read_bytes()
    assert b"Private prompt" not in raw and b"sample source line" not in raw
    assert str(project / "main.py").encode() not in raw


def test_parallel_duplicate_reads_retain_one_original(project: Path) -> None:
    activate(project)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda i: complete(project, event(project, "PostToolUse", "toolu_" + str(i))), range(6)))
    assert results.count({}) == 1
    assert optimizer.totals(project)["duplicate_reads"] == 5


@pytest.mark.parametrize("kind", ["secret-name", "secret-content", "ignored", "symlink", "outside", "pdf", "unknown-shape", "subagent"])
def test_privacy_and_unknown_outputs_pass_through(project: Path, kind: str) -> None:
    activate(project)
    payload = event(project, "PostToolUse")
    target = project / "main.py"
    if kind == "secret-name":
        target = project / ".env.private"
        target.write_text("private data")
    elif kind == "secret-content":
        payload["tool_response"]["file"]["content"] += "\napi_key='synthetic-credential-content'"
    elif kind == "ignored":
        (project / ".gitignore").write_text("main.py\n/.contextpilot/\n")
    elif kind == "symlink":
        target = project / "linked.py"
        target.symlink_to(project / "main.py")
    elif kind == "outside":
        target = project.parent / "outside.py"
        target.write_text("outside")
    elif kind == "pdf":
        payload["tool_response"]["type"] = "pdf"
    elif kind == "unknown-shape":
        payload["tool_response"]["file"].pop("totalLines")
    elif kind == "subagent":
        payload["agent_id"] = "different-context"
    payload["tool_input"]["file_path"] = str(target)
    payload["tool_response"]["file"]["filePath"] = str(target)
    for index in range(2):
        payload["tool_use_id"] = "toolu_private_" + str(index)
        if kind == "symlink":
            with pytest.raises(ValueError, match="Symlink"):
                complete(project, payload)
        else:
            assert complete(project, payload) == {}
    assert optimizer.totals(project)["duplicate_reads"] == 0


def test_bad_settings_and_symlinks_are_preserved(project: Path) -> None:
    (project / ".claude").mkdir()
    settings = project / optimizer.SETTINGS
    settings.write_text('{"hooks": "invalid"}')
    with pytest.raises(ValueError):
        optimizer.configure(project, True)
    assert settings.read_text() == '{"hooks": "invalid"}'
    assert not (project / ".contextpilot").exists()
    settings.unlink()
    outside = project / "outside.json"
    outside.write_text("{}")
    settings.symlink_to(outside)
    with pytest.raises(ValueError, match="Symlink"):
        optimizer.configure(project, True)
    assert outside.read_text() == "{}"


def test_late_result_from_interrupted_prompt_cannot_seed_new_context(project: Path) -> None:
    activate(project)
    optimizer.hook(project, event(project, "PreToolUse", "toolu_old"))
    optimizer.hook(project, event(project, "UserPromptSubmit"))
    assert optimizer.hook(project, event(project, "PostToolUse", "toolu_old")) == {}
    assert optimizer.hook(project, event(project, "PostToolUse", "toolu_without_start")) == {}
    assert complete(project, event(project, "PostToolUse", "toolu_new")) == {}
    assert optimizer.totals(project)["duplicate_reads"] == 0


def test_forged_ownership_cannot_remove_an_unrelated_hook(project: Path) -> None:
    activate(project)
    settings = project / optimizer.SETTINGS
    before = settings.read_bytes()
    state_path = project / ".contextpilot/optimizer-config.json"
    state = json.loads(state_path.read_text())
    state["handlers"]["PostToolUse"] = {"matcher": "Bash", "hooks": [{"type": "command", "command": "unrelated"}]}
    state_path.write_text(json.dumps(state))
    with pytest.raises(ValueError):
        optimizer.configure(project, False)
    assert settings.read_bytes() == before


def test_config_lock_and_corrupt_history_do_not_destroy_settings(project: Path) -> None:
    activate(project)
    before = (project / optimizer.SETTINGS).read_bytes()
    lock = project / ".contextpilot/optimizer-config.lock"
    lock.write_text("held")
    with pytest.raises(FileExistsError):
        optimizer.configure(project, False)
    assert (project / optimizer.SETTINGS).read_bytes() == before
    lock.unlink()
    (project / optimizer.DB).write_bytes(b"damaged history to preserve")
    result = optimizer.configure(project, False)
    assert not result["enabled"] and "history_warning" in result
    assert (project / optimizer.DB).read_bytes() == b"damaged history to preserve"


def test_cli_bad_input_fails_open_and_never_reads_transcript(project: Path) -> None:
    activate(project)
    script = Path(optimizer.__file__)
    for value in (b"not json", b"[]", b"x" * (optimizer.MAX_INPUT + 1),
                  json_bytes(event(project, "UserPromptSubmit", session_id="bad/id", transcript_path="/private/never-read"))):
        result = subprocess.run([sys.executable, str(script), "hook", "--root", str(project)], input=value, capture_output=True)
        assert result.returncode == 0 and json.loads(result.stdout) == {}
        assert b"private/never-read" not in result.stderr


def test_generated_hook_exec_form_works_with_spaces_and_no_shell(project: Path) -> None:
    root = project / "space and $(literal) `name`"
    root.mkdir()
    (root / "main.py").write_text("# fixture\n")
    activate(root)
    settings = json.loads((root / optimizer.SETTINGS).read_text())
    command = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    result = subprocess.run([command["command"], *command["args"]], input=json_bytes(event(root, "UserPromptSubmit")), capture_output=True)
    assert result.returncode == 0 and json.loads(result.stdout) == {}
    assert "permissionDecision" not in result.stdout.decode()


def test_old_claude_version_is_rejected_before_any_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "2.1.125 (Claude Code)\n", ""))
    with pytest.raises(ValueError, match="2.1.303"):
        optimizer.configure(tmp_path, True)
    assert not list(tmp_path.iterdir())


def test_exported_override_and_disabled_hooks_are_not_overridden(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(optimizer.THRESHOLD, "45")
    optimizer.configure(project, True)
    assert optimizer.THRESHOLD not in json.loads((project / optimizer.SETTINGS).read_text())["env"]
    optimizer.configure(project, False)
    (project / optimizer.SETTINGS).write_text('{"disableAllHooks":true}')
    with pytest.raises(ValueError, match="disable hooks"):
        optimizer.configure(project, True)
    assert json.loads((project / optimizer.SETTINGS).read_text()) == {"disableAllHooks": True}


def test_failed_settings_replace_preserves_original_configuration(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project / ".claude").mkdir()
    settings = project / optimizer.SETTINGS
    settings.write_text('{"permissions":{"deny":["Bash(rm *)"]}}\n')
    before = settings.read_bytes()
    original_replace = optimizer.os.replace
    def fail_settings(source, destination):
        if Path(destination) == settings:
            raise OSError("simulated settings write failure")
        return original_replace(source, destination)
    monkeypatch.setattr(optimizer.os, "replace", fail_settings)
    with pytest.raises(OSError):
        optimizer.configure(project, True)
    assert settings.read_bytes() == before
    assert not optimizer.status(project)["enabled"]


def test_local_claude_settings_are_excluded_from_scans_and_git(project: Path) -> None:
    import scan
    activate(project)
    settings = project / optimizer.SETTINGS
    value = json.loads(settings.read_text())
    value["env"]["PRIVATE_METADATA"] = "must-not-export-this-value"
    settings.write_text(json.dumps(value))
    result = scan.scan_repository(project)
    assert optimizer.SETTINGS not in [f["path"] for f in result["files"]]
    assert optimizer.SETTINGS not in [f["path"] for f in result["git"]["changes"]]
    assert "must-not-export-this-value" not in json.dumps(result)
    assert "/.claude/settings.local.json" in (project / ".gitignore").read_text().splitlines()
