"""Synchronous public HTTP client, also used by the stdio-to-service bridge."""

from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx

from .errors import CoordinationError
from .models import OPERATIONS


@asynccontextmanager
async def mcp_client(url: str, token: str, mode: str = "auto"):
    """Authenticated SDK client with proxy inheritance and response caching disabled."""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    base = validate_url(url)
    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + token}, trust_env=False
    ) as http:
        transport = streamable_http_client(base + "/mcp", http_client=http)
        async with Client(transport, mode=mode, cache=None, read_timeout_seconds=20) as client:
            yield client


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Use an http(s) service URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Service URL must not contain credentials, query or fragment")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Plain HTTP is supported only over loopback; use an SSH tunnel or HTTPS")
    return url.rstrip("/")


class RelayFenceClient:
    def __init__(self, url: str, token: str, timeout: float = 20):
        self.url = validate_url(url)
        self.http = httpx.Client(
            base_url=self.url,
            headers={"Authorization": "Bearer " + token},
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )

    def call_raw(self, operation: str, payload: dict) -> tuple[dict, int]:
        if operation not in OPERATIONS:
            return CoordinationError(
                "unknown_operation", "Unknown operation", status=404
            ).as_dict(), 404
        try:
            response = self.http.post("/v1/" + operation, json=payload)
            result = response.json()
            if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                raise ValueError("Invalid envelope")
            return result, response.status_code
        except (httpx.HTTPError, ValueError):
            return CoordinationError(
                "transport_error",
                "No usable server response. Outcome may be unknown; retry the SAME request_id and payload",
                status=503,
            ).as_dict(), 503

    def call(self, operation: str, **payload) -> dict:
        result, status = self.call_raw(operation, payload)
        if not result["ok"]:
            error = result["error"]
            raise CoordinationError(error["code"], error["message"], error.get("details"), status)
        return result

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
