from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".claude/skills/contextpilot/scripts"))


@pytest.fixture(autouse=True)
def offline_update_checks(monkeypatch):
    # Ordinary regression tests never contact GitHub, including copied CLI tests.
    monkeypatch.setenv("CONTEXTPILOT_UPDATE_CHECK","0")
