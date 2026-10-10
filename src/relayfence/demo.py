"""Real protocol demonstration. Client labels are roles, not vendor integration claims."""

import asyncio
import json
import time
import uuid
from pathlib import Path

from .cli import init_directory, read_token
from .client import mcp_client
from .runner import LocalServer


async def tool(client, operation, actor="planner", **payload):
    request = {"project": "demo", **payload}
    if operation not in ("item_get", "items_list", "task_get", "tasks_list", "events_list"):
        request = {"actor": actor, "request_id": str(uuid.uuid4()), **request}
    result = await client.call_tool(operation, {"request": request})
    envelope = result.structured_content
    if not isinstance(envelope, dict) or result.is_error == envelope.get("ok"):
        raise RuntimeError(f"Invalid MCP result for {operation}")
    return envelope


def require(result, code=None):
    if code is None and not result["ok"]:
        raise RuntimeError(f"Demo failed: {result}")
    if code is not None and (result["ok"] or result["error"]["code"] != code):
        raise RuntimeError(f"Expected {code}, got {result}")
    return result.get("data")


def lease_args(claim):
    return {
        "task_id": claim["task"]["task_id"],
        "fence": claim["task"]["fence"],
        "lease_token": claim["lease_token"],
    }


async def scenario(url, token, record):
    async with (
        mcp_client(url, token) as planner,
        mcp_client(url, token) as worker_a,
        mcp_client(url, token, "legacy") as worker_b,
    ):
        require(
            await tool(
                planner,
                "state_put",
                key="api/spec",
                expected_version=0,
                value={"route": "/login", "auth": "session-cookie"},
            )
        )
        require(
            await tool(
                planner,
                "task_create",
                task_id="implement-login",
                title="Implement login",
                inputs=["api/spec"],
            )
        )
        claims = await asyncio.gather(
            tool(worker_a, "task_claim", actor="worker-a", task_id="implement-login"),
            tool(worker_b, "task_claim", actor="worker-b", task_id="implement-login"),
        )
        if sum(r["ok"] for r in claims) != 1:
            raise RuntimeError("Claim race did not have exactly one winner")
        index = 0 if claims[0]["ok"] else 1
        winner = worker_a if index == 0 else worker_b
        actor = "worker-a" if index == 0 else "worker-b"
        claim = require(claims[index])
        require(claims[1 - index], "task_busy")
        record(
            "exclusive-claim",
            "PASS",
            "Two independent MCP clients raced; exactly one received a lease.",
        )

        require(
            await tool(
                planner,
                "state_put",
                key="api/spec",
                expected_version=1,
                value={"route": "/login", "auth": "short-lived-token"},
            )
        )
        require(
            await tool(
                winner,
                "task_complete",
                actor=actor,
                **lease_args(claim),
                outputs=[
                    {
                        "key": "build/login",
                        "expected_version": 0,
                        "value": {"review": "based on old cookie contract"},
                    }
                ],
            ),
            "stale_input",
        )
        record(
            "stale-input",
            "PASS",
            "Publishing against api/spec@1 was rejected after the contract changed to @2.",
        )
        require(await tool(winner, "task_release", actor=actor, **lease_args(claim)))
        claim = require(await tool(winner, "task_claim", actor=actor, task_id="implement-login"))
        complete_payload = {
            "project": "demo",
            "actor": actor,
            "request_id": str(uuid.uuid4()),
            **lease_args(claim),
            "outputs": [
                {
                    "key": "build/login",
                    "expected_version": 0,
                    "value": {"review": "recomputed against token contract"},
                }
            ],
        }
        result = await winner.call_tool("task_complete", {"request": complete_payload})
        require(result.structured_content)
        retry = await winner.call_tool("task_complete", {"request": complete_payload})
        require(retry.structured_content)
        if not retry.structured_content["replayed"]:
            raise RuntimeError("Completion replay was not recognized")
        record(
            "idempotent-retry",
            "PASS",
            "Repeating a successful completion returned its saved receipt; artifact stayed at version 1.",
        )

        require(
            await tool(
                planner,
                "state_put",
                key="api/spec",
                expected_version=2,
                value={"route": "/login", "auth": "passkey"},
            )
        )
        artifact = require(await tool(planner, "item_get", key="build/login"))
        if artifact["fresh"]:
            raise RuntimeError("Derived artifact was not invalidated")
        record(
            "artifact-freshness",
            "PASS",
            "The published login artifact became stale after its dependency changed again.",
        )

        require(
            await tool(
                planner, "task_create", task_id="review-login", title="Lease takeover example"
            )
        )
        old = require(
            await tool(
                worker_a, "task_claim", actor="worker-a", task_id="review-login", lease_seconds=1
            )
        )
        await asyncio.sleep(max(0, old["task"]["expires_at"] - time.time()) + 0.15)
        new = require(await tool(worker_b, "task_claim", actor="worker-b", task_id="review-login"))
        require(
            await tool(
                worker_a,
                "task_complete",
                actor="worker-a",
                **lease_args(old),
                outputs=[
                    {
                        "key": "review/login",
                        "expected_version": 0,
                        "value": "late worker result",
                    }
                ],
            ),
            "lease_lost",
        )
        require(
            await tool(
                worker_b,
                "task_complete",
                actor="worker-b",
                **lease_args(new),
                outputs=[
                    {
                        "key": "review/login",
                        "expected_version": 0,
                        "value": "replacement worker result",
                    }
                ],
            )
        )
        record(
            "fenced-takeover",
            "PASS",
            "A replacement took fence 2; the old fence 1 worker could no longer publish.",
        )
        return complete_payload


async def verify_restart(url, token, payload, record):
    async with mcp_client(url, token) as client:
        result = (await client.call_tool("task_complete", {"request": payload})).structured_content
        require(result)
        if not result["replayed"]:
            raise RuntimeError("Receipt did not survive process restart")
        current = require(await tool(client, "item_get", key="build/login"))
        if current["version"] != 1 or current["fresh"]:
            raise RuntimeError("Restart lost ledger state")
        events = require(await tool(client, "events_list", limit=200))
        record(
            "crash-recovery",
            "PASS",
            "After forcibly terminating and restarting the service, receipts, versions and events survived.",
        )
        return len(events["events"])


def run_demo(directory: Path | None = None):
    directory = directory or Path(".relayfence") / ("demo-" + uuid.uuid4().hex[:10])
    if directory.exists():
        raise ValueError("Demo directory must be new so previous data is never overwritten")
    directory = init_directory(directory)
    token = read_token(directory / "access.token")
    checks = []

    def record(name, status, detail):
        checks.append({"check": name, "status": status, "detail": detail})
        print(f"[{status}] {name}: {detail}", flush=True)

    print("RelayFence demo: real HTTP MCP clients, no model calls or API keys.", flush=True)
    server = LocalServer(directory, token)
    try:
        server.start()
        payload = asyncio.run(scenario(server.url, token, record))
        server.stop(kill=True)
        server.start()
        events = asyncio.run(verify_restart(server.url, token, payload, record))
    finally:
        server.stop()
    report = {
        "checks": checks,
        "events": events,
        "vendor_clients_tested": [],
        "note": "Protocol clients are not live Codex, ZCode or DSH sessions.",
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nAll {len(checks)} checks passed. Report: {directory / 'report.json'}")
    print(f'Inspect the saved run: relayfence serve --directory "{directory}"')
    return report
