"""FastAPI application: dashboard, bookmark entry points and JSON API."""

from __future__ import annotations

import html
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .config import SHARED_FILE, ConfigError
from .paths import default_config_dir
from .hub import Hub
from .icons import CONTENT_TYPES
from .service import ServiceRuntime, State

STATIC_DIR = Path(__file__).parent / "static"
NO_STORE = {"Cache-Control": "no-store"}


class RouteChoice(BaseModel):
    route: int | None = None


def _is_trusted_fetch(request: Request) -> bool:
    """Whether a GET may come from the user rather than from another web page.

    Browsers send Sec-Fetch-* headers that pages cannot forge. Allowed are
    bookmarks and typed URLs ("none"), the dashboard itself ("same-origin"), and
    links the user clicked on another page (a top-level navigation with user
    activation). Clients without these headers, such as curl, are allowed too.
    """
    site = request.headers.get("sec-fetch-site")
    if site is None or site in ("none", "same-origin"):
        return True
    return (
        request.headers.get("sec-fetch-mode") == "navigate"
        and request.headers.get("sec-fetch-dest") == "document"
        and request.headers.get("sec-fetch-user") == "?1"
    )


def create_app(hub: Hub) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await hub.start()
        yield
        await hub.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # Shown on the setup card; a plain `tunhub init` only fits the default directory.
    init_command = "tunhub init"
    if hub.config_dir.resolve() != default_config_dir().resolve():
        init_command += f' --config-dir "{hub.config_dir}"'
    settings = hub.hub_settings
    allowed_hosts = settings.allowed_hosts
    allowed_origins = {f"http://{host}" for host in allowed_hosts}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Reject foreign Host headers: blocks DNS-rebinding attacks.
        if request.headers.get("host", "").lower() not in allowed_hosts:
            return PlainTextResponse("tunhub: host not allowed", status_code=403)
        # State-changing calls need a custom header. Browsers cannot add it
        # cross-site without a CORS preflight, which this server never grants.
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if request.headers.get("x-tunhub") != "1" or (origin and origin not in allowed_origins):
                return PlainTextResponse("tunhub: cross-site request rejected", status_code=403)
        # Other web pages must not send GETs either: /open/<id> starts tunnels,
        # and <img>, fetch() or a scripted redirect could trigger it.
        elif not _is_trusted_fetch(request):
            return PlainTextResponse("tunhub: cross-site request rejected", status_code=403)

        response = await call_next(request)
        # Never render inside another site's frame (clickjacking).
        response.headers["X-Frame-Options"] = "DENY"
        csp = response.headers.get("Content-Security-Policy")
        response.headers["Content-Security-Policy"] = (
            f"{csp}; frame-ancestors 'none'" if csp else "frame-ancestors 'none'"
        )
        return response

    def runtime_or_404(service_id: str) -> ServiceRuntime:
        runtime = hub.get(service_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail=f"unknown service '{service_id}'")
        return runtime

    # ------------------------------------------------------------------
    # Pages

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers=NO_STORE)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/open/{service_id}", include_in_schema=False)
    async def open_service(service_id: str):
        """Bookmark entry point: bring the service up, then redirect to it."""
        runtime = hub.get(service_id)
        if runtime is None:
            if hub.config_error:  # the service may be in the files that cannot be loaded
                return _error_page(
                    "tunhub cannot use its configuration",
                    hub.config_error,
                    [],
                    retry_url=f"/open/{service_id}",
                    status=503,
                    hint="Fix the file, reload the configuration on the dashboard, then retry.",
                )
            return _error_page(f"Unknown service '{service_id}'", "", [], status=404)

        if await runtime.ensure_up(settings.open_timeout):
            target = runtime.service.url or f"/#{service_id}"
            return RedirectResponse(target, status_code=302, headers=NO_STORE)

        lines = [message for _, message in list(runtime.logs)[-30:]]
        return _error_page(
            f"Could not open {runtime.service.name}",
            runtime.last_error or "timed out",
            lines,
            retry_url=f"/open/{service_id}",
        )

    # ------------------------------------------------------------------
    # JSON API

    @app.get("/api/state")
    async def state() -> JSONResponse:
        return JSONResponse(
            {
                "machine": hub.config.machine,
                "version": __version__,
                "hub": f"http://{settings.hostname}:{settings.port}",
                # Where to edit the services, and whether the file exists yet.
                "config": {
                    "path": str(hub.config_dir / SHARED_FILE),
                    "found": hub.config.found,
                    "init_command": init_command,
                    "local_path": str(hub.config.local_path) if hub.config.local_path else None,
                    "local_found": hub.config.local_found,
                    "warnings": hub.warnings,
                    # The files cannot be used: why, and whether an earlier
                    # version of them is still in use.
                    "error": hub.config_error,
                    "error_keeps_previous": hub.config_loaded,
                },
                "services": [
                    {**runtime.snapshot(), "icon_version": hub.icons.version(runtime.service)}
                    for runtime in hub.runtimes.values()
                ],
            },
            headers=NO_STORE,
        )

    @app.get("/api/icons/{service_id}")
    async def icon(service_id: str) -> FileResponse:
        path = hub.icons.lookup(runtime_or_404(service_id).service)
        if path is None:
            raise HTTPException(status_code=404, detail="no icon")
        media_type = CONTENT_TYPES.get(path.suffix.lstrip(".").lower(), "application/octet-stream")
        # Icons come from the services; never let one run as a document here.
        headers = {
            "Cache-Control": "no-cache",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "X-Content-Type-Options": "nosniff",
        }
        return FileResponse(path, media_type=media_type, headers=headers)

    @app.post("/api/services/{service_id}/icon/refresh")
    async def refresh_icon(service_id: str) -> dict:
        runtime = runtime_or_404(service_id)
        if not runtime.service.is_web or runtime.service.icon:
            raise HTTPException(status_code=400, detail="this service has no discoverable icon")
        if runtime.state is not State.UP:
            raise HTTPException(status_code=409, detail="connect the service first")
        await hub.icons.refresh(runtime.service, runtime.log)
        return {"ok": hub.icons.lookup(runtime.service) is not None}

    @app.get("/api/services/{service_id}/logs")
    async def logs(service_id: str) -> JSONResponse:
        runtime = runtime_or_404(service_id)
        entries = [{"time": t, "message": m} for t, m in list(runtime.logs)]
        return JSONResponse({"logs": entries}, headers=NO_STORE)

    @app.post("/api/services/{service_id}/start")
    async def start(service_id: str) -> dict:
        runtime_or_404(service_id).start()
        return {"ok": True}

    @app.post("/api/services/{service_id}/stop")
    async def stop(service_id: str) -> dict:
        await runtime_or_404(service_id).stop()
        return {"ok": True}

    @app.post("/api/services/{service_id}/route")
    async def choose_route(service_id: str, choice: RouteChoice) -> dict:
        try:
            runtime_or_404(service_id).set_pinned_route(choice.route)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"ok": True}

    @app.post("/api/disconnect-ssh")
    async def disconnect_ssh() -> dict:
        return {"ok": True, "stopped": await hub.disconnect_ssh()}

    @app.post("/api/reload")
    async def reload() -> JSONResponse:
        try:
            messages = await hub.reload()
        except ConfigError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "messages": messages})

    return app


def _error_page(
    title: str,
    error: str,
    log_lines: list[str],
    retry_url: str | None = None,
    status: int = 502,
    hint: str = "",
) -> HTMLResponse:
    escape = html.escape
    retry = f'<a class="btn" href="{escape(retry_url)}">Retry</a>' if retry_url else ""
    hint_html = f'<p class="muted">{escape(hint)}</p>' if hint else ""
    log_html = f'<h2>Recent log</h2><pre class="log">{escape(chr(10).join(log_lines))}</pre>' if log_lines else ""
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<link rel="stylesheet" href="/static/style.css">
</head><body><main class="error-page">
<h1>{escape(title)}</h1>
<p class="error">{escape(error)}</p>
{hint_html}
<div class="actions">{retry}<a class="btn ghost" href="/">Back to hub</a></div>
{log_html}
</main></body></html>"""
    return HTMLResponse(body, status_code=status, headers=NO_STORE)
