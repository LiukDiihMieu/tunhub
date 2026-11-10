import pytest


@pytest.fixture(autouse=True)
def isolated_icon_cache(tmp_path, monkeypatch):
    """Never touch the real per-user icon cache from tests."""
    monkeypatch.setenv("TUNHUB_CACHE_DIR", str(tmp_path / "cache"))


@pytest.fixture(autouse=True)
def fixed_machine_name(monkeypatch):
    """Tests read local.testbox.yaml, whatever this machine is called."""
    monkeypatch.setenv("TUNHUB_MACHINE", "testbox")
