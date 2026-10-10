import asyncio
import uuid

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from relayfence.cli import init_directory
from relayfence.client import RelayFenceClient, mcp_client
from relayfence.runner import LocalServer


@pytest.fixture(scope="module")
def role_server(tmp_path_factory):
    directory = init_directory(tmp_path_factory.mktemp("role-server"))
    with LocalServer(directory, (directory / "access.token").read_text().strip()) as server:
        yield server


@pytest.mark.parametrize("transport", ["auto", "legacy", "stdio"])
def test_instance_identity_survives_each_mcp_transport_and_blocks_spoofing(role_server, transport):
    import sys

    project = uuid.uuid4().hex
    server = role_server
    with RelayFenceClient(server.url, server.token) as admin:
        base = {"project": project, "actor": "admin", "request_id": "register"}
        admin.call(
            "agent_put",
            **base,
            agent_id="reviewer",
            expected_revision=0,
            label="Reviewer",
            runtime="custom",
            models=[{"id": "m1"}],
        )
        token = admin.call(
            "agent_token_rotate",
            **{**base, "request_id": "token"},
            agent_id="reviewer",
            expected_revision=1,
        )["data"]["token"]
        admin.call(
            "role_put",
            **{**base, "request_id": "role"},
            role_id="review",
            expected_revision=0,
            label="Review",
            agent_id="reviewer",
            selection={"model": "m1"},
        )
        admin.call(
            "task_create",
            **{**base, "request_id": "task"},
            task_id="t",
            title="Review",
            role_id="review",
        )

    async def scenario():
        if transport == "stdio":
            session = Client(
                StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "relayfence", "bridge", "--url", server.url],
                    env={"RELAYFENCE_TOKEN": token},
                ),
                cache=None,
            )
        else:
            session = mcp_client(server.url, token, transport)
        async with session as client:
            listing = await client.list_tools()
            assert (
                "ctx"
                not in next(t for t in listing.tools if t.name == "task_claim").input_schema[
                    "properties"
                ]
            )
            request = {
                "project": project,
                "actor": "reviewer",
                "request_id": "claim",
                "task_id": "t",
                "selection": {"model": "m1"},
            }
            denied = await client.call_tool(
                "task_claim", {"request": {**request, "actor": "someone-else"}}
            )
            assert denied.structured_content["error"]["code"] == "actor_mismatch"
            denied = await client.call_tool(
                "task_get", {"request": {"project": "other", "task_id": "t"}}
            )
            assert denied.structured_content["error"]["code"] == "forbidden"
            result = await client.call_tool("task_claim", {"request": request})
            assert not result.is_error, result
            assert (
                result.structured_content["data"]["task"]["execution"]["requested"]["model"] == "m1"
            )
            denied = await client.call_tool(
                "role_put",
                {
                    "request": {
                        "project": project,
                        "actor": "reviewer",
                        "request_id": "elevate",
                        "role_id": "review",
                        "expected_revision": 1,
                        "label": "Changed",
                        "agent_id": "reviewer",
                    }
                },
            )
            assert denied.structured_content["error"]["code"] == "admin_required"

    asyncio.run(scenario())
    with RelayFenceClient(server.url, token) as worker:
        assert worker.call("task_get", project=project, task_id="t")["data"]["owner"] == "reviewer"
        with RelayFenceClient(server.url, server.token) as admin:
            admin.call(
                "agent_token_rotate",
                **{**base, "request_id": "revoke"},
                agent_id="reviewer",
                expected_revision=2,
            )
        assert worker.call_raw("task_get", {"project": project, "task_id": "t"})[1] == 401
