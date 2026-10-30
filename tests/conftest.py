import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def amperes_home(tmp_path, monkeypatch):
    """Every test gets an isolated data directory; nothing touches ./amperes_data."""
    monkeypatch.setenv("AMPERES_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def run_dir(amperes_home):
    d = amperes_home / "artifacts" / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d
