from pathlib import Path

import pytest

from tunhub import paths


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in ("TUNHUB_CONFIG_DIR", "TUNHUB_CACHE_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def test_linux_defaults_and_xdg(clean_env, monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    assert paths.default_config_dir() == clean_env / ".config" / "tunhub"
    assert paths.icon_cache_dir() == clean_env / ".cache" / "tunhub" / "icons"
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg/config")
    assert paths.default_config_dir() == Path("/xdg/config/tunhub")


def test_macos_defaults(clean_env, monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    assert paths.default_config_dir() == clean_env / ".config" / "tunhub"
    assert paths.icon_cache_dir() == clean_env / "Library" / "Caches" / "tunhub" / "icons"
    assert paths.default_log_file() == clean_env / "Library" / "Logs" / "tunhub.log"


def test_windows_defaults(clean_env, monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", "/appdata")
    monkeypatch.setenv("LOCALAPPDATA", "/localappdata")
    assert paths.default_config_dir() == Path("/appdata/tunhub")
    assert paths.default_log_file() == Path("/localappdata/tunhub/tunhub.log")


def test_env_overrides_win_over_defaults(clean_env, monkeypatch):
    monkeypatch.setenv("TUNHUB_CONFIG_DIR", "/custom/config")
    monkeypatch.setenv("TUNHUB_CACHE_DIR", "/custom/cache")
    assert paths.default_config_dir() == Path("/custom/config")
    assert paths.icon_cache_dir() == Path("/custom/cache/icons")
