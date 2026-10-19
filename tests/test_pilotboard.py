from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import sqlite3
import threading

import pytest

import pilotboard
import snapshot
import usage


def test_signed_estimates_rounding_sessions_and_idempotency(tmp_path: Path) -> None:
    usage.record(tmp_path, 400, 80, 0, "session-a", "prompt-1")
    usage.record(tmp_path, 40, 160, 40, "session-b", "prompt-2")
    usage.record(tmp_path, 5, 1, 0, "session-a", "prompt-3")
    usage.record(tmp_path, 800, 0, 0, "session-a", "prompt-1")
    result = usage.summary(tmp_path, "session-a")
    assert result["lifetime"] == {"prompts": 3, "baseline_tokens": 112,
        "handoff_tokens": 61, "saved_tokens": 51, "cache_bytes": 40}
    assert result["selected"]["saved_tokens"] == 81
    assert result["session_count"] == 2
    assert len(result["daily"]) == 1
    assert result["daily"][0]["saved_tokens"] == 81
    assert usage.summary(tmp_path, "session-b")["selected"]["saved_tokens"] == -30
    assert [p["prompt_id"] for p in result["prompts"]] == ["prompt-3", "prompt-1"]


def test_pagination_and_concurrent_writers_preserve_lifetime(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda i: usage.record(tmp_path, 800, 400, 0, "session", "p" + str(i)), range(61)))
    first = usage.summary(tmp_path)
    second = usage.summary(tmp_path, offset=50)
    assert first["lifetime"]["prompts"] == 61
    assert first["lifetime"]["saved_tokens"] == 6100
    assert len(first["prompts"]) == 50 and len(second["prompts"]) == 11
    assert not {p["id"] for p in first["prompts"]} & {p["id"] for p in second["prompts"]}


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_usage_symlinks_are_never_followed(tmp_path: Path, suffix: str) -> None:
    (tmp_path / ".contextpilot").mkdir()
    outside = tmp_path / "outside"
    outside.write_text("private")
    (tmp_path / (usage.DB_NAME + suffix)).symlink_to(outside)
    with pytest.raises(ValueError, match="Symlink"):
        usage.record(tmp_path, 4, 0, 0)
    with pytest.raises(ValueError, match="Symlink"):
        usage.summary(tmp_path)
    assert outside.read_text() == "private"


@pytest.mark.parametrize("bad", ["../private", "<script>", "", "a" * 129, "sk-" + "x" * 30])
def test_bad_usage_identifiers_fail_before_writes(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        snapshot.create_snapshot(tmp_path, session_id=bad)
    assert list(tmp_path.iterdir()) == []


def test_snapshot_records_only_numbers_and_explicit_ids(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("# source text must not enter usage history\n" * 200)
    (tmp_path / "config.py").write_text("api_key='synthetic-secret-for-exclusion'\n")
    snapshot.create_snapshot(tmp_path, session_id="work-session", prompt_id="request-1")
    first = usage.summary(tmp_path)
    handoff = (tmp_path / ".contextpilot/HANDOFF.md").read_bytes()
    assert first["lifetime"]["baseline_tokens"] == (len((tmp_path / "main.py").read_bytes()) + 3) // 4
    assert first["lifetime"]["handoff_tokens"] == (len(handoff) + 3) // 4
    assert "source text" not in json.dumps(first)
    before = {p.name: p.read_bytes() for p in (tmp_path / ".contextpilot").iterdir()}
    snapshot.resume(tmp_path)
    snapshot.status(tmp_path)
    usage.summary(tmp_path)
    assert before == {p.name: p.read_bytes() for p in (tmp_path / ".contextpilot").iterdir()}
    snapshot.create_snapshot(tmp_path, session_id="work-session", prompt_id="request-2")
    assert usage.summary(tmp_path)["lifetime"]["cache_bytes"] > 0


def test_lifetime_retains_old_sessions_beyond_visible_limits(tmp_path: Path) -> None:
    for index in range(103):
        usage.record(tmp_path, 400, 80, 0, "session-" + str(index), "request")
    with sqlite3.connect(tmp_path / usage.DB_NAME) as db:
        db.execute("UPDATE events SET created_at='2000-01-01T00:00:00+00:00' WHERE id=1")
    result = usage.summary(tmp_path)
    assert result["lifetime"]["prompts"] == result["session_count"] == 103
    assert len(result["sessions"]) == 100
    assert sum(day["prompts"] for day in result["daily"]) == 102
    assert result["first_recorded"].startswith("2000-01-01")
    assert usage.summary(tmp_path, "session-0")["selected"]["saved_tokens"] == 80


def test_corrupt_history_preserved_without_losing_snapshot(tmp_path: Path) -> None:
    (tmp_path / ".contextpilot").mkdir()
    ledger = tmp_path / usage.DB_NAME
    ledger.write_bytes(b"not a database")
    result = snapshot.create_snapshot(tmp_path)
    assert "usage_warning" in result
    assert (tmp_path / ".contextpilot/HANDOFF.md").is_file()
    assert ledger.read_bytes() == b"not a database"
    with pytest.raises(sqlite3.DatabaseError):
        usage.summary(tmp_path)


@contextmanager
def running_server(root: Path, session: str | None = None):
    server = pilotboard.make_server(root, session_id=session)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(server, path="/", headers=None, method="GET"):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_local_dashboard_api_auth_host_origin_and_readonly(tmp_path: Path) -> None:
    usage.record(tmp_path, 400, 80, 0, "session-a", "p1")
    usage.record(tmp_path, 800, 80, 0, "session-b", "p2")
    before = {p.name: p.read_bytes() for p in (tmp_path / ".contextpilot").iterdir()}
    with running_server(tmp_path, "session-a") as server:
        auth = {"X-Pilotboard-Key": server.dashboard_url.split("#")[1]}
        code, headers, body = request(server)
        assert code == 200 and b"Pilotboard" in body
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["Cache-Control"] == "no-store"
        assert request(server, "/app.css")[0] == 200
        assert request(server, "/app.js")[0] == 200
        assert request(server, "/api/usage")[0] == 403
        assert request(server, "/api/usage", {"X-Pilotboard-Key": "ÿ"})[0] == 403
        assert request(server, "/api/usage", {**auth, "Host": "evil.example"})[0] == 403
        assert request(server, "/api/usage", {**auth, "Origin": "https://evil.example"})[0] == 403
        assert request(server, "/../scan.py")[0] == 404
        assert request(server, "/api/usage", auth, "POST")[0] == 405
        selected = json.loads(request(server, "/api/usage", auth)[2])
        assert selected["selected"]["saved_tokens"] == 80
        all_sessions = json.loads(request(server, "/api/usage?session=", auth)[2])
        assert all_sessions["selected"]["saved_tokens"] == 260
        for query in ("offset=-1", "session=bad%2Fid", "session=a&session=b", "unknown=1", "offset=no"):
            assert request(server, "/api/usage?" + query, auth)[0] == 400
    assert before == {p.name: p.read_bytes() for p in (tmp_path / ".contextpilot").iterdir()}


def test_empty_dashboard_never_creates_artifacts(tmp_path: Path) -> None:
    with running_server(tmp_path) as server:
        auth = {"X-Pilotboard-Key": server.dashboard_url.split("#")[1]}
        result = json.loads(request(server, "/api/usage", auth)[2])
        assert result["lifetime"]["prompts"] == 0
        assert not result["prompts"]
    assert list(tmp_path.iterdir()) == []


def test_installed_bundle_survives_copy_and_alias_is_discoverable(tmp_path: Path) -> None:
    import shutil
    import subprocess
    import sys
    bundle = Path(snapshot.__file__).resolve().parents[1]
    target = tmp_path / "installed skills/contextpilot"
    shutil.copytree(bundle, target, ignore=shutil.ignore_patterns("__pycache__"))
    result = subprocess.run([sys.executable, str(target / "scripts/pilotboard.py"), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert (target / "assets/pilotboard/app.js").is_file()
    alias = bundle.parent / "pilotboard/SKILL.md"
    shutil.copytree(alias.parent, target.parent / "pilotboard")
    assert "name: pilotboard" in alias.read_text()
    assert "`contextpilot`" in alias.read_text()
    alias_script = target.parent / "pilotboard/../contextpilot/scripts/pilotboard.py"
    assert subprocess.run([sys.executable, str(alias_script), "--help"], capture_output=True).returncode == 0
