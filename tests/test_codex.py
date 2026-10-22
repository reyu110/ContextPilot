"""Codex support uses trusted lifecycle events, never Claude settings or chat history."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import codex
import optimizer
from optimizer_report import report
from scan import allowed_path
import snapshot
import usage
from test_pilotboard import running_server, request


@pytest.fixture
def project(tmp_path: Path) -> Path:
    subprocess.run(["git","init","-q",str(tmp_path)],check=True)
    (tmp_path / "main.py").write_text("# public source\n"*100)
    (tmp_path / "AGENTS.md").write_text("Preserve all explicit requirements.\n")
    return tmp_path


def event(root: Path, name: str, **extra) -> dict:
    return {"session_id":"codex-session","turn_id":"turn-1","cwd":str(root),"hook_event_name":name,
            "transcript_path":"/must/never/read/session.jsonl","prompt":"never retain this chat prompt",**extra}


def files(root: Path) -> dict:
    return {str(path.relative_to(root)):path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_codex_startup_is_independent_and_preserves_settings_and_pause(project: Path) -> None:
    (project / ".claude").mkdir()
    (project / ".claude/settings.local.json").write_text('{"model":"explicit-choice"}')
    (project / ".codex").mkdir()
    (project / ".codex/config.toml").write_text('model = "explicit-choice"\n')
    (project / ".codex/sessions").mkdir()
    (project / ".codex/sessions/private.jsonl").write_text("private conversation text")
    before = {name:content for name,content in files(project).items() if name.startswith((".claude/",".codex/"))}
    result = codex.hook(project,event(project,"SessionStart"))
    assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert codex.status(project)["enabled"] and not optimizer.status(project)["enabled"]
    assert {name:content for name,content in files(project).items() if name.startswith((".claude/",".codex/"))} == before
    assert not (project / optimizer.DB).exists()
    audit = report(project,agent="codex")["instruction_audit"]
    assert [row["path"] for row in audit["files"]] == ["AGENTS.md"]
    raw = (project / codex.DB).read_bytes()
    assert b"never retain" not in raw and b"/must/never/read" not in raw
    assert not codex.configure(project,False)["enabled"]
    assert codex.hook(project,event(project,"SessionStart")) == {}
    assert codex.configure(project,True)["enabled"]
    assert codex.hook(project,event(project,"SessionStart"))["hookSpecificOutput"]


def test_codex_status_and_empty_report_never_create_files(project: Path) -> None:
    before = files(project)
    assert not codex.status(project)["enabled"]
    assert report(project,agent="codex")["lifetime"]["actions"] == 0
    assert files(project) == before


def test_retry_receipts_are_bounded_prompt_local_and_do_not_grant_permissions(project: Path) -> None:
    codex.hook(project,event(project,"SessionStart"))
    codex.hook(project,event(project,"UserPromptSubmit"))
    for index in range(1,5):
        payload = event(project,"PreToolUse",tool_name="Bash",tool_use_id="tool-"+str(index),tool_input={"command":"pytest private_fixture.py"})
        result = codex.hook(project,payload)
        if index == 3:
            assert result["hookSpecificOutput"].keys() == {"hookEventName","additionalContext"}
            assert codex.hook(project,payload) == {}
        else:
            assert result == {}
    result = report(project,agent="codex")
    assert next(row for row in result["features"] if row["id"] == "retry")["actions"] == 1
    assert result["lifetime"]["net_tokens"] < 0 and result["lifetime"]["before_bytes"] == 0
    assert b"private_fixture.py" not in (project / codex.DB).read_bytes()
    codex.hook(project,event(project,"UserPromptSubmit",turn_id="turn-2"))
    assert codex.hook(project,payload) == {}  # A late old-turn request cannot seed the new turn.
    assert codex.hook(project,{**payload,"turn_id":"turn-2"}) == {}


def test_precompact_checkpoint_preserves_notes_is_idempotent_and_excludes_codex_history(project: Path) -> None:
    codex.hook(project,event(project,"SessionStart"))
    codex.hook(project,event(project,"UserPromptSubmit"))
    section = next(name for name in snapshot.NOTE_SECTIONS if "task" in name)
    notes = {section:[{"text":"Explicit human next step","source":"user"}]}
    path = project / ".contextpilot/notes.json"
    path.write_text(json.dumps(notes))
    (project / ".codex").mkdir()
    (project / ".codex/private.json").write_text('"private conversation body"')
    codex.hook(project,event(project,"PreCompact"))
    assert json.loads(path.read_text()) == notes
    handoff = (project / ".contextpilot/HANDOFF.md").read_text()
    assert "Explicit human next step" in handoff and ".codex" not in handoff
    before = files(project)
    codex.hook(project,event(project,"PreCompact"))
    assert files(project) == before
    assert report(project,agent="codex")["lifetime"]["actions"] == 2
    assert usage.summary(project)["lifetime"]["prompts"] == 1
    assert not (project / optimizer.DB).exists()
    snapshot.resume(project)
    snapshot.status(project)
    assert files(project) == before
    codex.hook(project,event(project,"SessionEnd"))
    assert usage.summary(project)["lifetime"]["prompts"] == 1  # No scan in the three-second shutdown hook.


@pytest.mark.parametrize("extra",[{"agent_id":"subagent"},{"cwd":"/"},{"hook_event_name":"PostToolUse"}])
def test_subagents_outside_roots_and_unhandled_events_pass_unchanged(project: Path, extra: dict) -> None:
    before = files(project)
    payload = event(project,"SessionStart")
    payload.update(extra)
    assert codex.hook(project,payload) == {}
    assert files(project) == before


def test_corrupt_codex_state_and_symlinked_databases_are_preserved(project: Path) -> None:
    codex.configure(project,True)
    path = project / ".contextpilot" / codex.STATE
    path.write_text('{"version":true,"enabled":true}')
    before = files(project)
    with pytest.raises(ValueError): codex.configure(project,True)
    assert files(project) == before
    path.write_text('{"version":1,"enabled":true}')
    outside = project.parent / "outside-codex.sqlite3"
    outside.write_bytes(b"private")
    (project / codex.DB).symlink_to(outside)
    with pytest.raises(ValueError,match="Symlink"): codex.hook(project,event(project,"UserPromptSubmit"))
    assert outside.read_bytes() == b"private"


def test_codex_dashboard_is_readonly_and_keeps_claude_history_separate(project: Path) -> None:
    codex.hook(project,event(project,"SessionStart"))
    with optimizer.history(project) as db:
        optimizer.receipt(db,"claude-session","prompt","tool","duplicate",10000,100)
    before = files(project)
    with running_server(project,agent="codex") as server:
        auth = {"X-Pilotboard-Key":server.dashboard_url.split("#")[1]}
        code,_,body = request(server,"/api/usage",auth)
        result = json.loads(body)
        assert code == 200 and result["agent"] == "codex"
        assert result["optimizer"]["status"]["mode"] == "codex-plugin"
        summary = result["optimizer"]["report"]
        assert summary["native"] is None and summary["lifetime"]["before_bytes"] == 0
        assert all(row["session_id"] != "claude-session" for row in summary["sessions"])
        assert next(row for row in summary["features"] if row["id"] == "duplicate")["mode"] == "unavailable"
        assert request(server,"/api/usage?agent=claude-code",auth)[0] == 400
    assert files(project) == before
    assert report(project)["lifetime"]["net_tokens"] > 0


def test_installed_codex_bundle_and_hook_commands_work_from_spaced_paths(project: Path, tmp_path: Path) -> None:
    source = Path(codex.__file__).resolve().parents[1]
    installed = tmp_path / "installed plugin with spaces"
    target = installed / ".claude/skills/contextpilot"
    shutil.copytree(source,target,ignore=shutil.ignore_patterns("__pycache__"))
    script = target / "scripts/codex.py"
    subprocess.run([sys.executable,str(script),"status","--root",str(project)],check=True,capture_output=True,text=True)
    payload = event(project,"SessionStart")
    hooks = json.loads((target / "hooks/codex.json").read_text())["hooks"]
    command = hooks["SessionStart"][0]["hooks"][0]["command"].replace('${PLUGIN_ROOT}',str(installed))
    result = subprocess.run(command,shell=True,input=json.dumps(payload),cwd=project,capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert not (project / ".claude/settings.local.json").exists()
    for mode in ("snapshot","resume","status"):
        result = subprocess.run([sys.executable,str(target / "scripts/snapshot.py"),mode,"--root",str(project)],capture_output=True,text=True)
        assert result.returncode == 0,result.stderr
    bad = subprocess.run([sys.executable,str(script),"hook","--root",str(project)],input="[]",capture_output=True,text=True)
    assert bad.returncode == 0 and json.loads(bad.stdout) == {}
    assert codex.hook(project,event(project,"PostToolUse",tool_name="Bash",tool_response="original")) == {}


def test_codex_cli_resolves_git_root_from_subdirectory_and_rejects_bad_payloads(project: Path) -> None:
    nested = project / "nested"
    nested.mkdir()
    script = str(Path(codex.__file__).resolve())
    payload = event(nested,"SessionStart")
    result = subprocess.run([sys.executable,script,"hook"],input=json.dumps(payload),cwd=nested,capture_output=True,text=True)
    assert result.returncode == 0 and json.loads(result.stdout)["hookSpecificOutput"]
    assert (project / codex.DB).is_file() and not (nested / ".contextpilot").exists()
    for raw in ("not json","[]",json.dumps({**payload,"session_id":"bad/id"}),"x"*(optimizer.MAX_INPUT+1)):
        result = subprocess.run([sys.executable,script,"hook"],input=raw,cwd=nested,capture_output=True,text=True)
        assert result.returncode == 0 and json.loads(result.stdout) == {}
        assert "bad/id" not in result.stderr and "/must/never/read" not in result.stderr


def test_codex_manifest_routes_to_existing_hooks_and_shared_skills() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / ".codex-plugin/plugin.json").read_text())
    hooks = root / manifest["hooks"]
    assert hooks.is_file() and (root / manifest["skills"] / "contextpilot/SKILL.md").is_file()
    config = json.loads(hooks.read_text())["hooks"]
    assert "PostToolUse" not in config and "PermissionRequest" not in config
    for entries in config.values():
        for entry in entries:
            for handler in entry["hooks"]:
                import shlex
                command = shlex.split(handler["command"].replace("${PLUGIN_ROOT}",str(root)))
                assert command[0] == "python3" and command[-1] == "hook" and Path(command[1]).is_file()
                assert handler["timeout"] <= (3 if entries is config["SessionEnd"] else 30)


@pytest.mark.parametrize("path",[".codex/config.toml",".codex/auth.json",".codex/sessions/session.jsonl",".codex/hooks.json"])
def test_codex_private_state_is_excluded_before_discovery(path: str) -> None:
    assert not allowed_path(Path(path))
    assert allowed_path(Path(".codex-plugin/plugin.json"))
