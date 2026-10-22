"""Native features stay owned by Claude; observation never invents savings."""
import json
from pathlib import Path

import pytest

import optimizer
from optimizer_report import report
from policies import FEATURES, native_controls
from test_auto_features import project, start
from test_optimizer import event


@pytest.mark.parametrize("value,expected",[(None,"platform_default"),("true","enabled"),("false","disabled"),("auto","automatic"),("auto:5","automatic"),("auto:101","custom"),("auto:"+"9"*5000,"custom"),(["private"],"custom")])
def test_native_tool_search_flags_are_labels_not_values(value, expected: str) -> None:
    settings = {} if value is None else {"env":{"ENABLE_TOOL_SEARCH":value}}
    result = native_controls(settings,{})
    assert result["tool_search"] == {"mode":expected,"source":"platform" if value is None else "settings"}
    assert result["prompt_cache"] == {"mode":"platform_default","source":"platform"}
    assert "private" not in json.dumps(result)


def test_native_control_environment_precedence_and_saved_preferences_are_readonly(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    start(project)
    path = project / ".claude/settings.json"
    path.write_text(json.dumps({"env":{"ENABLE_TOOL_SEARCH":"false","DISABLE_PROMPT_CACHING":"1"},"model":"user-choice"}))
    monkeypatch.setenv("ENABLE_TOOL_SEARCH","auto:5")
    monkeypatch.delenv("DISABLE_PROMPT_CACHING",raising=False)
    before = {str(p.relative_to(project)):p.read_bytes() for p in project.rglob("*") if p.is_file()}
    controls = optimizer.status(project)["native_controls"]
    assert controls["tool_search"] == {"mode":"automatic","source":"environment"}
    assert controls["prompt_cache"] == {"mode":"disabled","source":"settings"}
    assert before == {str(p.relative_to(project)):p.read_bytes() for p in project.rglob("*") if p.is_file()}
    monkeypatch.setenv("ENABLE_TOOL_SEARCH","do-not-export-this-private-value")
    result = optimizer.status(project)
    assert result["native_controls"]["tool_search"]["mode"] == "custom"
    assert "do-not-export" not in json.dumps(result)


def test_native_preference_errors_never_look_like_enabled_defaults(project: Path) -> None:
    start(project)
    (project / ".claude/settings.json").write_text('{"env": []}')
    result = optimizer.status(project)
    assert result["preferences_warning"]
    assert all(control["mode"] == "unavailable" for control in result["native_controls"].values())


def test_tool_search_observation_is_prompt_local_idempotent_and_not_savings(project: Path) -> None:
    start(project)
    before = report(project)["lifetime"]
    pre = event(project,"PreToolUse","native-search",tool_name="ToolSearch",tool_input={"query":"private query not retained","max_results":5})
    post = {**pre,"hook_event_name":"PostToolUse","tool_response":{"tools":[{"description":"private tool metadata not retained"}]}}
    assert optimizer.hook(project,pre,plugin=True) == {}
    assert optimizer.hook(project,post,plugin=True) == {}
    assert optimizer.hook(project,post,plugin=True) == {}
    result = report(project)
    assert result["lifetime"]["actions"] == before["actions"]+1
    for field in ("before_bytes","after_bytes","net_tokens"):
        assert result["lifetime"][field] == before[field]
    feature = next(feature for feature in result["features"] if feature["id"] == "mcp")
    assert feature["mode"] == "native" and feature["actions"] == 1 and feature["net_tokens"] == 0
    raw = (project / optimizer.DB).read_bytes()
    assert b"private query" not in raw and b"private tool metadata" not in raw
    late = {**pre,"tool_use_id":"late-search"}
    optimizer.hook(project,late,plugin=True)
    optimizer.hook(project,event(project,"UserPromptSubmit"),plugin=True)
    assert optimizer.hook(project,{**late,"hook_event_name":"PostToolUse"},plugin=True) == {}
    assert report(project)["lifetime"] == result["lifetime"]


def test_platform_limits_are_available_without_creating_history(project: Path) -> None:
    before = list(project.rglob("*"))
    result = report(project)
    assert len(result["limitations"]) == 5
    assert all(row["label"] and row["reason"] for row in result["limitations"])
    assert list(project.rglob("*")) == before
    assert next(row[2] for row in FEATURES if row[0] == "cache") == "native"
