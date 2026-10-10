"""Authenticated loopback HTTP and MCP application."""

import json
import secrets
from importlib.resources import files
from urllib.parse import urlparse

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from . import __version__
from .errors import CoordinationError
from .mcp_server import make_mcp
from .models import OPERATIONS
from .service import Service
from .store import MAX_PAYLOAD_BYTES, Store


class LocalBoundary:
    """Host/Origin checks also cover REST and the dashboard, outside the MCP SDK."""

    def __init__(self, app, token):
        self.app = app
        self.authorization = ("Bearer " + token).encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        host = headers.get(b"host", b"").decode("latin-1")
        try:
            hostname = urlparse("http://" + host).hostname
        except ValueError:
            hostname = None
        error = None
        if hostname not in ("127.0.0.1", "localhost", "::1"):
            error = CoordinationError(
                "invalid_host", "This service accepts loopback hosts only", status=421
            )
        elif (
            b"origin" in headers
            and headers[b"origin"].decode("latin-1") != f"{scope['scheme']}://{host}"
        ):
            error = CoordinationError(
                "invalid_origin", "Cross-origin requests are not allowed", status=403
            )
        elif scope["path"] not in ("/", "/healthz") and not secrets.compare_digest(
            headers.get(b"authorization", b""), self.authorization
        ):
            error = CoordinationError(
                "unauthorized", "A valid RelayFence bearer token is required", status=401
            )
        if error:
            response = JSONResponse(
                error.as_dict(), status_code=error.status, headers={"Cache-Control": "no-store"}
            )
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)


def create_app(store: Store, token: str):
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("Access token must be at least 32 ASCII characters without whitespace")
    service = Service(store)
    mcp = make_mcp(service.call)
    app = mcp.streamable_http_app(
        json_response=True, stateless_http=True, max_request_body_size=MAX_PAYLOAD_BYTES + 65536
    )

    async def health(request: Request):
        return JSONResponse({"status": "ok", "version": __version__})

    async def schema(request: Request):
        return JSONResponse(
            {
                name: {"description": description, "request": model.model_json_schema()}
                for name, (model, description) in OPERATIONS.items()
            }
        )

    async def call(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_PAYLOAD_BYTES:
                return JSONResponse(
                    CoordinationError(
                        "payload_too_large", "Request exceeds 128 KiB", status=413
                    ).as_dict(),
                    status_code=413,
                )
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            return JSONResponse(
                CoordinationError(
                    "invalid_json", "Expected a JSON request body", status=400
                ).as_dict(),
                status_code=400,
            )
        result, status = await anyio.to_thread.run_sync(
            service.call, request.path_params["operation"], payload
        )
        return JSONResponse(result, status_code=status, headers={"Cache-Control": "no-store"})

    async def dashboard(request: Request):
        return HTMLResponse(
            files("relayfence").joinpath("dashboard.html").read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            },
        )

    app.router.routes[0:0] = [
        Route("/", dashboard),
        Route("/healthz", health),
        Route("/v1/schema", schema),
        Route("/v1/{operation}", call, methods=["POST"]),
    ]
    return LocalBoundary(app, token)
