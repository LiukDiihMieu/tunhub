"""HTTP layer tests: host guard, CSRF guard, /open redirect, reload."""

import json
import socket
import sys
import threading

import pytest
from fastapi.testclient import TestClient

from tunhub.app import create_app
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
        assert hub.get("web").mode is not Mode.MANUAL  # the request started nothing
