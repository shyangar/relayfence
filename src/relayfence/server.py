"""Authenticated loopback HTTP and MCP application."""

import ipaddress
import json
import secrets
import sqlite3
from contextlib import asynccontextmanager
from importlib.resources import files
from urllib.parse import urlparse

import anyio
from starlette.requests import ClientDisconnect, Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import __version__
from .errors import CoordinationError
from .identity import ADMIN, Principal
from .mcp_server import make_mcp
from .models import OPERATIONS
from .service import Service
from .store import MAX_PAYLOAD_BYTES, Store, digest


class LocalBoundary:
    """Host/Origin checks also cover REST and the dashboard, outside the MCP SDK."""

    def __init__(self, app, token, store, sessions=None, local_ui=False):
        self.app = app
        self.authorization = ("Bearer " + token).encode("utf-8")
        self.store = store
        self.sessions = sessions
        self.local_ui = local_ui

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
        elif scope["path"] not in ("/", "/console", "/office.js", "/office.css", "/healthz"):
            authorization = headers.get(b"authorization", b"")
            try:
                local_peer = ipaddress.ip_address(scope.get("client", ("", 0))[0]).is_loopback
            except ValueError:
                local_peer = False
            # Local UI mode trusts the local OS account, not a public marker as
            # a secret. Browsers must positively identify a same-origin fetch;
            # foreign pages cannot set Sec-Fetch-Site or pass the Origin check.
            # Native/MCP clients still authenticate with their scoped tokens.
            local_browser = (
                self.local_ui
                and local_peer
                and not authorization
                and headers.get(b"x-relayfence-local") == b"1"
                and (scope["path"].startswith("/local/") or scope["path"].startswith("/v1/"))
                and headers.get(b"sec-fetch-site") == b"same-origin"
            )
            cookie_valid = self.sessions is not None and self.sessions.valid(
                headers.get(b"cookie", b"").decode("latin-1")
            )
            local_pairing = (
                self.sessions is not None
                and scope["path"] == "/local/session"
                and scope["method"] == "POST"
            )
            if (
                (cookie_valid or local_pairing or local_browser)
                and scope["method"] not in ("GET", "HEAD")
                and (
                    headers.get(b"origin", b"").decode("latin-1") != f"{scope['scheme']}://{host}"
                    or headers.get(b"content-type", b"").split(b";")[0] != b"application/json"
                )
            ):
                error = CoordinationError(
                    "invalid_origin", "Local browser writes require same-origin JSON", status=403
                )
            elif local_pairing:
                pass
            elif (
                secrets.compare_digest(authorization, self.authorization)
                or cookie_valid
                or local_browser
            ):
                scope["relayfence_principal"] = ADMIN
            else:
                hashed = (
                    digest(authorization[7:].decode("latin-1"))
                    if authorization.startswith(b"Bearer ")
                    else ""
                )
                if hashed and await anyio.to_thread.run_sync(self.store.credential_valid, hashed):
                    scope["relayfence_principal"] = Principal(token_hash=hashed)
                else:
                    error = CoordinationError(
                        "unauthorized", "A valid RelayFence bearer token is required", status=401
                    )
        if error:
            response = JSONResponse(
                error.as_dict(), status_code=error.status, headers={"Cache-Control": "no-store"}
            )
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)


def create_app(
    store: Store,
    token: str,
    *,
    sessions=None,
    workspace_manager=None,
    on_ready=None,
    local_ui=False,
):
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("Access token must be at least 32 ASCII characters without whitespace")
    service = Service(store)
    mcp = make_mcp(service.call, authenticated=True)
    app = mcp.streamable_http_app(
        json_response=True, stateless_http=True, max_request_body_size=MAX_PAYLOAD_BYTES + 65536
    )
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def local_lifespan(application):
        async with original_lifespan(application):
            if workspace_manager is not None:
                workspace_manager.start_discovery()
            if on_ready is not None:
                on_ready()
            try:
                yield
            finally:
                if workspace_manager is not None:
                    await anyio.to_thread.run_sync(workspace_manager.close)

    app.router.lifespan_context = local_lifespan

    async def local(request: Request):
        action = request.path_params["action"]
        headers = {"Cache-Control": "no-store"}
        if action != "session" and request.scope.get("relayfence_principal") != ADMIN:
            return JSONResponse({"error": {"message": "Local administration required"}}, 403)
        try:
            payload = {}
            if request.method == "POST":
                body = bytearray()
                try:
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > (
                            32768
                            if action
                            in (
                                "member-guide",
                                "task-control",
                                "agent-credential",
                                "decision-respond",
                            )
                            else 8192
                        ):
                            return JSONResponse({"error": {"message": "Request too large"}}, 413)
                except ClientDisconnect:
                    # The caller is gone (closed tab, cancelled fetch); nothing to answer.
                    return JSONResponse({"error": {"message": "Request interrupted"}}, 400)
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError("Expected a JSON object")
            if action == "session" and request.method == "POST" and sessions is not None:
                session = sessions.pair(payload.get("code"))
                if session is None:
                    return JSONResponse(
                        {"error": {"message": "配对链接已失效，请从本机重新启动服务。"}},
                        401,
                        headers=headers,
                    )
                response = JSONResponse({"ok": True}, headers=headers)
                response.set_cookie(
                    sessions.cookie_name,
                    session,
                    httponly=True,
                    samesite="strict",
                    max_age=8 * 3600,
                    path="/",
                )
                return response
            if workspace_manager is None:
                return JSONResponse(
                    {"error": {"message": "Local project mode is unavailable"}}, 404
                )
            if action == "runtime" and request.method == "POST":
                result = await anyio.to_thread.run_sync(
                    workspace_manager.runtime_status, payload.get("project")
                )
            elif action == "member-chat" and request.method == "POST":
                result = await anyio.to_thread.run_sync(
                    workspace_manager.member_chat.history,
                    payload.get("project"),
                    payload.get("member_id"),
                    payload.get("before"),
                    payload.get("limit", 100),
                )
            elif action == "group-chat" and request.method == "POST":
                result = await anyio.to_thread.run_sync(
                    workspace_manager.group_chat.history,
                    payload.get("project"),
                    payload.get("group_id"),
                    payload.get("after"),
                    payload.get("before"),
                    payload.get("limit", 100),
                )
            elif action == "meeting-control" and request.method == "POST":
                from .local_task_control import control_meeting

                result = await anyio.to_thread.run_sync(control_meeting, workspace_manager, payload)
            elif action == "decision-respond" and request.method == "POST":
                from .local_task_control import respond_decision

                result = await anyio.to_thread.run_sync(
                    respond_decision, workspace_manager, payload
                )
            elif action == "task-control" and request.method == "POST":
                from .local_task_control import control

                result = await anyio.to_thread.run_sync(control, workspace_manager, payload)
            elif action == "task-delete" and request.method == "POST":
                from .local_task_control import delete

                result = await anyio.to_thread.run_sync(delete, workspace_manager, payload)
            elif action == "member-guide" and request.method == "POST":
                result = await anyio.to_thread.run_sync(workspace_manager.member_chat.send, payload)
            elif action == "agents" and request.method == "GET":
                result = await anyio.to_thread.run_sync(workspace_manager.agent_inventory)
            elif action == "agent-credential" and request.method == "POST":
                result = await anyio.to_thread.run_sync(
                    workspace_manager.save_agent_credential, payload
                )
            elif action == "agent-credential-delete" and request.method == "POST":
                result = await anyio.to_thread.run_sync(
                    workspace_manager.delete_agent_credential, payload
                )
            elif action == "projects" and request.method == "GET":
                result = await anyio.to_thread.run_sync(workspace_manager.projects)
            elif action == "projects" and request.method == "POST":
                result = await anyio.to_thread.run_sync(
                    workspace_manager.add_project, payload.get("path")
                )
            elif action == "status" and request.method == "GET":
                result = await anyio.to_thread.run_sync(workspace_manager.status)
            elif action == "project-sessions" and request.method == "POST":
                result = await anyio.to_thread.run_sync(
                    workspace_manager.project_sessions, payload.get("project")
                )
            elif action == "project-session-reset" and request.method == "POST":
                result = await anyio.to_thread.run_sync(
                    workspace_manager.reset_project_session, payload
                )
            elif action == "connect" and request.method == "POST":
                result = await anyio.to_thread.run_sync(
                    workspace_manager.connect, payload.get("path")
                )
            elif action == "refresh" and request.method == "POST":
                result = await anyio.to_thread.run_sync(workspace_manager.refresh)
            elif action == "browse" and request.method == "POST":
                from .local_session import browse_directories

                result = await anyio.to_thread.run_sync(browse_directories, payload.get("path"))
            elif action == "pick" and request.method == "POST":
                from .local_session import pick_directory

                result = {"path": await anyio.to_thread.run_sync(pick_directory)}
            else:
                return JSONResponse({"error": {"message": "Unknown local operation"}}, 404)
            return JSONResponse(result, headers=headers)
        except CoordinationError as exc:
            return JSONResponse(exc.as_dict(), status_code=exc.status, headers=headers)
        except sqlite3.Error:
            # A busy ledger is transient; surface a 503 instead of an unhandled 500.
            return JSONResponse(
                {"error": {"message": "Ledger temporarily busy, try again shortly"}},
                503,
                headers=headers,
            )
        except (ValueError, OSError) as exc:
            # Manager validation errors are safe user-facing messages, not raw vendor logs.
            return JSONResponse({"error": {"message": str(exc)}}, 400, headers=headers)

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
        try:
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_PAYLOAD_BYTES:
                    return JSONResponse(
                        CoordinationError(
                            "payload_too_large", "Request exceeds 128 KiB", status=413
                        ).as_dict(),
                        status_code=413,
                    )
        except ClientDisconnect:
            return JSONResponse({"error": {"message": "Request interrupted"}}, status_code=400)
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
            service.call,
            request.path_params["operation"],
            payload,
            request.scope.get("relayfence_principal", Principal()),
        )
        return JSONResponse(result, status_code=status, headers={"Cache-Control": "no-store"})

    async def dashboard(request: Request):
        return HTMLResponse(
            files("relayfence")
            .joinpath("dashboard.html" if request.url.path == "/console" else "office.html")
            .read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            },
        )

    async def office_asset(request: Request):
        name = request.url.path.removeprefix("/")
        return Response(
            files("relayfence").joinpath(name).read_text(encoding="utf-8"),
            media_type="text/javascript" if name.endswith(".js") else "text/css",
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    app.router.routes[0:0] = [
        Route("/", dashboard),
        Route("/console", dashboard),
        Route("/office.js", office_asset),
        Route("/office.css", office_asset),
        Route("/healthz", health),
        Route("/local/{action}", local, methods=["GET", "POST"]),
        Route("/v1/schema", schema),
        Route("/v1/{operation}", call, methods=["POST"]),
    ]
    return LocalBoundary(app, token, store, sessions, local_ui)
