"""HTTP layer tests: host guard, CSRF guard, /open redirect, reload."""

import json
import socket
import sys
import threading

import pytest
from fastapi.testclient import TestClient

from tunhub.app import create_app
from tunhub.config import ConfigError
from tunhub.hub import Hub
from tunhub.service import Mode

from .helpers import FAKE_SSH, free_port


@pytest.fixture
def http_target():
    """A tiny blocking HTTP server in a thread (independent of the app's loop)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    sock.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except OSError:
                continue
            with conn:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield sock.getsockname()[1]
    stop.set()
    thread.join()
    sock.close()


@pytest.fixture
def setup(tmp_path, http_target):
    hub_port = free_port()
    local_port = free_port()

    def write_config(extra: str = "") -> None:
        (tmp_path / "services.yaml").write_text(
            f"""
hub: {{port: {hub_port}}}
tunnel: {{ssh_command: {json.dumps([sys.executable, FAKE_SSH])}, probe_timeout: 0.5}}
services:
  web:
    name: Web
    local_port: {local_port}
    routes: [{{direct: "127.0.0.1:{http_target}"}}]
{extra}"""
        )

    write_config()
    hub = Hub(tmp_path)
    client = TestClient(create_app(hub), base_url=f"http://hub.localhost:{hub_port}", follow_redirects=False)
    with client:
        yield client, hub, local_port, write_config


def test_rejects_foreign_host(setup):
    client, *_ = setup
    response = client.get("/api/state", headers={"Host": "evil.example:80"})
    assert response.status_code == 403


def test_post_requires_custom_header(setup):
    client, *_ = setup
    assert client.post("/api/services/web/start").status_code == 403
    assert client.post(
        "/api/services/web/start", headers={"X-Tunhub": "1", "Origin": "http://evil.example"}
    ).status_code == 403
    assert client.post("/api/services/web/start", headers={"X-Tunhub": "1"}).status_code == 200


def test_open_redirects_to_fixed_local_hostname(setup):
    client, hub, local_port, _ = setup
    response = client.get("/open/web")
    assert response.status_code == 302
    assert response.headers["location"] == f"http://web.localhost:{local_port}/"
    state = client.get("/api/state").json()["services"][0]
    assert state["state"] == "up" and state["active_route"] == 0


def test_open_unknown_service(setup):
    client, *_ = setup
    assert client.get("/open/nope").status_code == 404


def test_reload_adds_service_and_keeps_running_one(setup):
    client, hub, _, write_config = setup
    client.get("/open/web")
    running = hub.get("web")

    write_config(
        f"""  rdp:
    type: tcp
    local_port: {free_port()}
    routes: [{{ssh: lab, remote: 3390}}]
"""
    )
    response = client.post("/api/reload", headers={"X-Tunhub": "1"})
    assert response.json()["messages"] == ["added rdp"]
    assert hub.get("web") is running and running.desired

    (hub.config_dir / "services.yaml").write_text("services: [broken")
    response = client.post("/api/reload", headers={"X-Tunhub": "1"})
    assert response.status_code == 400
    assert hub.get("rdp") is not None  # old config kept


def test_reload_rejects_service_on_the_running_dashboard_port(setup):
    client, hub, local_port, _ = setup
    running_port = hub.hub_settings.port
    # Moving the hub elsewhere only takes effect after a restart, so the
    # dashboard still owns its old port and no service may take it.
    (hub.config_dir / "services.yaml").write_text(
        f"""
hub: {{port: {free_port()}}}
services:
  web:
    local_port: {local_port}
    routes: [{{direct: "127.0.0.1:1"}}]
  intruder:
    type: tcp
    local_port: {running_port}
    routes: [{{direct: "127.0.0.1:1"}}]
"""
    )
    response = client.post("/api/reload", headers={"X-Tunhub": "1"})
    assert response.status_code == 400
    assert "running dashboard" in response.json()["error"]
    assert hub.get("intruder") is None  # old config kept


def test_icon_endpoint_serves_cached_icon_safely(setup, tmp_path):
    client, hub, _, _ = setup
    assert client.get("/api/icons/web").status_code == 404
    assert client.get("/api/state").json()["services"][0]["icon_version"] is None

    hub.icons.cache_dir.mkdir(parents=True, exist_ok=True)
    (hub.icons.cache_dir / "web.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    response = client.get("/api/icons/web")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in response.headers["content-security-policy"]
    assert client.get("/api/state").json()["services"][0]["icon_version"] is not None
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_pages_refuse_to_be_framed(setup):
    client, *_ = setup
    response = client.get("/")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"


CROSS_SITE_IMG = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"}
CROSS_SITE_CLICK = {
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-User": "?1",
}


@pytest.mark.parametrize(
    "headers, allowed",
    [
        ({}, True),  # curl and other non-browser clients
        ({"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}, True),  # bookmark
        ({"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}, True),
        (CROSS_SITE_CLICK, True),  # the user clicked a link on another page
        (CROSS_SITE_IMG, False),  # <img src=".../open/web">
        ({**CROSS_SITE_CLICK, "Sec-Fetch-User": None}, False),  # scripted redirect, no user click
        ({**CROSS_SITE_CLICK, "Sec-Fetch-Dest": "iframe"}, False),
        ({**CROSS_SITE_IMG, "Sec-Fetch-Site": "same-site"}, False),  # e.g. from another *.localhost service
    ],
)
def test_open_rejects_requests_from_other_pages(setup, headers, allowed):
    client, hub, *_ = setup
    headers = {k: v for k, v in headers.items() if v is not None}
    response = client.get("/open/web", headers=headers)
    if allowed:
        assert response.status_code == 302
    else:
        assert response.status_code == 403
        assert hub.get("web").mode is not Mode.ON  # the request started nothing


def test_dashboard_starts_without_config_and_picks_it_up_on_reload(tmp_path):
    hub = Hub(tmp_path)  # no services.yaml yet
    client = TestClient(create_app(hub), base_url="http://hub.localhost:1110", follow_redirects=False)
    with client:
        state = client.get("/api/state").json()
        assert state["services"] == []
        assert state["config"]["found"] is False
        assert state["config"]["path"] == str(tmp_path / "services.yaml")
        assert state["config"]["init_command"] == f'tunhub init --config-dir "{tmp_path}"'

        (tmp_path / "services.yaml").write_text(
            f"services:\n  web:\n    local_port: {free_port()}\n    routes: [{{direct: '127.0.0.1:1'}}]\n"
        )
        assert client.post("/api/reload", headers={"X-Tunhub": "1"}).json()["messages"] == ["added web"]
        state = client.get("/api/state").json()
        assert state["config"]["found"] is True and [s["id"] for s in state["services"]] == ["web"]


def test_init_command_is_plain_for_the_default_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("tunhub.app.default_config_dir", lambda: tmp_path)
    client = TestClient(create_app(Hub(tmp_path)), base_url="http://hub.localhost:1110")
    with client:
        assert client.get("/api/state").json()["config"]["init_command"] == "tunhub init"


def test_reload_keeps_the_old_config_when_the_file_disappears(setup):
    # A moved/renamed services.yaml (or an editor's save in progress) must not
    # drop every running service; only a first start treats "missing" as empty.
    client, hub, *_ = setup
    client.get("/open/web")
    running = hub.get("web")
    (hub.config_dir / "services.yaml").rename(hub.config_dir / "services.yaml.bak")
    response = client.post("/api/reload", headers={"X-Tunhub": "1"})
    assert response.status_code == 400
    assert "not found" in response.json()["error"]
    assert hub.get("web") is running and running.desired


def test_state_reports_the_running_version(setup):
    from tunhub import __version__

    client, *_ = setup
    assert client.get("/api/state").json()["version"] == __version__


def test_host_names_outside_localhost_are_resolved_once(tmp_path):
    import asyncio

    (tmp_path / "services.yaml").write_text(
        "hub: {hostname: tunhub.lan}\n"
        "services:\n"
        "  ok: {local_port: 15000, hostname: ok.lan, routes: [{ssh: nas, remote: 1}]}\n"
        "  far: {local_port: 15001, hostname: far.lan, routes: [{ssh: nas, remote: 1}]}\n"
        "  gone: {local_port: 15002, hostname: gone.lan, routes: [{ssh: nas, remote: 1}]}\n"
        "  slow: {local_port: 15003, hostname: slow.lan, routes: [{ssh: nas, remote: 1}]}\n"
        "  plain: {local_port: 15004, routes: [{ssh: nas, remote: 1}]}\n"
        "  ip: {local_port: 15005, hostname: 127.0.0.1, routes: [{ssh: nas, remote: 1}]}\n"
        "  rdp: {local_port: 15006, type: tcp, hostname: rdp.lan, routes: [{ssh: nas, remote: 1}]}\n"
    )
    asked = []

    async def resolve(name):
        asked.append(name)
        if name == "slow.lan":
            await asyncio.sleep(10)
        if name == "gone.lan":
            raise OSError("unknown host")
        return {"tunhub.lan": ["127.0.0.1", "::1"], "ok.lan": ["127.0.0.1"], "far.lan": ["192.168.1.10"]}[name]

    async def scenario():
        hub = Hub(tmp_path, tmp_path / "cache", resolve=resolve)
        warnings = await hub.check_hostnames(hub.config)
        assert hub.warnings == warnings
        return warnings

    from tunhub import hub as hub_module

    original = hub_module.RESOLVE_TIMEOUT
    hub_module.RESOLVE_TIMEOUT = 0.2
    try:
        warnings = asyncio.run(scenario())
    finally:
        hub_module.RESOLVE_TIMEOUT = original
    assert sorted(asked) == ["far.lan", "gone.lan", "ok.lan", "slow.lan", "tunhub.lan"]
    assert len(warnings) == 3
    assert "far.lan (service 'far') resolves to 192.168.1.10" in warnings[0]
    assert "gone.lan (service 'gone') does not resolve" in warnings[1]
    assert "slow.lan (service 'slow') does not resolve" in warnings[2]


def test_state_lists_config_warnings(setup):
    client, *_ = setup
    warnings = client.get("/api/state").json()["config"]["warnings"]
    # The fixture's direct route to 127.0.0.1 sits in services.yaml.
    assert len(warnings) == 1 and "connects directly to 127.0.0.1" in warnings[0]


def test_state_names_the_machine_and_its_local_file(setup):
    client, hub, *_ = setup
    config = client.get("/api/state").json()
    assert config["machine"] == "testbox"
    assert config["config"]["local_path"] == str(hub.config_dir / "local.testbox.yaml")
    assert config["config"]["local_found"] is False


# ---------------------------------------------------------------------------
# Config errors are shown on the dashboard


def test_invalid_config_still_starts_and_shows_the_error(tmp_path):
    port = free_port()
    (tmp_path / "services.yaml").write_text(f"requires_tunhub: '>=999'\nhub: {{port: {port}}}\n")
    hub = Hub(tmp_path)
    assert hub.hub_settings.port == port  # the hub: section still decides where to listen
    client = TestClient(create_app(hub), base_url=f"http://hub.localhost:{port}", follow_redirects=False)
    with client:
        state = client.get("/api/state").json()
        assert state["services"] == []
        assert "please upgrade" in state["config"]["error"]
        assert state["config"]["error_keeps_previous"] is False

        page = client.get("/open/nas")
        assert page.status_code == 503 and "please upgrade" in page.text

        (tmp_path / "services.yaml").write_text(
            f"hub: {{port: {port}}}\nservices:\n  web: {{local_port: {free_port()}, routes: [{{ssh: x, remote: 1}}]}}\n"
        )
        assert client.post("/api/reload", headers={"X-Tunhub": "1"}).status_code == 200
        state = client.get("/api/state").json()
        assert state["config"]["error"] is None and [s["id"] for s in state["services"]] == ["web"]


def test_failed_reload_keeps_the_old_config_and_shows_the_error(setup):
    client, hub, _, write_config = setup
    write_config("    bad_key_level: [")  # not YAML
    assert client.post("/api/reload", headers={"X-Tunhub": "1"}).status_code == 400
    config = client.get("/api/state").json()["config"]
    assert config["error"] and config["error_keeps_previous"] is True
    assert hub.get("web") is not None

    write_config()
    assert client.post("/api/reload", headers={"X-Tunhub": "1"}).status_code == 200
    assert client.get("/api/state").json()["config"]["error"] is None


def test_unreadable_files_use_the_last_working_dashboard_address(tmp_path):
    port = free_port()
    (tmp_path / "services.yaml").write_text(f"hub: {{port: {port}, hostname: tunhub.localhost}}\n")
    Hub(tmp_path)  # a working start remembers where the dashboard listens

    (tmp_path / "services.yaml").write_text("hub: {port: [\n")  # not YAML
    hub = Hub(tmp_path)
    assert (hub.hub_settings.port, hub.hub_settings.hostname) == (port, "tunhub.localhost")
    assert hub.config_error and not hub.config_loaded


def test_unreadable_files_without_a_remembered_address_cannot_start(tmp_path):
    (tmp_path / "services.yaml").write_text("hub: {port: [\n")
    with pytest.raises(ConfigError):
        Hub(tmp_path)
