"""Service icons: discovered from the web service itself, cached per machine.

Discovery goes through the service's own local port (so it works for both
direct and ssh routes) and never leaves that origin. Best candidates first:
web-app manifest icons > apple-touch-icon > <link rel=icon> > /favicon.ico.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import json
import logging
import ssl
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote_to_bytes, urljoin, urlsplit

from .config import Service
from .paths import icon_cache_dir

logger = logging.getLogger("tunhub")

MAX_PAGE_BYTES = 512 * 1024
MAX_ICON_BYTES = 1024 * 1024
MAX_REDIRECTS = 5
REQUEST_TIMEOUT = 5.0
RETRY_FAILED_AFTER = 3600.0  # seconds before retrying a service without icon

CONTENT_TYPES = {
    "png": "image/png",
    "ico": "image/x-icon",
    "svg": "image/svg+xml",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


def sniff_image(data: bytes) -> str | None:
    """Return a file extension if data looks like a supported image."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "ico"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    head = data[:512].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "svg"
    return None


# ---------------------------------------------------------------------------
# Candidate discovery


@dataclass(frozen=True)
class Candidate:
    url: str
    score: int  # roughly the icon size in pixels; higher is better


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "link":
            self.links.append({name: value or "" for name, value in attrs})


def _size_score(sizes: str, default: int) -> int:
    best = 0
    for token in sizes.lower().split():
        if token == "any":
            best = max(best, 512)
        elif "x" in token:
            width = token.split("x", 1)[0]
            if width.isdigit():
                best = max(best, int(width))
    return best or default


def parse_html_candidates(html: str, page_url: str) -> tuple[list[Candidate], str | None]:
    """Return icon candidates and the manifest URL (if any) declared by a page."""
    parser = _LinkParser()
    try:
        parser.feed(html)
    except Exception:  # malformed HTML: keep whatever was parsed
        pass

    candidates: list[Candidate] = []
    manifest_url = None
    for link in parser.links:
        rel = link.get("rel", "").lower().split()
        href = link.get("href", "").strip()
        if not href:
            continue
        url = urljoin(page_url, href)
        if "manifest" in rel:
            manifest_url = manifest_url or url
        elif "apple-touch-icon" in rel or "apple-touch-icon-precomposed" in rel:
            candidates.append(Candidate(url, _size_score(link.get("sizes", ""), 180)))
        elif "icon" in rel:
            is_svg = "svg" in link.get("type", "") or href.lower().split("?")[0].endswith(".svg")
            candidates.append(Candidate(url, _size_score(link.get("sizes", ""), 512 if is_svg else 32)))
    return candidates, manifest_url


def parse_manifest_candidates(text: str, manifest_url: str) -> list[Candidate]:
    try:
        icons = json.loads(text).get("icons") or []
    except (ValueError, AttributeError):
        return []
    candidates = []
    for icon in icons:
        if isinstance(icon, dict) and icon.get("src"):
            score = _size_score(str(icon.get("sizes", "")), 64)
            candidates.append(Candidate(urljoin(manifest_url, str(icon["src"])), score))
    return candidates


# ---------------------------------------------------------------------------
# Fetching (blocking; run in a worker thread)


class _Origin:
    """Requests restricted to one service, always sent to 127.0.0.1:<local_port>."""

    def __init__(self, service: Service) -> None:
        self.service = service
        self.base_url = service.url or ""
        self.allowed_hosts = {service.hostname, "127.0.0.1", "localhost"}

    def is_same_origin(self, url: str) -> bool:
        parts = urlsplit(url)
        return (
            parts.scheme == self.service.type
            and parts.hostname in self.allowed_hosts
            and (parts.port or (443 if parts.scheme == "https" else 80)) == self.service.local_port
        )

    def get(self, url: str, limit: int) -> tuple[str, bytes]:
        """GET url (following same-origin redirects); return (final_url, body)."""
        if url.startswith("data:"):
            return url, _decode_data_url(url, limit)
        for _ in range(MAX_REDIRECTS + 1):
            if not self.is_same_origin(url):
                raise ValueError(f"refusing to leave the service origin: {url}")
            status, location, body = self._request(url, limit)
            if status in (301, 302, 303, 307, 308) and location:
                url = urljoin(url, location)
                continue
            if status != 200:
                raise ValueError(f"HTTP {status} for {url}")
            return url, body
        raise ValueError("too many redirects")

    def _request(self, url: str, limit: int) -> tuple[int, str | None, bytes]:
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        if self.service.type == "https":
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                "127.0.0.1", self.service.local_port, timeout=REQUEST_TIMEOUT, context=context
            )
        else:
            conn = http.client.HTTPConnection("127.0.0.1", self.service.local_port, timeout=REQUEST_TIMEOUT)
        try:
            conn.request("GET", path, headers={"Host": self.service.address, "User-Agent": "tunhub"})
            response = conn.getresponse()
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError(f"response too large: {url}")
            return response.status, response.getheader("Location"), body
        finally:
            conn.close()


def _decode_data_url(url: str, limit: int) -> bytes:
    header, _, payload = url.partition(",")
    data = base64.b64decode(payload) if header.endswith(";base64") else unquote_to_bytes(payload)
    if len(data) > limit:
        raise ValueError("data URL too large")
    return data


def discover_icon(service: Service) -> tuple[bytes, str]:
    """Find the best icon of a running web service. Blocking."""
    origin = _Origin(service)
    candidates: list[Candidate] = []
    page_url = origin.base_url
    try:
        page_url, page = origin.get(origin.base_url, MAX_PAGE_BYTES)
        html_candidates, manifest_url = parse_html_candidates(page.decode("utf-8", "replace"), page_url)
        candidates += html_candidates
        if manifest_url:
            try:
                manifest_url, manifest = origin.get(manifest_url, MAX_PAGE_BYTES)
                candidates += parse_manifest_candidates(manifest.decode("utf-8", "replace"), manifest_url)
            except (OSError, ValueError) as exc:
                logger.debug("manifest fetch failed: %s", exc)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        logger.debug("page fetch failed: %s", exc)
    candidates.append(Candidate(urljoin(page_url, "/favicon.ico"), 16))

    seen = set()
    errors = []
    for candidate in sorted(candidates, key=lambda c: c.score, reverse=True):
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        try:
            _, data = origin.get(candidate.url, MAX_ICON_BYTES)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            errors.append(str(exc))
            continue
        extension = sniff_image(data)
        if extension:
            return data, extension
        errors.append(f"not an image: {candidate.url}")
    raise LookupError("no icon found (" + "; ".join(errors[-3:]) + ")")


# ---------------------------------------------------------------------------
# Store


class IconStore:
    def __init__(self, config_dir: Path, cache_dir: Path | None = None) -> None:
        self.config_dir = config_dir
        self.cache_dir = cache_dir or icon_cache_dir()
        self._failed_at: dict[str, float] = {}
        self._inflight: set[str] = set()

    def lookup(self, service: Service) -> Path | None:
        """Configured icon file first, then the cached discovered icon."""
        if service.icon:
            path = (self.config_dir / service.icon).resolve()
            return path if path.is_file() else None
        for extension in CONTENT_TYPES:
            path = self.cache_dir / f"{service.id}.{extension}"
            if path.is_file():
                return path
        return None

    def version(self, service: Service) -> str | None:
        path = self.lookup(service)
        return str(path.stat().st_mtime_ns) if path else None

    async def ensure(self, service: Service, log=None) -> None:
        """Discover and cache the icon unless it is known or recently failed."""
        if not service.is_web or service.icon or self.lookup(service) is not None:
            return
        if service.id in self._inflight:
            return
        if time.time() - self._failed_at.get(service.id, 0) < RETRY_FAILED_AFTER:
            return
        self._inflight.add(service.id)
        try:
            data, extension = await asyncio.to_thread(discover_icon, service)
            self._save(service.id, data, extension)
            self._failed_at.pop(service.id, None)
            if log:
                log(f"icon cached ({extension}, {len(data)} bytes)")
        except Exception as exc:
            self._failed_at[service.id] = time.time()
            if log:
                log(f"icon discovery failed: {exc}")
        finally:
            self._inflight.discard(service.id)

    async def refresh(self, service: Service, log=None) -> None:
        self._remove_cached(service.id)
        self._failed_at.pop(service.id, None)
        await self.ensure(service, log)

    def _save(self, service_id: str, data: bytes, extension: str) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._remove_cached(service_id)
        target = self.cache_dir / f"{service_id}.{extension}"
        temp = target.with_suffix(".tmp")
        temp.write_bytes(data)
        temp.replace(target)

    def _remove_cached(self, service_id: str) -> None:
        for extension in CONTENT_TYPES:
            (self.cache_dir / f"{service_id}.{extension}").unlink(missing_ok=True)
