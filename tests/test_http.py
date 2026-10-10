import json
import sqlite3

import pytest
from starlette.testclient import TestClient

from relayfence.server import create_app

TOKEN = "test-access-token-with-at-least-32-characters"


@pytest.fixture
def api(ledger):
    with TestClient(
        create_app(ledger.store, TOKEN),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN},
    ) as client:
        yield client


def test_http_matches_domain_contract_and_no_cache(api):
    payload = {
        "actor": "api",
        "request_id": "one",
        "key": "spec",
        "expected_version": 0,
        "value": {"ok": True},
    }
    first = api.post("/v1/state_put", json=payload)
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert api.post("/v1/state_put", json=payload).json()["replayed"]
    conflict = api.post("/v1/state_put", json={**payload, "request_id": "two"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "version_conflict"
    assert api.post("/v1/item_get", json={"key": "spec"}).json()["data"]["value"] == {"ok": True}
    assert "task_claim" in api.get("/v1/schema").json()


@pytest.mark.parametrize(
    "path,method", [("/v1/schema", "get"), ("/v1/items_list", "post"), ("/mcp", "post")]
)
def test_all_data_and_mcp_routes_require_bearer(api, path, method):
    for token in ["", "Bearer wrong"]:
        response = getattr(api, method)(path, headers={"Authorization": token})
        assert response.status_code == 401
        assert TOKEN not in response.text


def test_origin_and_host_checks_cover_the_whole_app(api):
    assert api.get("/", headers={"Host": "attacker.example"}).status_code == 421
    assert api.get("/healthz", headers={"Origin": "https://attacker.example"}).status_code == 403
    assert api.get("/healthz", headers={"Origin": "null"}).status_code == 403
    assert api.get("/healthz", headers={"Origin": "http://127.0.0.1"}).status_code == 200
    assert api.get("/healthz").json()["status"] == "ok"


def test_dashboard_has_no_credentials_or_stored_data(api):
    response = api.get("/", headers={"Authorization": ""})
    assert response.status_code == 200
    assert "RelayFence" in response.text and TOKEN not in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_bad_requests_are_bounded_and_redacted(api):
    assert api.post("/v1/item_get", content="{").status_code == 400
    assert api.post("/v1/item_get", content=b"\xff").status_code == 400
    assert api.post("/v1/state_put", json=[]).status_code == 422
    assert api.post("/v1/unknown", json={}).status_code == 404
    assert api.post("/v1/state_put", content="x" * 140_000).status_code == 413
    assert api.post("/mcp", content="x" * 210_000).status_code == 413
    response = api.post("/v1/item_get", json={"key": "x", "surprise": "private-secret"})
    assert response.status_code == 422 and "private-secret" not in response.text


@pytest.mark.parametrize("token", ["", "short", "x" * 32 + " ", "密" * 32])
def test_invalid_server_token_fails_at_startup(ledger, token):
    with pytest.raises(ValueError, match="Access token"):
        create_app(ledger.store, token)


class LoseFirstResponse:
    def __init__(self, app):
        self.app = app
        self.lost = False

    async def __call__(self, scope, receive, send):
        async def drop(message):
            if not self.lost and scope.get("path") == "/v1/task_complete":
                self.lost = True
                raise RuntimeError("Simulated connection loss AFTER commit")
            await send(message)

        await self.app(scope, receive, drop)


def test_lost_completion_response_can_be_retried_without_duplicate_effect(ledger):
    lease = ledger.claim()
    payload = {
        **lease,
        "request_id": "complete-once",
        "outputs": [
            {"key": "artifact", "expected_version": 0, "value": "done"},
        ],
    }
    app = LoseFirstResponse(create_app(ledger.store, TOKEN))
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        raise_server_exceptions=False,
        headers={"Authorization": "Bearer " + TOKEN},
    ) as client:
        assert client.post("/v1/task_complete", json=payload).status_code == 500
        retry = client.post("/v1/task_complete", json=payload).json()
        assert retry["ok"] and retry["replayed"]
        assert retry["data"]["artifacts"][0]["version"] == 1
    assert (
        len([e for e in ledger.ok("events_list")["events"] if e["kind"] == "task.completed"]) == 1
    )


@pytest.mark.parametrize(
    "exception,code,status",
    [
        (sqlite3.OperationalError("database is locked"), "busy", 503),
        (sqlite3.OperationalError("disk issue"), "storage_error", 503),
        (RuntimeError("private internal details"), "internal_error", 500),
    ],
)
def test_storage_errors_are_actionable_without_leaking_internals(
    ledger, monkeypatch, exception, code, status
):
    def fail(*args):
        raise exception

    monkeypatch.setattr(ledger.store, "execute", fail)
    result, actual_status = ledger.service.call("items_list", {})
    assert actual_status == status and result["error"]["code"] == code
    assert "private internal details" not in json.dumps(result)
