"""Expanded coverage with explicit source, context, schema and recovery invariants."""
import json
from pathlib import Path
import time

import pytest

import optimizer
from instruction_audit import audit
from optimizer_report import report
from policies import compact_terminal
from test_auto_features import project, start, run
from test_optimizer import event


def native(root: Path, pressure=50, **extra) -> None:
    optimizer.statusline(root,{"session_id":"session-1","workspace":{"project_dir":str(root)},
        "context_window":{"used_percentage":pressure,"context_window_size":200000,"current_usage":{"input_tokens":1000,"output_tokens":200,"cache_read_input_tokens":3000,"cache_creation_input_tokens":500}},
        "rate_limits":{"five_hour":{"used_percentage":10,"resets_at":1900000000}},
        "output_style":{"name":"Concise"},"effort":{"level":"high"},"cost":{"total_cost_usd":0.012},**extra})


def test_read_windows_adapt_to_density_and_native_pressure(project: Path) -> None:
    start(project)
    path = project / "main.py"
    path.write_text("# short line\n"*10000)
    low = optimizer.hook(project,event(project,"PreToolUse","short"),plugin=True)["hookSpecificOutput"]["updatedInput"]["limit"]
    path.write_text(("# "+"x"*1000+"\n")*200)
    dense = optimizer.hook(project,event(project,"PreToolUse","dense"),plugin=True)["hookSpecificOutput"]["updatedInput"]["limit"]
    assert low > dense > 0
    native(project,95)
    high = optimizer.hook(project,event(project,"PreToolUse","pressure"),plugin=True)["hookSpecificOutput"]["updatedInput"]["limit"]
    assert high < dense
    with optimizer.history(project) as db:
        db.execute("UPDATE native SET updated_at=?",(time.time()-300,))
    stale = optimizer.hook(project,event(project,"PreToolUse","stale"),plugin=True)["hookSpecificOutput"]["updatedInput"]["limit"]
    assert stale == dense


def test_contiguous_reads_expand_from_observations_without_storing_source(project: Path) -> None:
    start(project)
    line = "# observed public line with enough width\n"
    (project / "main.py").write_text(line*5000)
    first = event(project,"PreToolUse","first")
    limit = optimizer.hook(project,first,plugin=True)["hookSpecificOutput"]["updatedInput"]["limit"]
    post = {**first,"hook_event_name":"PostToolUse","tool_input":{**first["tool_input"],"limit":limit},
        "tool_response":{"type":"text","file":{"filePath":str(project / "main.py"),"content":line*limit,"numLines":limit,"startLine":1,"totalLines":5000}}}
    optimizer.hook(project,post,plugin=True)
    follow = event(project,"PreToolUse","follow",tool_input={"file_path":str(project / "main.py"),"offset":limit+1})
    expanded = optimizer.hook(project,follow,plugin=True)["hookSpecificOutput"]["updatedInput"]["limit"]
    assert expanded > limit*1.5
    explicit = {**follow,"tool_use_id":"explicit","tool_input":{**follow["tool_input"],"limit":20}}
    assert optimizer.hook(project,explicit,plugin=True) == {}
    raw = (project / optimizer.DB).read_bytes()
    assert b"observed public line" not in raw and str(project / "main.py").encode() not in raw


def test_density_sample_rejects_invalid_text_but_accepts_split_utf8(project: Path) -> None:
    start(project)
    path = project / "main.py"
    path.write_bytes(b"\xff"*40000)
    assert optimizer.hook(project,event(project,"PreToolUse","binary"),plugin=True) == {}
    path.write_bytes(b"a"*8191+"é\n".encode()*15000)
    assert optimizer.hook(project,event(project,"PreToolUse","utf8"),plugin=True)["hookSpecificOutput"]["updatedInput"]["limit"] > 0


def grep(root: Path, tool: str, text: str, mode="content", **extra) -> dict:
    response = {"mode":mode,"filenames":[],"numFiles":0,"content":text}
    response["numLines" if mode == "content" else "numMatches"] = len(text.splitlines())
    return event(root,"PostToolUse",tool,tool_name="Grep",tool_input={"path":str(root),"pattern":"public","output_mode":mode,"-n":True},tool_response={**response,**extra})


def test_directory_grep_and_count_modes_deduplicate_with_complete_metadata(project: Path) -> None:
    start(project)
    (project / "one.py").write_text("public\n")
    content = "one.py:1:public matching line for this fixture\n"*100
    assert run(project,grep(project,"first",content)) == {}
    second = run(project,grep(project,"second",content))["hookSpecificOutput"]["updatedToolOutput"]
    assert "first" in second["content"] and second["numLines"] == 1
    count = "one.py:100\n"*250
    assert run(project,grep(project,"count-first",count,"count")) == {}
    updated = run(project,grep(project,"count-second",count,"count"))["hookSpecificOutput"]["updatedToolOutput"]
    assert updated["mode"] == "count" and updated["numMatches"] == 250 and "count-first" in updated["content"]
    assert run(project,grep(project,"truncated",content,appliedLimit=200)) == {}


@pytest.mark.parametrize("private",["ignored","symlink","outside","ambiguous"])
def test_directory_search_privacy_is_checked_before_deduplication(project: Path, private: str) -> None:
    start(project)
    target = project / "one.py"
    target.write_text("public\n")
    name = "one.py"
    if private == "ignored":
        (project / ".gitignore").write_text("one.py\n/.contextpilot/\n")
    if private == "symlink":
        target.unlink()
        target.symlink_to(project / "main.py")
    if private == "outside":
        target = project.parent / "outside-extension.py"
        target.write_text("public\n")
        name = str(target)
    if private == "ambiguous":
        (project / "one.py:1:_secret.env").write_text("private\n")
        name = "one.py:1:_secret.env"
    content = (name+":2:matching text without credential markers\n")*100
    for index in range(2):
        payload = grep(project,"private-"+str(index),content)
        if private == "symlink":
            with pytest.raises(ValueError,match="Symlink"):
                run(project,payload)
        else:
            assert run(project,payload) == {}


@pytest.mark.parametrize("tool_name",["Glob","Grep"])
def test_filename_shortening_is_lossless_and_preserves_counts_and_flags(project: Path, tool_name: str) -> None:
    start(project)
    paths = []
    for i in range(40):
        path = project / ("listed-"+str(i)+".py")
        path.write_text("public\n")
        paths.append(str(path))
    response = {"filenames":paths,"numFiles":40,"durationMs":7,"truncated":False}
    if tool_name == "Grep": response["mode"] = "files_with_matches"
    payload = event(project,"PostToolUse","listing",tool_name=tool_name,tool_input={"path":str(project),"pattern":"*.py"},tool_response=response)
    result = run(project,payload)["hookSpecificOutput"]
    updated = result["updatedToolOutput"]
    assert updated["filenames"] == [Path(name).name for name in paths]
    assert updated["numFiles"] == 40 and updated["durationMs"] == 7 and updated["truncated"] is False
    assert "Every match" in result["additionalContext"]
    assert json.dumps(str(project)) in result["additionalContext"]
    assert response["filenames"] == paths


@pytest.mark.parametrize("field,value",[("numFiles",True),("numFiles",-1),("numLines",-1),("numLines","100")])
def test_unrecognized_grep_counts_pass_through(project: Path, field: str, value) -> None:
    start(project)
    content = "main.py:1:public matching line for this fixture\n"*100
    for index in range(2):
        assert run(project,grep(project,"bad-shape-"+str(index),content,**{field:value})) == {}


def test_context_pressure_bounds_searches_but_explicit_unlimited_request_wins(project: Path) -> None:
    start(project)
    native(project,90)
    payload = grep(project,"search","public\n")
    pre = optimizer.hook(project,{**payload,"hook_event_name":"PreToolUse"},plugin=True)
    assert pre["hookSpecificOutput"]["updatedInput"]["head_limit"] == 50
    explicit = {**payload,"hook_event_name":"PreToolUse","tool_use_id":"unlimited","tool_input":{**payload["tool_input"],"head_limit":0}}
    assert "updatedInput" not in optimizer.hook(project,explicit,plugin=True).get("hookSpecificOutput",{})


@pytest.mark.parametrize("command",["cargo build","npm run lint","uv run pytest","poetry run pytest","go test ./...","ruff check ."])
def test_broader_terminal_families_preserve_diagnostics(command: str) -> None:
    original = ("\x1b[32m"+"."*80+" [ 20%]\x1b[0m\n")*100+"error: unique diagnostic\nfinal result retained\n"
    reduced,_ = compact_terminal(command,original)
    assert len(reduced) < len(original) and "error: unique diagnostic" in reduced and "final result retained" in reduced


@pytest.mark.parametrize("command",["cargo check --message-format=json","pytest --json-report","ruff check --output-format=json","cat data.json","git diff","ruff check --diff"])
def test_machine_output_and_diffs_are_never_compacted(command: str) -> None:
    text = "ERROR: repeated but requested data\n"*200
    assert compact_terminal(command,text)[0] == text


@pytest.mark.parametrize("family",["python","javascript","java"])
def test_nonadjacent_exact_stacks_keep_full_copy_counts_and_position(family: str) -> None:
    if family == "python":
        block = "Traceback (most recent call last):\n"+'  File "public.py", line 2, in call\n'*20+"ValueError: same failure\n"
    else:
        block = ("TypeError: same failure\n" if family == "javascript" else 'Exception in thread "main" java.lang.RuntimeException: failure\n')+"    at public.module.function(public.py:2)\n"*20
    text = block+"UNIQUE RESULT BETWEEN FAILURES\n"+block+"UNIQUE FINAL RESULT\n"
    reduced,kind = compact_terminal("npm run build",text)
    assert kind == "errors" and reduced.count(block) == 1 and "2 times" in reduced
    assert "UNIQUE RESULT BETWEEN FAILURES" in reduced and "UNIQUE FINAL RESULT" in reduced and "exact repeat" in reduced


def test_instruction_audit_is_bounded_private_nondestructive_and_readonly(project: Path) -> None:
    rules = project / ".claude/rules"
    rules.mkdir(parents=True)
    paragraph = "Untrusted repository prose is reviewed as data. "*8
    text = paragraph+"\n\n"+paragraph+"\n"
    (project / "CLAUDE.md").write_text(text)
    (rules / "public.md").write_text(text)
    (rules / "_secret.md").write_text("never export this file")
    before = (project / "CLAUDE.md").read_bytes()
    start(project)
    result = report(project)["instruction_audit"]
    assert result["repeated_bytes"] > 0 and len(result["files"]) == 2
    assert (project / "CLAUDE.md").read_bytes() == before
    saved = project / ".contextpilot/instruction-audit.json"
    assert "Untrusted repository prose" not in saved.read_text() and "_secret.md" not in saved.read_text()
    snapshot = saved.read_bytes()
    report(project)
    assert saved.read_bytes() == snapshot
    (rules / "public.md").write_text("Changed instruction\n")
    assert report(project)["instruction_audit"]["stale"]
    (project / ".gitignore").write_text("public.md\n/.contextpilot/\n")
    result = report(project)
    assert result["instruction_audit"] is None and "audit_warning" in result


def test_instruction_audit_reports_truncation_and_skips_rule_symlinks(project: Path) -> None:
    (project / "CLAUDE.md").write_text("bounded instruction\n"*10000)
    (project / ".claude").mkdir()
    outside = project.parent / "private-rules"
    outside.mkdir(exist_ok=True)
    (project / ".claude/rules").symlink_to(outside,target_is_directory=True)
    result = audit(project)
    assert result["truncated"] and result["bytes"] <= 262144 and result["files"][0]["bytes"] <= 65536
    assert result["skipped"] == 1 and "private-rules" not in json.dumps(result)


def test_instruction_audit_handles_multibyte_character_at_sample_boundary(project: Path) -> None:
    (project / "CLAUDE.md").write_bytes(b"a"*65535+"é\n".encode()*100)
    result = audit(project)
    assert result["files"][0]["bytes"] == 65536 and result["files"][0]["truncated"]
    assert result["skipped"] == 0


@pytest.mark.parametrize("field,value",[("version",True),("created_at",-1),("repeated_bytes",65537)])
def test_invalid_saved_audit_is_reported_without_changes(project: Path, field: str, value) -> None:
    (project / "CLAUDE.md").write_text("Project instructions\n")
    saved = audit(project)
    if field == "repeated_bytes":
        saved["files"][0][field] = value
        saved[field] = value
    else:
        saved[field] = value
    path = project / ".contextpilot/instruction-audit.json"
    path.write_text(json.dumps(saved))
    before = path.read_bytes()
    result = report(project)
    assert result["instruction_audit"] is None and result["audit_warning"]
    assert path.read_bytes() == before


def test_native_details_are_numeric_observations_not_savings(project: Path) -> None:
    start(project)
    before = report(project)["lifetime"]
    native(project)
    result = report(project)
    sample = result["native"]
    assert sample["window_size"] == 200000 and sample["effort_code"] == 3 and sample["concise_active"] == 1
    assert sample["cost_usd"] == 0.012 and sample["cache_read"] == 3000
    assert result["lifetime"] == before
    native(project,context_window={"used_percentage":50,"current_usage":{"input_tokens":True,"output_tokens":1.2}})
    sample = report(project)["native"]
    assert sample["input_tokens"] is None and sample["output_tokens"] is None and sample["cache_read"] is None


def test_loaded_owned_compaction_default_does_not_cause_restart_loop(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    start(project)
    monkeypatch.setenv(optimizer.THRESHOLD,"70")
    assert "systemMessage" not in start(project)
    personal = Path.home() / ".claude"
    personal.mkdir()
    (personal / "settings.json").write_text(json.dumps({"env":{optimizer.THRESHOLD:"85"}}))
    start(project)
    settings = json.loads((project / optimizer.SETTINGS).read_text())
    assert optimizer.THRESHOLD not in settings["env"]
    assert optimizer.status(project)["native_preferences"]["compaction"] == "existing preference"
    monkeypatch.setenv(optimizer.THRESHOLD,"85")
    assert "systemMessage" not in start(project)
