"""The stable interfaces listed in docs/COMPATIBILITY.md, pinned in one place.

A failure here means a change breaks users' configs, bookmarks or login
entries. Do not just update the expected value: follow the deprecation rules
in docs/COMPATIBILITY.md (keep the old form working and warn), or reconsider.
"""

import json
import socket
import subprocess
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


@pytest.mark.parametrize("position", ["before", "after"])
def test_machine_option(position):
    argv = ["--machine", "laptop", "run"] if position == "before" else ["run", "--machine", "laptop"]
    assert build_parser().parse_args(argv).machine == "laptop"


# ---------------------------------------------------------------------------
# Start at login


def test_login_entry_names_and_command(tmp_path):
    assert autostart.SYSTEMD_UNIT == "tunhub.service"
    assert autostart.LAUNCHD_LABEL == "io.github.liukdiihmieu.tunhub"
    assert autostart.WINDOWS_TASK == "TunHub"
    assert autostart.run_command(tmp_path) == [sys.executable, "-m", "tunhub", "run", "--config-dir", str(tmp_path)]
    # That command line keeps working.
    args = build_parser().parse_args(autostart.run_command(tmp_path)[3:])
    assert args.config_dir == tmp_path
    # An explicitly chosen machine name is recorded as well.
    argv = autostart.run_command(tmp_path, "laptop")
    assert argv[-2:] == ["--machine", "laptop"]
    assert build_parser().parse_args(argv[3:]).machine == "laptop"


# ---------------------------------------------------------------------------
# Command line


def test_commands_exist():
    parser = build_parser()
    for argv in (["run"], ["init"], ["service", "install"], ["service", "uninstall"], ["service", "status"], ["forget", "192.0.2.1"]):
        assert parser.parse_args(argv).handler is not None, argv


# ---------------------------------------------------------------------------
# More of the configuration contract


def test_documented_keys_stay_known():
    """Removing or renaming a key breaks configs; adding keys is fine."""
    from tunhub import config

    assert set(config.TOP_KEYS) >= {"requires_tunhub", "hub", "tunnel", "services"}
    assert set(config.HUB_KEYS) >= {"listen", "port", "hostname", "open_timeout"}
    assert set(config.TUNNEL_KEYS) >= {
        "ssh_command", "ssh_options", "probe_timeout", "ready_timeout", "health_timeout",
        "auto_direct_interval", "auto_direct",
    }
    assert set(config.SERVICE_KEYS) >= {
        "name", "description", "type", "local_port", "hostname", "path", "icon", "routes",
        "autostart", "health_check", "auto_direct", "enabled",
    }
    assert set(config.DIRECT_ROUTE_KEYS) >= {"direct", "label"}
    assert set(config.SSH_ROUTE_KEYS) >= {"ssh", "remote", "label"}
    assert set(config.SERVICE_TYPES) >= {"http", "https", "tcp"}


def test_unknown_keys_warn_at_every_level():
    raw = {
        "top_new": 1,
        "hub": {"hub_new": 1},
        "tunnel": {"tunnel_new": 1},
        "services": {
            "a": {
                "local_port": 15000,
                "service_new": 1,
                "routes": [{"direct": "192.0.2.10:1", "route_new": 1}, {"ssh": "x", "remote": 1, "route_new": 1}],
            }
        },
    }
    assert len(parse_config(raw).warnings) == 6


def test_endpoint_formats():
    from tunhub.config import parse_endpoint

    assert parse_endpoint("192.0.2.10:5000", "t") == ("192.0.2.10", 5000)
    assert parse_endpoint("nas.lan:5000", "t") == ("nas.lan", 5000)
    assert parse_endpoint("[2001:db8::1]:5000", "t") == ("2001:db8::1", 5000)
    assert parse_endpoint(5000, "t", default_host="127.0.0.1") == ("127.0.0.1", 5000)  # bare port for `remote`


def test_ip_versions_of_routes():
    from tunhub.config import ConfigError

    ssh = {"local_port": 15000, "routes": [{"ssh": "x", "remote": "[2001:db8::1]:5000"}]}
    assert parse_config({"services": {"a": ssh}}).services["a"].routes[0].target_host == "2001:db8::1"
    direct = {"local_port": 15000, "routes": [{"direct": "[2001:db8::1]:5000"}]}
    with pytest.raises(ConfigError):
        parse_config({"services": {"a": direct}})


def test_required_version_not_met_is_an_error():
    from tunhub.config import ConfigError

    with pytest.raises(ConfigError, match="please upgrade"):
        parse_config({"requires_tunhub": ">=999"})


@pytest.mark.parametrize(("listen", "ok"), [("127.0.0.1", True), ("::1", True), ("0.0.0.0", False), ("192.0.2.1", False)])
def test_listen_is_loopback_only(listen, ok):
    from tunhub.config import ConfigError

    if ok:
        assert parse_config({"hub": {"listen": listen}}).hub.listen_host == listen
    else:
        with pytest.raises(ConfigError):
            parse_config({"hub": {"listen": listen}})


@pytest.mark.parametrize(
    ("service_id", "ok"), [("nas", True), ("my-nas-2", True), ("x" * 63, True), ("x" * 64, False), ("-nas", False), ("Nas", False)]
)
def test_service_id_rule(service_id, ok):
    from tunhub.config import ConfigError

    raw = {"services": {service_id: {"local_port": 15000, "routes": [{"ssh": "x", "remote": 1}]}}}
    if ok:
        assert service_id in parse_config(raw).services
    else:
        with pytest.raises(ConfigError):
            parse_config(raw)


# ---------------------------------------------------------------------------
# /open redirects


@pytest.fixture
def reachable(tmp_path):
    """Two services whose direct route reaches a listening socket."""
    target = socket.create_server(("127.0.0.1", 0))
    port = target.getsockname()[1]
    hub_port, web_port, tcp_port = free_port(), free_port(), free_port()
    (tmp_path / "services.yaml").write_text(
        f"hub: {{port: {hub_port}}}\n"
        "services:\n"
        f"  nas: {{local_port: {web_port}, health_check: false, routes: [{{direct: '127.0.0.1:{port}'}}]}}\n"
        f"  rdp: {{local_port: {tcp_port}, type: tcp, routes: [{{direct: '127.0.0.1:{port}'}}]}}\n"
    )
    client = TestClient(create_app(Hub(tmp_path)), base_url=f"http://hub.localhost:{hub_port}")
    with client:
        yield client, web_port
    target.close()


def test_open_redirects_web_service_to_its_address(reachable):
    client, web_port = reachable
    response = client.get("/open/nas", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == f"http://nas.localhost:{web_port}/"


def test_open_redirects_tcp_service_to_the_dashboard(reachable):
    client, _ = reachable
    response = client.get("/open/rdp", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/#rdp"


# ---------------------------------------------------------------------------
# More of the command line contract


def test_no_command_means_run():
    from tunhub.cli import cmd_run

    assert build_parser().parse_args([]).handler is cmd_run


def test_python_dash_m():
    result = subprocess.run([sys.executable, "-m", "tunhub", "--version"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and result.stdout.startswith("tunhub ")


def test_exit_codes(tmp_path, monkeypatch, capsys):
    from tunhub.cli import main

    assert main(["init", "--config-dir", str(tmp_path)]) == 0
    assert main(["init", "--config-dir", str(tmp_path)]) == 1  # could not be done: file exists
    assert main(["forget", "not-an-ip", "--config-dir", str(tmp_path)]) == 2
    with pytest.raises(SystemExit) as exc:
        main(["no-such-command"])
    assert exc.value.code == 2

    # A config error that leaves nowhere to listen (other errors are shown on the dashboard).
    (tmp_path / "services.yaml").write_text("hub: {port: 0}\n")
    assert main(["run", "--config-dir", str(tmp_path)]) == 2

    monkeypatch.setattr(autostart, "status", lambda: "")
    monkeypatch.setattr(autostart, "installed", lambda: True)
    assert main(["service", "status"]) == 0
    monkeypatch.setattr(autostart, "installed", lambda: False)
    assert main(["service", "status"]) == 1
