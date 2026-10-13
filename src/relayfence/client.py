"""Synchronous public HTTP client, also used by the stdio-to-service bridge."""

import asyncio
import json
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx

from .errors import CoordinationError
from .models import OPERATIONS

# List/claim replies contain multiple 128 KiB values; the request limit is too
# small for replies. Bound decoded response storage, including chunked bodies.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def _transport_error():
    return CoordinationError(
        "transport_error",
        "No usable server response. Outcome may be unknown; retry the SAME request_id and payload",
        status=503,
    ).as_dict(), 503


def _decode_response(raw, status):
    result = json.loads(raw)
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise ValueError("Invalid envelope")
    if result["ok"]:
        if status != 200 or not isinstance(result.get("data"), dict):
            raise ValueError("Invalid success envelope")
    else:
        error = result.get("error")
        if (
            not 400 <= status <= 599
            or not isinstance(error, dict)
            or not isinstance(error.get("code"), str)
            or not error["code"]
            or not isinstance(error.get("message"), str)
            or not isinstance(error.get("details", {}), dict)
        ):
            raise ValueError("Invalid error envelope")
    return result, status


def _unwrap(result, status):
    if not result["ok"]:
        error = result["error"]
        raise CoordinationError(error["code"], error["message"], error.get("details"), status)
    return result


def _append(raw, chunk):
    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
        raise ValueError("Response exceeded its size limit")
    raw.extend(chunk)


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
            with self.http.stream("POST", "/v1/" + operation, json=payload) as response:
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=65_536):
                    _append(raw, chunk)
                return _decode_response(raw, response.status_code)
        except (httpx.HTTPError, ValueError):
            return _transport_error()

    def call(self, operation: str, **payload) -> dict:
        return _unwrap(*self.call_raw(operation, payload))

    def async_peer(self, timeout=5):
        """Independent connection pool for cancellable lease-control requests."""
        return AsyncRelayFenceClient(self.url, self.http.headers["Authorization"], timeout)

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class AsyncRelayFenceClient:
    """Task-scoped control transport with an overall, cancellable deadline."""

    def __init__(self, url, authorization, timeout):
        self.timeout = timeout
        self.http = httpx.AsyncClient(
            base_url=validate_url(url),
            headers={"Authorization": authorization},
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )

    async def call(self, operation, **payload):
        if operation not in OPERATIONS:
            raise CoordinationError("unknown_operation", "Unknown operation", status=404)
        try:
            async with asyncio.timeout(self.timeout):
                async with self.http.stream("POST", "/v1/" + operation, json=payload) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65_536):
                        _append(raw, chunk)
                    result, status = _decode_response(raw, response.status_code)
        except (httpx.HTTPError, ValueError, TimeoutError):
            result, status = _transport_error()
        return _unwrap(result, status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()
