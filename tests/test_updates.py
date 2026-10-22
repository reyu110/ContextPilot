"""Update discovery never executes remote code, uploads project data or rewrites installs."""
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import urllib.error

import pytest

import codex
import optimizer
import updates
from test_auto_features import project, start
from test_optimizer import event
from test_pilotboard import running_server, request


def contents(root: Path) -> dict:
    return {str(path.relative_to(root)):path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_cached_checks_semantic_comparison_and_explicit_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100000.0]
    monkeypatch.setattr(updates.time,"time",lambda:now[0])
    calls = []
    monkeypatch.setattr(updates,"fetch_latest",lambda:calls.append(1) or "0.10.0")
    result = updates.check(tmp_path,agent="codex")
    assert result["state"] == "available" and len(calls) == 1
    assert updates.check(tmp_path)["latest"] == "0.10.0" and len(calls) == 1
    updates.check(tmp_path,force=True)
    assert len(calls) == 2
    now[0] += updates.INTERVAL+1
    assert updates.status(tmp_path)["stale"]
    updates.check(tmp_path)
    assert len(calls) == 3
    assert updates.version("0.10.0") > updates.version("0.9.0")


@pytest.mark.parametrize("failure",[urllib.error.URLError("never display this private proxy detail"),http.client.IncompleteRead(b"partial")])
def test_failure_is_cached_without_fabricating_current_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    calls = []
    def offline():
        calls.append(1)
        raise failure
    monkeypatch.setattr(updates,"fetch_latest",offline)
    result = updates.check(tmp_path)
    assert result["state"] == "unavailable" and result["latest"] is None
    assert "private proxy" not in json.dumps(result)
    updates.check(tmp_path)
    assert len(calls) == 1
    monkeypatch.setattr(updates,"fetch_latest",updates.installed_version)
    assert updates.check(tmp_path,force=True)["state"] == "current"
    monkeypatch.setattr(updates,"fetch_latest",offline)
    result = updates.check(tmp_path,force=True)
    assert result["state"] == "stale" and result["latest"] == updates.installed_version()
    assert result["warning"]


def test_invalid_cached_timestamp_is_preserved_without_disrupting_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates,"fetch_latest",updates.installed_version)
    updates.check(tmp_path)
    path = tmp_path / ".contextpilot" / updates.CACHE
    saved = json.loads(path.read_text())
    saved["attempted_at"] = 10**500
    path.write_text(json.dumps(saved))
    before = contents(tmp_path)
    monkeypatch.delenv("CONTEXTPILOT_UPDATE_CHECK")
    assert updates.status(tmp_path)["state"] == "unavailable"
    assert updates.session_notice(tmp_path,agent="codex") == ""
    assert contents(tmp_path) == before


def test_notices_once_per_new_version_per_agent_preserve_restart_message(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTEXTPILOT_UPDATE_CHECK")
    calls = []
    monkeypatch.setattr(updates,"fetch_latest",lambda:calls.append(1) or "1.0.0")
    result = start(project)
    assert "restart" in result["systemMessage"] and "1.0.0" in result["systemMessage"]
    assert "update" not in result["hookSpecificOutput"]["additionalContext"].lower()
    assert "systemMessage" not in start(project)
    result = codex.hook(project,{"session_id":"codex","cwd":str(project),"hook_event_name":"SessionStart"})
    assert "$contextpilot update" in result["systemMessage"]
    assert len(calls) == 1
    assert updates.session_notice(project,agent="codex") == ""
    assert set(updates.cache(project)["notified"]) == {"claude-code","codex"}


def test_disabled_automatic_checks_do_not_write_but_manual_check_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates,"fetch_latest",lambda:"1.0.0")
    assert updates.session_notice(tmp_path,agent="codex") == ""
    assert list(tmp_path.iterdir()) == []
    assert updates.check(tmp_path,force=True)["state"] == "available"
    assert updates.status(tmp_path)["automatic_checks"] is False


@pytest.mark.parametrize("raw",[b'[]',b'{"version":true}',b'{"version":"1.0.0; run code"}',b'{"version":"1.0.0","command":"evil"}',b'{"version":"1.0.0-beta"}',b'x'*513])
def test_untrusted_version_metadata_is_strictly_bounded(raw: bytes) -> None:
    with pytest.raises(ValueError): updates.decode_version(raw)


def test_fetch_uses_fixed_url_no_redirects_bounded_body_and_no_project_data(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def geturl(self): return updates.URL
        def read(self,size):
            assert size == 513
            return b'{"version":"1.0.0"}'
    class Opener:
        def open(self,req,timeout):
            assert req.full_url == updates.URL and req.data is None and timeout == 2
            assert not {key.lower() for key in req.headers} & {"authorization","cookie"}
            return Response()
    def opener(handler):
        assert isinstance(handler,updates.NoRedirect)
        assert handler.redirect_request(None,None,302,"",{},"https://evil.example") is None
        return Opener()
    monkeypatch.setattr(updates.urllib.request,"build_opener",opener)
    assert updates.fetch_latest() == "1.0.0"
    monkeypatch.setattr(Response,"geturl",lambda self:"https://evil.example")
    with pytest.raises(ValueError): updates.fetch_latest()


def test_corrupt_cache_and_symlinks_fail_open_without_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTEXTPILOT_UPDATE_CHECK")
    folder = tmp_path / ".contextpilot"
    folder.mkdir()
    path = folder / updates.CACHE
    path.write_bytes(b"preserve corrupt metadata")
    monkeypatch.setattr(updates,"fetch_latest",lambda:pytest.fail("must validate cache before requesting"))
    before = contents(tmp_path)
    assert updates.status(tmp_path)["state"] == "unavailable"
    assert updates.session_notice(tmp_path,agent="codex") == ""
    with pytest.raises(ValueError): updates.check(tmp_path)
    assert contents(tmp_path) == before
    path.unlink()
    outside = tmp_path.parent / "private-update-cache.json"
    outside.write_bytes(b"private external content")
    path.symlink_to(outside)
    assert updates.session_notice(tmp_path,agent="codex") == ""
    assert outside.read_bytes() == b"private external content"
    assert not (tmp_path / ".gitignore").exists()


def test_update_lock_failure_cannot_disable_optimizer_or_clobber_other_lock(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTEXTPILOT_UPDATE_CHECK")
    (project / ".contextpilot").mkdir()
    lock = project / ".contextpilot/update-check.lock"
    lock.write_text("held")
    result = start(project)
    assert result["hookSpecificOutput"] and optimizer.status(project)["enabled"]
    assert lock.read_text() == "held"


@pytest.mark.parametrize("agent",["claude-code","codex"])
def test_dashboard_reads_cached_updates_without_network_or_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str) -> None:
    monkeypatch.setattr(updates,"fetch_latest",lambda:"1.0.0")
    updates.check(tmp_path)
    monkeypatch.setattr(updates,"fetch_latest",lambda:pytest.fail("read-only dashboard must not request metadata"))
    before = contents(tmp_path)
    with running_server(tmp_path,agent=agent) as server:
        auth = {"X-Pilotboard-Key":server.dashboard_url.split("#")[1]}
        code,_,body = request(server,"/api/usage",auth)
        result = json.loads(body)
        assert code == 200 and result["updates"]["state"] == "available"
        assert result["updates"]["commands"]["skills-global"]["command"].endswith("--agent "+agent+" --global --yes")
        assert request(server,"/api/usage",auth,"POST")[0] == 405
        (tmp_path / ".contextpilot" / updates.CACHE).write_bytes(b"corrupted")
        result = json.loads(request(server,"/api/usage",auth)[2])
        assert result["updates"]["state"] == "unavailable" and result["lifetime"]["prompts"] == 0
        (tmp_path / ".contextpilot" / updates.CACHE).write_bytes(before[".contextpilot/"+updates.CACHE])
    assert contents(tmp_path) == before


def test_update_cli_status_is_portable_readonly_and_release_versions_match(tmp_path: Path) -> None:
    installed = tmp_path.parent / (tmp_path.name+" installed bundle")
    shutil.copytree(updates.BUNDLE,installed,ignore=shutil.ignore_patterns("__pycache__"))
    before = contents(tmp_path)
    result = subprocess.run([sys.executable,str(installed / "scripts/updates.py"),"status","--root",str(tmp_path),"--agent","codex"],capture_output=True,text=True)
    assert result.returncode == 0 and json.loads(result.stdout)["state"] == "unchecked"
    assert contents(tmp_path) == before
    root = Path(__file__).resolve().parents[1]
    expected = updates.installed_version()
    for name in (".claude-plugin/plugin.json",".codex-plugin/plugin.json"):
        assert json.loads((root / name).read_text())["version"] == expected
    assert json.loads((root / ".claude-plugin/marketplace.json").read_text())["plugins"][0]["version"] == expected
    assert tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"] == expected
