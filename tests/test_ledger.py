import json
import sqlite3

import pytest

from relayfence.service import Service
from relayfence.store import Store


def test_compare_and_swap_preserves_immutable_history(ledger):
    one = ledger.ok("state_put", key="spec", expected_version=0, value={"hello": "世界"})
    assert one["version"] == 1
    ledger.error("version_conflict", "state_put", key="spec", expected_version=0, value="lost")
    ledger.ok("state_put", key="spec", expected_version=1, value="next")
    old = ledger.ok("item_get", key="spec", version=1)
    assert old["value"] == {"hello": "世界"}
    assert old["version"] == 1 and old["current_version"] == 2 and not old["fresh"]
    ledger.error("not_found", "item_get", key="spec", version=3)
    ledger.error("not_found", "item_get", key="missing")
    assert len(ledger.ok("events_list")["events"]) == 2


def test_idempotency_includes_operation_payload_actor_and_project(ledger):
    payload = {"key": "spec", "expected_version": 0, "value": 1, "request_id": "same"}
    first = ledger.request("state_put", **payload)
    retry = ledger.request("state_put", **payload)
    assert first["data"] == retry["data"]
    assert not first["replayed"] and retry["replayed"]
    ledger.error("idempotency_conflict", "state_put", **{**payload, "value": 2})
    ledger.error(
        "idempotency_conflict", "task_create", task_id="work", title="Work", request_id="same"
    )
    ledger.ok("state_put", **payload, project="other")
    ledger.ok("state_put", **{**payload, "key": "another"}, actor="other")
    ledger.error("not_found", "item_get", key="another", project="other")


def test_rejected_request_does_not_poison_receipt(ledger):
    payload = {"key": "spec", "expected_version": 1, "value": 2, "request_id": "retry"}
    ledger.error("version_conflict", "state_put", **payload)
    ledger.ok("state_put", key="spec", expected_version=0, value=1)
    assert ledger.ok("state_put", **payload)["version"] == 2


def test_exclusive_claim_and_secret_ownership(ledger):
    lease = ledger.claim(actor="alpha")
    ledger.error("task_busy", "task_claim", task_id="task", actor="beta")
    public = ledger.ok("task_get", task_id="task")
    assert "lease_token" not in public and "lease_hash" not in public
    assert lease["lease_token"] not in json.dumps(ledger.ok("events_list"))
    ledger.error("lease_lost", "task_heartbeat", **{**lease, "actor": "beta"})
    ledger.error(
        "lease_lost", "task_release", **{**lease, "lease_token": "incorrect-token-1234567890"}
    )
    ledger.error(
        "lease_lost",
        "task_complete",
        **{**lease, "fence": 2},
        outputs=[{"key": "out", "expected_version": 0, "value": 1}],
    )
    ledger.complete(lease)
    ledger.error("task_done", "task_claim", task_id="task")


@pytest.mark.parametrize("operation", ["task_heartbeat", "task_release", "task_complete"])
def test_expired_lease_cannot_write_even_without_takeover(ledger, operation):
    lease = ledger.claim(lease_seconds=10)
    ledger.clock.advance(10)  # The exact deadline is expired.
    extra = (
        {"outputs": [{"key": "out", "expected_version": 0, "value": "late"}]}
        if operation == "task_complete"
        else {}
    )
    ledger.error("lease_expired", operation, **lease, **extra)
    assert ledger.ok("task_get", task_id="task")["lease_expired"]
    assert len(ledger.ok("tasks_list", available_only=True)["tasks"]) == 1


def test_takeover_fences_old_owner_even_when_actor_name_is_reused(ledger):
    old = ledger.claim(lease_seconds=1)
    ledger.clock.advance(2)
    new = ledger.ok("task_claim", task_id="task", actor="worker")
    assert new["task"]["fence"] == old["fence"] + 1
    assert new["lease_token"] != old["lease_token"]
    ledger.error("lease_lost", "task_heartbeat", **old)
    ledger.complete(
        {"task_id": "task", "actor": "worker", "fence": 2, "lease_token": new["lease_token"]}
    )


def test_heartbeat_never_shortens_and_release_allows_new_snapshot(ledger):
    ledger.ok("state_put", key="spec", expected_version=0, value=1)
    lease = ledger.claim(inputs=["spec"], lease_seconds=100)
    deadline = ledger.ok("task_get", task_id="task")["expires_at"]
    ledger.clock.advance(1)
    assert ledger.ok("task_heartbeat", **lease, lease_seconds=1)["expires_at"] == deadline
    ledger.clock.advance(1)
    assert ledger.ok("task_heartbeat", **lease, lease_seconds=200)["expires_at"] > deadline
    ledger.ok("state_put", key="spec", expected_version=1, value=2)
    ledger.error(
        "stale_input",
        "task_complete",
        **lease,
        outputs=[{"key": "out", "expected_version": 0, "value": "old"}],
    )
    ledger.ok("task_release", **lease)
    new = ledger.ok("task_claim", task_id="task")
    assert new["inputs"][0]["value"] == 2 and new["task"]["fence"] == 2


def test_dependency_invalidation_is_transitive_and_blocks_downstream(ledger):
    ledger.ok("state_put", key="spec", expected_version=0, value=1)
    api = ledger.claim("api-work", ["spec"])
    ledger.complete(api, "api")
    tests = ledger.claim("test-work", ["api"])
    ledger.complete(tests, "tests")
    assert ledger.ok("item_get", key="tests")["fresh"]
    ledger.ok("state_put", key="spec", expected_version=1, value=2)
    for key in ["api", "tests"]:
        item = ledger.ok("item_get", key=key)
        assert not item["fresh"]
        assert item["stale_dependencies"] == [
            {"key": "spec", "expected_version": 1, "current_version": 2}
        ]
    ledger.ok("task_create", task_id="downstream", title="Downstream", inputs=["tests"])
    ledger.error("stale_input", "task_claim", task_id="downstream")
    assert not ledger.ok("task_get", task_id="test-work")["result_freshness"]["fresh"]


def test_inputs_may_arrive_after_task_creation(ledger):
    ledger.ok("task_create", task_id="task", title="Task", inputs=["future"])
    ledger.error("not_found", "task_claim", task_id="task")
    ledger.ok("state_put", key="future", expected_version=0, value=True)
    assert ledger.ok("task_claim", task_id="task")["inputs"][0]["value"] is True


def test_failed_multi_output_completion_is_atomic(ledger):
    other = ledger.claim("other")
    ledger.complete(other, "occupied")
    lease = ledger.claim()
    before = len(ledger.ok("events_list")["events"])
    ledger.error(
        "version_conflict",
        "task_complete",
        **lease,
        outputs=[
            {"key": "new", "expected_version": 0, "value": "would be written"},
            {"key": "occupied", "expected_version": 0, "value": "wrong version"},
        ],
    )
    ledger.error("not_found", "item_get", key="new")
    assert ledger.ok("task_get", task_id="task")["status"] == "running"
    assert len(ledger.ok("events_list")["events"]) == before
    result = ledger.ok(
        "task_complete",
        **lease,
        outputs=[
            {"key": "new", "expected_version": 0, "value": "ok"},
            {"key": "occupied", "expected_version": 1, "value": "ok"},
        ],
    )
    assert [i["version"] for i in result["artifacts"]] == [1, 2]


def test_cannot_overwrite_a_transitive_input(ledger):
    ledger.complete(ledger.claim("source"), "root")
    ledger.complete(ledger.claim("middle", ["root"]), "middle")
    lease = ledger.claim("cycle", ["middle"])
    ledger.error(
        "dependency_cycle",
        "task_complete",
        **lease,
        outputs=[{"key": "root", "expected_version": 1, "value": "cycle"}],
    )


def test_key_kind_is_permanent(ledger):
    ledger.ok("state_put", key="spec", expected_version=0, value=1)
    lease = ledger.claim()
    ledger.error(
        "kind_conflict",
        "task_complete",
        **lease,
        outputs=[{"key": "spec", "expected_version": 1, "value": 2}],
    )
    ledger.complete(lease)
    ledger.error("kind_conflict", "state_put", key="artifact", expected_version=1, value=2)


def test_reopen_is_revision_checked_and_retains_history(ledger):
    lease = ledger.claim()
    ledger.error("invalid_transition", "task_reopen", task_id="task", expected_revision=2)
    result = ledger.complete(lease)
    revision = result["task"]["revision"]
    ledger.error("revision_conflict", "task_reopen", task_id="task", expected_revision=revision - 1)
    ledger.ok("task_reopen", task_id="task", expected_revision=revision)
    claim = ledger.ok("task_claim", task_id="task")
    ledger.complete(
        {
            "task_id": "task",
            "actor": "worker",
            "fence": claim["task"]["fence"],
            "lease_token": claim["lease_token"],
        },
        version=1,
    )
    assert ledger.ok("item_get", key="artifact", version=1)["current_version"] == 2


def test_project_scoping_and_pagination(ledger):
    for key in ("c", "a", "b"):
        ledger.ok("state_put", key=key, expected_version=0, value=key)
        ledger.ok("task_create", task_id=key, title=key)
    ledger.ok("state_put", project="private", key="hidden", expected_version=0, value="hidden")
    for operation, field in (("items_list", "items"), ("tasks_list", "tasks")):
        first = ledger.ok(operation, limit=2)
        second = ledger.ok(operation, after=first["next_cursor"], limit=2)
        assert len(first[field]) == 2 and len(second[field]) == 1 and second["next_cursor"] is None
    events = ledger.ok("events_list", limit=2)
    assert events["has_more"]
    all_events = events["events"]
    cursor = events["next_cursor"]
    while events["has_more"]:
        events = ledger.ok("events_list", after=cursor, limit=2)
        cursor = events["next_cursor"]
        all_events += events["events"]
    assert len(all_events) == 6
    assert ledger.ok("events_list", after=cursor)["next_cursor"] == cursor


def test_receipts_events_and_live_lease_survive_reopening_database(ledger):
    ledger.ok("task_create", task_id="task", title="Task")
    payload = {"task_id": "task", "actor": "worker", "request_id": "claim"}
    result, _ = ledger.service.call("task_claim", payload)
    service = Service(Store(ledger.store.path, clock=ledger.clock))
    replay, status = service.call("task_claim", payload)
    assert status == 200 and replay["replayed"] and replay["data"] == result["data"]
    assert service.call("events_list", {})[0]["data"]["next_cursor"] == 2


def test_online_backup_and_forward_schema_rejection(ledger, tmp_path):
    ledger.ok("state_put", key="spec", expected_version=0, value={"saved": True})
    backup = tmp_path / "backup.sqlite3"
    ledger.store.backup(backup)
    restored = Service(Store(backup))
    assert restored.call("item_get", {"key": "spec"})[0]["data"]["value"] == {"saved": True}
    for path in (backup, ledger.store.path):
        with pytest.raises(ValueError, match="new file"):
            ledger.store.backup(path)
    with sqlite3.connect(backup) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="schema 99"):
        Store(backup)


@pytest.mark.parametrize(
    "payload",
    [
        {"key": "x", "value": 1, "expected_version": True},
        {"key": "x", "value": 1, "expected_version": -1},
        {"key": "x", "value": 1, "expected_version": "0"},
        {"key": "../x", "value": 1, "expected_version": 0},
        {"key": "x", "value": 1, "expected_version": 0, "unexpected": "secret"},
        {"key": "x", "value": float("nan"), "expected_version": 0},
    ],
)
def test_invalid_requests_are_rejected_without_echoing_input(ledger, payload):
    result = ledger.error("invalid_request", "state_put", **payload)
    assert "secret" not in json.dumps(result)


def test_size_duplicates_and_missing_resources(ledger):
    ledger.error("payload_too_large", "state_put", key="x", expected_version=0, value="a" * 140_000)
    ledger.error("invalid_request", "task_create", task_id="a", title="A", inputs=["same", "same"])
    ledger.ok("task_create", task_id="a", title="A")
    ledger.error("already_exists", "task_create", task_id="a", title="A")
    lease = ledger.ok("task_claim", task_id="a")
    ledger.error(
        "invalid_request",
        "task_complete",
        task_id="a",
        fence=1,
        lease_token=lease["lease_token"],
        outputs=[{"key": "out", "expected_version": 0, "value": 1}] * 2,
    )
    ledger.error("not_found", "task_get", task_id="missing")
    result, status = ledger.service.call("unknown", {})
    assert status == 404 and result["error"]["code"] == "unknown_operation"


def test_dependency_bound_fails_closed(ledger, monkeypatch):
    ledger.ok("state_put", key="root", expected_version=0, value=1)
    ledger.complete(ledger.claim("one", ["root"]), "one")
    monkeypatch.setattr("relayfence.store.MAX_GRAPH_NODES", 1)
    ledger.error("dependency_limit", "item_get", key="one")


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("item_get", {"key": "x", "version": 2**63}),
        ("events_list", {"after": 2**63}),
        ("task_reopen", {"task_id": "x", "expected_revision": 2**63}),
    ],
)
def test_out_of_range_integer_never_reaches_sqlite(ledger, operation, payload):
    ledger.error("invalid_request", operation, **payload)
