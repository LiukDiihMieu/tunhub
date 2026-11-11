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


# ---------------------------------------------------------------------------
# Machine name: picks local.<machine>.yaml


@pytest.fixture
def os_name(monkeypatch):
    """Let the OS report a given host name (no TUNHUB_MACHINE)."""
    from tunhub import paths

    monkeypatch.delenv("TUNHUB_MACHINE", raising=False)
    paths._os_machine_name.cache_clear()
    yield lambda name: monkeypatch.setattr(paths.socket, "gethostname", lambda: name)
    paths._os_machine_name.cache_clear()


@pytest.mark.parametrize(
    ("host", "machine"),
    [("Laptop", "laptop"), ("DESKTOP-AB12CD", "desktop-ab12cd"), ("nas.lan", "nas"), ("ws.lab.example.org", "ws")],
)
def test_machine_name_from_host_name(os_name, monkeypatch, host, machine):
    monkeypatch.setattr("sys.platform", "linux")
    os_name(host)
    assert paths.machine_name() == machine


def test_machine_name_on_macos_uses_local_host_name(os_name, monkeypatch):
    import subprocess

    monkeypatch.setattr("sys.platform", "darwin")
    os_name("ignored.local")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="Example-MacBook\n")

    monkeypatch.setattr(paths.subprocess, "run", fake_run)
    assert paths.machine_name() == "example-macbook"
    assert calls == [["scutil", "--get", "LocalHostName"]]


def test_machine_name_on_macos_falls_back_to_host_name(os_name, monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    os_name("Mac.local")

    def broken_run(command, **kwargs):
        raise OSError("no scutil")

    monkeypatch.setattr(paths.subprocess, "run", broken_run)
    assert paths.machine_name() == "mac"


def test_machine_name_from_environment(monkeypatch):
    monkeypatch.setenv("TUNHUB_MACHINE", " Work-Laptop ")
    assert paths.machine_name() == "work-laptop"


def test_state_dir_per_platform(clean_env, monkeypatch):
    monkeypatch.delenv("TUNHUB_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(paths.sys, "platform", "linux")
    assert paths.state_dir() == clean_env / ".local" / "state" / "tunhub"
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
    assert paths.state_dir() == Path("/xdg/state/tunhub")
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    assert paths.state_dir() == clean_env / "Library" / "Application Support" / "tunhub"
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(clean_env / "Local"))
    assert paths.state_dir() == clean_env / "Local" / "tunhub"
    monkeypatch.setenv("TUNHUB_STATE_DIR", "/elsewhere")
    assert paths.state_dir() == Path("/elsewhere")
