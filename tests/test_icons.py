"""Icon discovery: candidate parsing, fetching through the local port, caching."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tunhub.config import parse_config
from tunhub.icons import IconStore, discover_icon, parse_html_candidates, parse_manifest_candidates, sniff_image

PNG_SMALL = b"\x89PNG\r\n\x1a\n" + b"small"
PNG_BIG = b"\x89PNG\r\n\x1a\n" + b"big"
ICO = b"\x00\x00\x01\x00" + b"ico"


def test_parse_html_candidates():
    html = """<html><head>
      <link rel="shortcut icon" href="/favicon-16.png">
      <link rel="apple-touch-icon" href="touch.png">
      <link rel="icon" type="image/png" sizes="96x96" href="/i96.png">
      <link rel="manifest" href="/site.webmanifest">
    </head></html>"""
    candidates, manifest = parse_html_candidates(html, "http://dsm.localhost:15000/web/")
    scores = {c.url: c.score for c in candidates}
    assert scores == {
        "http://dsm.localhost:15000/favicon-16.png": 32,
        "http://dsm.localhost:15000/web/touch.png": 180,
        "http://dsm.localhost:15000/i96.png": 96,
    }
    assert manifest == "http://dsm.localhost:15000/site.webmanifest"


def test_parse_manifest_candidates():
    text = json.dumps({"icons": [{"src": "a.png", "sizes": "192x192"}, {"src": "b.svg", "sizes": "any"}, {}]})
    candidates = parse_manifest_candidates(text, "http://x.localhost:1/m/manifest.json")
    assert [(c.url, c.score) for c in candidates] == [
        ("http://x.localhost:1/m/a.png", 192),
        ("http://x.localhost:1/m/b.svg", 512),
    ]
    assert parse_manifest_candidates("not json", "http://x/") == []


@pytest.mark.parametrize(
    "data, ext",
    [(PNG_BIG, "png"), (ICO, "ico"), (b"  <svg xmlns='...'>", "svg"), (b"<html>login</html>", None)],
)
def test_sniff_image(data, ext):
    assert sniff_image(data) == ext


@pytest.fixture
def fake_web_service():
    """Serves a login redirect, a page with icon links and a manifest."""
    routes = {
        "/": (302, {"Location": "/login"}, b""),
        "/login": (200, {}, b'<link rel="icon" href="/small.png"><link rel="manifest" href="/manifest.json">'),
        "/manifest.json": (200, {}, json.dumps({"icons": [{"src": "/big.png", "sizes": "192x192"}]}).encode()),
        "/big.png": (200, {}, PNG_BIG),
        "/small.png": (200, {}, PNG_SMALL),
    }
    seen_hosts = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen_hosts.append(self.headers["Host"])
            status, headers, body = routes.get(self.path, (404, {}, b"<html>not found</html>"))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], routes, seen_hosts
    server.shutdown()


def web_service(port: int, **extra):
    raw = {"local_port": port, "routes": [{"direct": "10.0.0.1:80"}], **extra}
    return parse_config({"services": {"dsm": raw}}).services["dsm"]


def test_discover_prefers_largest_and_uses_service_host(fake_web_service):
    port, _, seen_hosts = fake_web_service
    data, ext = discover_icon(web_service(port))
    assert (data, ext) == (PNG_BIG, "png")
    assert set(seen_hosts) == {f"dsm.localhost:{port}"}


def test_discover_falls_back_to_favicon(fake_web_service):
    port, routes, _ = fake_web_service
    routes["/login"] = (200, {}, b"<html>no links</html>")
    routes["/favicon.ico"] = (200, {}, ICO)
    assert discover_icon(web_service(port)) == (ICO, "ico")


def test_discover_ignores_html_pretending_to_be_icon(fake_web_service):
    port, routes, _ = fake_web_service
    routes["/login"] = (200, {}, b"<html>no links</html>")
    with pytest.raises(LookupError):
        discover_icon(web_service(port))  # /favicon.ico -> HTML 404 page


def test_store_caches_and_prefers_configured_icon(fake_web_service, tmp_path):
    port, _, _ = fake_web_service
    store = IconStore(tmp_path / "config", tmp_path / "cache")
    service = web_service(port)

    asyncio.run(store.ensure(service))
    assert store.lookup(service).read_bytes() == PNG_BIG
    assert store.version(service) is not None

    (tmp_path / "config" / "icons").mkdir(parents=True)
    (tmp_path / "config" / "icons" / "mine.svg").write_text("<svg/>")
    configured = web_service(port, icon="icons/mine.svg")
    assert store.lookup(configured).name == "mine.svg"


def test_store_remembers_failures(tmp_path):
    store = IconStore(tmp_path, tmp_path / "cache")
    service = web_service(1)  # nothing listens on port 1
    asyncio.run(store.ensure(service))
    assert store.lookup(service) is None and "dsm" in store._failed_at
