"""Automatic startup, conservative transforms and honest signed reporting."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import optimizer
from optimizer_report import report
from policies import FEATURES, GUIDANCE, compact_terminal
from scan import json_bytes
from test_optimizer import event


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(optimizer,"check_version",lambda: None)
    preferences = tmp_path / "personal"
    preferences.mkdir()
    monkeypatch.setattr(Path,"home",classmethod(lambda cls: preferences))
    (tmp_path / "main.py").write_text("# bounded public fixture\n" * 3000)
    return tmp_path


def start(root: Path) -> dict:
    return optimizer.hook(root,event(root,"SessionStart"),plugin=True)


def run(root: Path, payload: dict, plugin: bool = True) -> dict:
    optimizer.hook(root,{**payload,"hook_event_name":"PreToolUse"},plugin=plugin)
    return optimizer.hook(root,payload,plugin=plugin)


def test_plugin_starts_without_manual_on_and_preserves_pause(project: Path) -> None:
    result = start(project)
    assert GUIDANCE in result["hookSpecificOutput"]["additionalContext"]
    assert optimizer.status(project)["enabled"]
    settings = json.loads((project / optimizer.SETTINGS).read_text())
    assert settings["hooks"] == {}  # Plugin supplies hooks; no duplicated registrations.
    assert settings["outputStyle"] == "Concise" and settings["effortLevel"] == "medium"
    assert "statusline" in settings["statusLine"]["command"]
    assert "systemMessage" in result
    assert "systemMessage" not in start(project)  # No endless restart instruction.
    assert not optimizer.configure(project,False)["enabled"]
    assert start(project) == {}  # Restart cannot override an explicit pause.
    assert optimizer.configure(project,True)["enabled"]
    assert start(project)["hookSpecificOutput"]


def test_plugin_migrates_owned_legacy_hooks_without_double_filtering(project: Path) -> None:
    optimizer.configure(project,True)
    start(project)
    assert optimizer.status(project)["mode"] == "plugin"
    assert optimizer.hook(project,event(project,"PreToolUse")) == {}
    assert 1 <= optimizer.hook(project,event(project,"PreToolUse"),plugin=True)["hookSpecificOutput"]["updatedInput"]["limit"] <= 1200


def test_upgrade_from_another_installed_bundle_preserves_owned_cleanup(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_script = project / "old-install/contextpilot/scripts/optimizer.py"
    with monkeypatch.context() as patch:
        patch.setattr(optimizer,"__file__",str(old_script))
        optimizer.configure(project,True,plugin=True)
    assert str(old_script) in (project / optimizer.SETTINGS).read_text()
    start(project)
    assert str(old_script) not in (project / optimizer.SETTINGS).read_text()
    optimizer.configure(project,False)
    assert "statusLine" not in json.loads((project / optimizer.SETTINGS).read_text())


def test_forged_default_cannot_remove_unrelated_statusline(project: Path) -> None:
    start(project)
    state_file = project / ".contextpilot/optimizer-config.json"
    state = json.loads(state_file.read_text())
    state["defaults"]["statusLine"] = {"type":"command","command":"unrelated-command"}
    state_file.write_text(json.dumps(state))
    settings = (project / optimizer.SETTINGS).read_bytes()
    with pytest.raises(ValueError,match="ownership"):
        optimizer.configure(project,False)
    assert (project / optimizer.SETTINGS).read_bytes() == settings


def test_personal_project_and_later_preferences_survive(project: Path) -> None:
    personal = Path.home() / ".claude"
    personal.mkdir()
    custom = {"outputStyle":"Explanatory","effortLevel":"high","statusLine":{"type":"command","command":"existing-status"},"env":{optimizer.THRESHOLD:"88"}}
    (personal / "settings.json").write_text(json.dumps(custom))
    start(project)
    local = json.loads((project / optimizer.SETTINGS).read_text())
    for key in ("outputStyle","effortLevel","statusLine"):
        assert key not in local
    assert optimizer.THRESHOLD not in local["env"]
    assert json.loads((personal / "settings.json").read_text()) == custom
    optimizer.configure(project,False)
    (personal / "settings.json").write_text("{}")
    optimizer.configure(project,True)
    path = project / optimizer.SETTINGS
    edited = json.loads(path.read_text())
    edited["outputStyle"] = "My preference"
    path.write_text(json.dumps(edited))
    optimizer.configure(project,False)
    assert json.loads(path.read_text())["outputStyle"] == "My preference"


def test_legacy_read_only_report_and_atomic_migration(project: Path) -> None:
    folder = project / ".contextpilot"
    folder.mkdir()
    with sqlite3.connect(project / optimizer.DB) as db:
        db.execute("CREATE TABLE reductions(id INTEGER PRIMARY KEY,session_id TEXT,prompt_id TEXT,tool_id TEXT,kind TEXT,before_bytes INTEGER,after_bytes INTEGER,UNIQUE(session_id,tool_id,kind))")
        db.execute("INSERT INTO reductions VALUES (1,'old','old','old','duplicate',4000,100)")
    before = (project / optimizer.DB).read_bytes()
    result = report(project)
    assert result["lifetime"]["net_tokens"] == 975
    assert result["legacy_actions"] == 1 and result["daily"] == []
    assert (project / optimizer.DB).read_bytes() == before
    start(project)
    assert report(project)["legacy_actions"] == 1


def bash(root: Path, tool: str = "bash-1", **updates) -> dict:
    payload = event(root,"PostToolUse",tool,tool_name="Bash",tool_input={"command":"python3 -m pytest -q"},
                 tool_response={"stdout":"." * 80 + " [ 10%]\n" + ("." * 80 + " [ 20%]\n") * 100 + "ERROR: meaningful error\nFAILED test_public.py\n1 failed, 99 passed\n", "stderr":"tool working-directory notice", "interrupted":False})
    return {**payload, **updates}


def test_terminal_noise_removed_without_changing_command_errors_or_notices(project: Path) -> None:
    start(project)
    payload = bash(project)
    before = json.dumps(payload)
    updated = run(project,payload)["hookSpecificOutput"]["updatedToolOutput"]
    assert "meaningful error" in updated["stdout"] and "1 failed" in updated["stdout"]
    assert updated["stderr"] == payload["tool_response"]["stderr"]
    assert "omitted 101 recognized progress" in updated["stdout"]
    assert len(updated["stdout"]) < len(payload["tool_response"]["stdout"])
    assert json.dumps(payload) == before
    assert report(project)["lifetime"]["net_tokens"] > 0


@pytest.mark.parametrize("change", ["shell","secret","background","image","unknown","interrupted","structured"])
def test_unsafe_or_unknown_terminal_results_pass_through(project: Path, change: str) -> None:
    start(project)
    payload = bash(project)
    if change == "shell": payload["tool_input"]["command"] += " | cat"
    if change == "secret": payload["tool_response"]["stdout"] += "api_key='private-test-value'"
    if change == "background": payload["tool_response"]["backgroundTaskId"] = "background"
    if change == "image": payload["tool_response"]["isImage"] = True
    if change == "unknown": payload["tool_response"] = {"result":"unknown"}
    if change == "interrupted": payload["tool_response"]["interrupted"] = True
    if change == "structured": payload["tool_response"]["structuredContent"] = [{"value":1}]
    assert run(project,payload) == {}


def test_grouped_diagnostics_keep_count_and_unique_lines() -> None:
    text = "ERROR: exactly repeated diagnostic\n" * 100 + "Traceback: unique details\n1 failed\n"
    reduced, kind = compact_terminal("pytest -q",text)
    assert kind == "errors" and "100 times" in reduced and "Traceback: unique details" in reduced and "1 failed" in reduced
    assert compact_terminal("cat data.json",text)[0] == text
    assert compact_terminal("pytest",'{"important":42}\n')[0] == '{"important":42}\n'


def test_exact_traceback_blocks_keep_one_complete_copy_and_count() -> None:
    block = 'Traceback (most recent call last):\n  File "public.py", line 2, in test\n    raise ValueError("example")\nValueError: example\n'
    unique = block.replace("ValueError: example","ValueError: another cause")
    reduced, kind = compact_terminal("pytest -q",block * 30 + unique + "2 failed\n")
    assert kind == "errors" and "30 times" in reduced
    assert block in reduced and unique in reduced and "2 failed" in reduced


def test_grep_exact_repeats_are_prompt_local_and_explicitly_recoverable(project: Path) -> None:
    start(project)
    content = "main.py:1: public matching line\n" * 100
    def payload(tool):
        return event(project,"PostToolUse",tool,tool_name="Grep",tool_input={"pattern":"public","path":str(project / "main.py"),"output_mode":"content"},tool_response={"mode":"content","numFiles":0,"filenames":[],"content":content,"numLines":100})
    assert run(project,payload("grep-1")) == {}
    reduced = run(project,payload("grep-2"))["hookSpecificOutput"]["updatedToolOutput"]
    assert "grep-1" in reduced["content"] and reduced["numLines"] == 1
    explicit = payload("grep-3")
    explicit["tool_input"]["head_limit"] = 100
    assert run(project,explicit).get("hookSpecificOutput",{}).get("updatedToolOutput") is None
    optimizer.hook(project,event(project,"UserPromptSubmit"),plugin=True)
    assert run(project,payload("grep-4")) == {}
    raw = (project / optimizer.DB).read_bytes()
    assert content.encode() not in raw and b"public matching line" not in raw


def test_late_terminal_results_cannot_cross_prompt_boundary(project: Path) -> None:
    start(project)
    payload = bash(project)
    optimizer.hook(project,{**payload,"hook_event_name":"PreToolUse"},plugin=True)
    optimizer.hook(project,event(project,"UserPromptSubmit"),plugin=True)
    assert optimizer.hook(project,payload,plugin=True) == {}


def test_boundary_during_read_validation_cannot_reregister_old_call(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    start(project)
    original = optimizer.read_hook
    interrupted = False
    def boundary_before_filter(root, payload):
        nonlocal interrupted
        if payload.get("hook_event_name") == "PreToolUse" and not interrupted:
            interrupted = True
            optimizer.hook(root,event(root,"UserPromptSubmit"),plugin=True)
        return original(root,payload)
    monkeypatch.setattr(optimizer,"read_hook",boundary_before_filter)
    assert optimizer.hook(project,event(project,"PreToolUse","old-request"),plugin=True) == {}
    assert optimizer.hook(project,event(project,"PostToolUse","old-request"),plugin=True) == {}
    assert run(project,event(project,"PostToolUse","new-request")) == {}


def test_adaptive_limit_and_instruction_notice_have_separate_receipts(project: Path) -> None:
    (project / "main.py").write_text("# public large source\n" * 60000)
    (project / "CLAUDE.md").write_text("A public instruction.\n" * 1000)
    result = start(project)
    assert "exceeds 16 KiB" in result["hookSpecificOutput"]["additionalContext"]
    result = optimizer.hook(project,event(project,"PreToolUse"),plugin=True)
    assert 1 <= result["hookSpecificOutput"]["updatedInput"]["limit"] <= 1200
    features = {f["id"]:f for f in report(project)["features"]}
    assert features["adaptive"]["actions"] == features["instructions"]["actions"] == 1
    assert optimizer.totals(project)["bounded_reads"] == 1


def test_retry_guidance_once_and_signed_overhead_is_not_savings(project: Path) -> None:
    start(project)
    results = [optimizer.hook(project,{**bash(project,"retry-"+str(i),tool_input={"command":"unknown-command"}),"hook_event_name":"PreToolUse"},plugin=True) for i in range(4)]
    assert "three times" in results[2]["hookSpecificOutput"]["additionalContext"]
    assert results[3] == {}
    result = report(project)
    assert result["lifetime"]["net_tokens"] < 0
    assert next(f for f in result["features"] if f["id"] == "retry")["actions"] == 1
    assert len(FEATURES) == 28


def test_native_numeric_samples_context_warning_and_no_private_data(project: Path) -> None:
    start(project)
    payload = {"session_id":"session-1","workspace":{"project_dir":str(project)},"transcript_path":"/never/read/this", "prompt":"private prompt must never persist", "context_window":{"used_percentage":72.5,"current_usage":{"input_tokens":4000,"output_tokens":500,"cache_read_input_tokens":3000,"cache_creation_input_tokens":1000}},"cost":{"total_cost_usd":0.04},"rate_limits":{"five_hour":{"used_percentage":20,"resets_at":1800000000},"seven_day":{"used_percentage":40}}}
    assert "context 72%" in optimizer.statusline(project,payload)
    sample = report(project)["native"]
    assert sample["context_used"] == 72.5 and sample["five_hour"] == 20
    first = optimizer.hook(project,event(project,"PreToolUse"),plugin=True)
    assert "elevated context" in first["hookSpecificOutput"]["additionalContext"]
    second = optimizer.hook(project,event(project,"PreToolUse","next-read"),plugin=True)
    assert "elevated context" not in json.dumps(second)
    raw = (project / optimizer.DB).read_bytes()
    assert b"private prompt" not in raw and b"never/read/this" not in raw


def test_report_session_filters_pagination_and_signed_totals(project: Path) -> None:
    start(project)
    with optimizer.history(project) as db:
        for index in range(61):
            optimizer.receipt(db,"other","p"+str(index),"t"+str(index),"terminal",1000,100)
    result = report(project,"other")
    assert len(result["prompts"]) == 50 and result["prompt_count"] == 61
    assert len(report(project,"other",50)["prompts"]) == 11
    assert result["selected"]["net_tokens"] == 61 * 225
    assert result["lifetime"]["net_tokens"] < result["selected"]["net_tokens"]
    before = (project / optimizer.DB).read_bytes()
    report(project)
    assert (project / optimizer.DB).read_bytes() == before


def test_receipt_details_truncate_without_truncating_totals(project: Path) -> None:
    start(project)
    with optimizer.history(project) as db:
        for index in range(25):
            optimizer.receipt(db,"receipts","prompt","tool-"+str(index),"terminal",1000,100)
    result = report(project,"receipts")["prompts"][0]
    assert result["actions"] == 25 and len(result["events"]) == 20
    assert result["events_truncated"] and result["net_tokens"] == 25*225


def test_automatic_checkpoint_preserves_explicit_notes_and_never_reads_history(project: Path) -> None:
    start(project)
    # Use the existing notes schema, never infer a task from the prompt or transcript.
    from snapshot import NOTE_SECTIONS
    section = next(key for key in NOTE_SECTIONS if "task" in key)
    notes = {section:[{"text":"Human supplied next step","source":"user"}]}
    path = project / ".contextpilot/notes.json"
    path.write_text(json.dumps(notes))
    optimizer.hook(project,event(project,"PreCompact",transcript_path="/never/read"),plugin=True)
    assert json.loads(path.read_text()) == notes
    assert "Human supplied next step" in (project / ".contextpilot/HANDOFF.md").read_text()
    assert next(f for f in report(project)["features"] if f["id"] == "checkpoint")["actions"] == 1


def test_plugin_hook_exec_form_with_spaces_and_without_model_call(project: Path) -> None:
    bundle = Path(optimizer.__file__).resolve().parents[1]
    hooks = json.loads((bundle / "hooks/hooks.json").read_text())["hooks"]
    assert set(hooks) == set(optimizer.EVENTS)
    for entries in hooks.values():
        command = entries[0]["hooks"][0]
        assert command["command"] == "python3" and command["onFailure"] == "continue"
        assert "--plugin" in command["args"]
    # Bootstrap uses the same native CLI version gate; mock only in-process, then run an initialized hook.
    start(project)
    script = bundle / "scripts/optimizer.py"
    result = subprocess.run([sys.executable,str(script),"hook","--plugin","--root",str(project)],input=json_bytes(event(project,"UserPromptSubmit",transcript_path="/never/read")),capture_output=True)
    assert result.returncode == 0 and json.loads(result.stdout) == {}


@pytest.mark.parametrize("field", ["context_window","rate_limits","cost"])
def test_malformed_native_samples_fail_open_in_cli(project: Path, field: str) -> None:
    start(project)
    payload = {"session_id":"session-1","workspace":{"project_dir":str(project)},field:[1]}
    result = subprocess.run([sys.executable,optimizer.__file__,"statusline","--root",str(project)],input=json_bytes(payload),capture_output=True)
    assert result.returncode == 0 and b"telemetry unavailable" in result.stdout
