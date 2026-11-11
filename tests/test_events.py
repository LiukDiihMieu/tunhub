import logging
import time

from fastapi.testclient import TestClient

from tunhub.app import create_app
from tunhub.events import EventLog, event_log
from tunhub.hub import Hub
from tunhub.nexthop import NetworkFingerprint

from .helpers import free_port


def test_event_log_keeps_the_latest_records():
    log = EventLog(maxlen=2)
    logger = logging.getLogger("tunhub.test-events")
    logger.addHandler(log)
    logger.setLevel(logging.INFO)
    try:
        for message in ("one", "two", "three"):
            logger.info(message)
        logger.warning("careful %s", "now")
    finally:
        logger.removeHandler(log)
    assert [(level, message) for _, level, message in log.entries] == [("info", "three"), ("warning", "careful now")]


def test_event_log_sees_info_from_tunhub_even_if_the_root_logger_is_quiet():
    log = event_log()
    logging.getLogger("tunhub").info("hello from a test")
    assert log.entries[-1][1:] == ("info", "hello from a test")


def test_events_endpoint_reports_network_and_events(tmp_path):
    port = free_port()
    (tmp_path / "services.yaml").write_text(f"hub: {{port: {port}}}\n")
    home = NetworkFingerprint(gateway_ip="192.0.2.1", gateway_mac="aa:bb:cc:dd:ee:01", local_subnet="192.0.2.0/24")
    hub = Hub(tmp_path, tmp_path / "cache", fingerprint=lambda: home)
    client = TestClient(create_app(hub), base_url=f"http://hub.localhost:{port}")
    with client:
        deadline = time.monotonic() + 5
        while hub.network_since is None and time.monotonic() < deadline:
            time.sleep(0.05)
        client.post("/api/reload", headers={"X-Tunhub": "1"})
        data = client.get("/api/events").json()
    assert data["network"] == {"gateway": "192.0.2.1", "gateway_mac": "aa:bb:cc:dd:ee:01", "subnet": "192.0.2.0/24"}
    assert data["network_since"] is not None
    assert any(e["message"] == "reload: no changes" for e in data["events"])


def test_unknown_network_is_reported_as_such(tmp_path):
    port = free_port()
    (tmp_path / "services.yaml").write_text(f"hub: {{port: {port}}}\n")
    hub = Hub(tmp_path, tmp_path / "cache", fingerprint=lambda: None)
    client = TestClient(create_app(hub), base_url=f"http://hub.localhost:{port}")
    with client:
        deadline = time.monotonic() + 5
        while hub.network_since is None and time.monotonic() < deadline:
            time.sleep(0.05)
        data = client.get("/api/events").json()
    assert data["network"] is None and data["network_since"] is not None


def test_state_names_the_config_directory(tmp_path):
    port = free_port()
    (tmp_path / "services.yaml").write_text(f"hub: {{port: {port}}}\n")
    client = TestClient(create_app(Hub(tmp_path)), base_url=f"http://hub.localhost:{port}")
    with client:
        assert client.get("/api/state").json()["config"]["dir"] == str(tmp_path)
