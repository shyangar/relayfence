import asyncio
import json
import sys
import uuid

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from relayfence.cli import init_directory
from relayfence.client import RelayFenceClient, mcp_client
from relayfence.demo import run_demo
from relayfence.models import OPERATIONS
from relayfence.runner import LocalServer


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    directory = init_directory(tmp_path_factory.mktemp("mcp-server"))
    token = (directory / "access.token").read_text().strip()
    with LocalServer(directory, token) as process:
        yield process


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_actual_mcp_transport_schemas_success_and_conflict(server, mode):
    async def scenario():
        async with mcp_client(server.url, server.token, mode) as client:
            listing = await client.list_tools()
            assert {t.name for t in listing.tools} == set(OPERATIONS)
            claim_tool = next(t for t in listing.tools if t.name == "task_claim")
            assert "request" in claim_tool.input_schema["properties"]
            assert not claim_tool.annotations.read_only_hint
            key = "mcp-" + uuid.uuid4().hex
            payload = {
                "actor": "mcp",
                "request_id": "create",
                "project": key,
                "key": "spec",
                "expected_version": 0,
                "value": 1,
            }
            result = await client.call_tool("state_put", {"request": payload})
            assert not result.is_error and result.structured_content["data"]["version"] == 1
            result = await client.call_tool(
                "state_put", {"request": {**payload, "request_id": "conflict"}}
            )
            assert (
                result.is_error and result.structured_content["error"]["code"] == "version_conflict"
            )
            assert json.loads(result.content[0].text) == result.structured_content
            invalid = await client.call_tool(
                "state_put", {"request": {**payload, "expected_version": -1}}
            )
            assert invalid.is_error
            # No client cache may conceal another client's successful write.
            with RelayFenceClient(server.url, server.token) as http:
                http.call(
                    "state_put",
                    **{**payload, "expected_version": 1, "request_id": "advance", "value": 2},
                )
            latest = await client.call_tool(
                "item_get", {"request": {"project": key, "key": "spec"}}
            )
            assert latest.structured_content["data"]["version"] == 2

    asyncio.run(scenario())


def test_stdio_bridge_and_http_client_share_one_ledger(server):
    async def scenario():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "relayfence", "bridge", "--url", server.url],
            env={"RELAYFENCE_TOKEN": server.token},
        )
        project = uuid.uuid4().hex
        with RelayFenceClient(server.url, server.token) as http:
            http.call(
                "task_create",
                project=project,
                actor="http",
                request_id="create",
                task_id="shared",
                title="Shared",
            )
        async with Client(parameters, cache=None) as stdio:
            result = await stdio.call_tool(
                "task_claim",
                {
                    "request": {
                        "project": project,
                        "actor": "stdio",
                        "request_id": "claim",
                        "task_id": "shared",
                    }
                },
            )
            assert not result.is_error
            assert result.structured_content["data"]["task"]["owner"] == "stdio"
        with RelayFenceClient(server.url, server.token) as http:
            result, status = http.call_raw(
                "task_claim",
                {
                    "project": project,
                    "actor": "http",
                    "request_id": "race",
                    "task_id": "shared",
                },
            )
            assert status == 409 and result["error"]["code"] == "task_busy"

    asyncio.run(scenario())


def test_python_client_reports_auth_and_unknown_outcomes(server):
    with RelayFenceClient(server.url, "wrong") as client:
        result, status = client.call_raw("items_list", {})
        assert status == 401 and result["error"]["code"] == "unauthorized"
    with RelayFenceClient(server.url, server.token) as client:
        assert client.call_raw("../escape", {})[1] == 404


def test_demo_is_a_reproducible_protocol_acceptance_test(tmp_path):
    report = run_demo(tmp_path / "demo")
    assert len(report["checks"]) == 6 and all(c["status"] == "PASS" for c in report["checks"])
    assert report["vendor_clients_tested"] == []
    with pytest.raises(ValueError, match="new"):
        run_demo(tmp_path / "demo")
