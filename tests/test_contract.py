"""The stable interfaces listed in docs/COMPATIBILITY.md, pinned in one place.

A failure here means a change breaks users' configs, bookmarks or login
entries. Do not just update the expected value: follow the deprecation rules
in docs/COMPATIBILITY.md (keep the old form working and warn), or reconsider.
"""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tunhub import autostart, paths
from tunhub.app import create_app
from tunhub.cli import build_parser
from tunhub.config import load_config, parse_config
from tunhub.hub import Hub
from tunhub.known_macs import FILE_NAME, KnownMacs

from .helpers import free_port


# ---------------------------------------------------------------------------
# Files and locations


def test_file_names():
    assert FILE_NAME == "known_macs"
    from tunhub.config import SHARED_FILE, local_file_name

    assert SHARED_FILE == "services.yaml"
    assert local_file_name("laptop") == "local.laptop.yaml"


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("linux", {}, ".config/tunhub"),
        ("linux", {"XDG_CONFIG_HOME": "{home}/xdg"}, "xdg/tunhub"),
        ("darwin", {}, ".config/tunhub"),
        ("win32", {"APPDATA": "{home}/AppData/Roaming"}, "AppData/Roaming/tunhub"),
        ("linux", {"TUNHUB_CONFIG_DIR": "{home}/elsewhere"}, "elsewhere"),
    ],
)
def test_config_directory(monkeypatch, tmp_path, platform, env, expected):
    for name in ("TUNHUB_CONFIG_DIR", "XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value.format(home=tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(paths.sys, "platform", platform)
    assert paths.default_config_dir() == tmp_path / expected


def test_machine_name_rule(monkeypatch):
    monkeypatch.setenv("TUNHUB_MACHINE", "Work-Laptop.example.org")
    assert paths.machine_name() == "work-laptop"


@pytest.mark.parametrize("position", ["before", "after"])
def test_config_dir_option(tmp_path, position):
    argv = ["--config-dir", str(tmp_path), "run"] if position == "before" else ["run", "--config-dir", str(tmp_path)]
    assert build_parser().parse_args(argv).config_dir == tmp_path


# ---------------------------------------------------------------------------
# Configuration


def test_defaults_that_decide_addresses():
    config = parse_config({"services": {"nas": {"local_port": 15000, "routes": [{"ssh": "nas", "remote": 5000}]}}})
    assert (config.hub.hostname, config.hub.port, config.hub.listen_host) == ("hub.localhost", 1110, "127.0.0.1")
    nas = config.services["nas"]
    assert nas.url == "http://nas.localhost:15000/"
    route = nas.routes[0]
    assert (route.ssh_host, route.target_host, route.target_port) == ("nas", "127.0.0.1", 5000)


def test_local_file_merge_rule(tmp_path):
    (tmp_path / "services.yaml").write_text(
        "services:\n  nas:\n    name: NAS\n    local_port: 15000\n"
        "    routes: [{direct: '192.0.2.10:5000'}, {ssh: nas, remote: 5000}]\n"
    )
    (tmp_path / "local.laptop.yaml").write_text("services:\n  nas: {routes: [{ssh: other, remote: 5000}]}\n")
    nas = load_config(tmp_path, machine="laptop").services["nas"]
    assert nas.name == "NAS"  # mappings merge key by key
    assert [route.ssh_host for route in nas.routes] == ["other"]  # lists replace


def test_null_means_not_set():
    assert parse_config({"hub": {"port": None}}).hub.port == 1110


def test_unknown_keys_are_warnings_not_errors():
    assert parse_config({"a_future_key": 1}).warnings


def test_requires_tunhub_is_understood():
    assert parse_config({"requires_tunhub": ">=0.1"}).warnings == ()


# ---------------------------------------------------------------------------
# Addresses


@pytest.fixture
def dashboard(tmp_path):
    hub_port, local_port = free_port(), free_port()
    (tmp_path / "services.yaml").write_text(
        f"hub: {{port: {hub_port}}}\n"
        f"tunnel: {{ssh_command: {json.dumps([sys.executable, '-c', 'import time; time.sleep(60)'])}, ready_timeout: 0.5}}\n"
        f"services:\n  nas: {{local_port: {local_port}, routes: [{{ssh: nas, remote: 5000}}]}}\n"
    )
    client = TestClient(create_app(Hub(tmp_path)), base_url=f"http://hub.localhost:{hub_port}")
    with client:
        yield client, hub_port


def test_dashboard_host_names(dashboard):
    client, port = dashboard
    for host in ("hub.localhost", "localhost", "127.0.0.1", "[::1]"):
        assert client.get("/", headers={"Host": f"{host}:{port}"}).status_code == 200, host


def test_open_url_exists(dashboard):
    client, _ = dashboard
    # The service cannot connect here; the bookmark URL itself must be served.
    assert client.get("/open/nas", follow_redirects=False).status_code != 404
    assert client.get("/open/missing", follow_redirects=False).status_code == 404


# ---------------------------------------------------------------------------
# known_macs


def test_known_macs_format(tmp_path):
    path = tmp_path / FILE_NAME
    path.write_text(
        "# comment\n"
        "192.0.2.10 aa:bb:cc:dd:ee:01\n"
        "192.0.2.10 AA-BB-CC-DD-EE-02 net=home  # extra fields are ignored\n"
        "@revoked 192.0.2.10 aa:bb:cc:dd:ee:03\n"
    )
    known = KnownMacs(path)
    assert [entry.mac for entry in known.lookup("192.0.2.10")] == ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]

    known.add("192.0.2.20", "aa:bb:cc:dd:ee:04")
    assert path.read_text().endswith("192.0.2.20 aa:bb:cc:dd:ee:04\n")

    assert known.forget("192.0.2.10") == 2
    assert "@revoked 192.0.2.10 aa:bb:cc:dd:ee:03" in path.read_text()


# ---------------------------------------------------------------------------
# Start at login


def test_login_entry_names_and_command(tmp_path):
    assert autostart.SYSTEMD_UNIT == "tunhub.service"
    assert autostart.LAUNCHD_LABEL == "io.github.liukdiihmieu.tunhub"
    assert autostart.WINDOWS_TASK == "TunHub"
    assert autostart.run_command(tmp_path) == [sys.executable, "-m", "tunhub", "run", "--config-dir", str(tmp_path.resolve())]
    # That command line keeps working.
    args = build_parser().parse_args(autostart.run_command(tmp_path)[3:])
    assert args.config_dir == tmp_path.resolve()


# ---------------------------------------------------------------------------
# Command line


def test_commands_exist():
    parser = build_parser()
    for argv in (["run"], ["init"], ["service", "install"], ["service", "uninstall"], ["service", "status"], ["forget", "192.0.2.1"]):
        assert parser.parse_args(argv).handler is not None, argv
