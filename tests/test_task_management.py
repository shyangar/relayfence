import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files

import pytest
from test_registry import as_agent, register

from relayfence.service import Service
from relayfence.store import Store


def control(ledger, task_id, action):
    task = ledger.ok("task_get", task_id=task_id)
    return ledger.ok(
        "task_control", task_id=task_id, expected_revision=task["revision"], action=action
    )


def test_pause_revokes_live_lease_and_resume_gets_new_fence(ledger):
    lease = ledger.claim()
    paused = control(ledger, "task", "pause")
    assert paused["status"] == "paused" and paused["owner"] is None
    ledger.error("lease_lost", "task_heartbeat", **lease)
    ledger.error(
        "lease_lost",
        "task_complete",
        **lease,
        outputs=[{"key": "x", "expected_version": 0, "value": 1}],
    )
    ledger.error("invalid_transition", "task_claim", task_id="task")
    control(ledger, "task", "resume")
    claim = ledger.ok("task_claim", task_id="task")
    assert claim["task"]["fence"] == 2


def test_cancel_retry_failure_and_filtered_history(ledger):
    lease = ledger.claim()
    failure = ledger.ok(
        "task_fail", **lease, error_code="adapter_error", message="Executable failed"
    )
    assert failure["status"] == "failed" and failure["failure"]["code"] == "adapter_error"
    control(ledger, "task", "cancel")
    ledger.error("invalid_transition", "task_claim", task_id="task")
    task = control(ledger, "task", "retry")
    assert task["status"] == "ready" and task["failure"] is None
    ledger.ok("task_create", task_id="other", title="Other")
    history = ledger.ok("events_list", subject="task", limit=2)
    assert len(history["events"]) == 2 and history["has_more"]
    rest = ledger.ok("events_list", subject="task", after=history["next_cursor"])
    assert [e["kind"] for e in rest["events"]] == ["task.failed", "task.cancel", "task.retry"]


def test_dependency_wait_snapshot_and_upstream_reopen(ledger):
    parent = ledger.claim("parent")
    ledger.ok("task_create", task_id="child", title="Child", depends_on=["parent"])
    ledger.ok("task_create", task_id="grandchild", title="Grandchild", depends_on=["child"])
    assert ledger.ok("task_get", task_id="child")["display_status"] == "blocked"
    ledger.error("task_blocked", "task_claim", task_id="child")
    ledger.complete(parent, key="spec", value={"contract": 1})
    child = ledger.ok("task_claim", task_id="child")
    assert child["inputs"][0]["key"] == "spec"
    lease = {
        "task_id": "child",
        "fence": child["task"]["fence"],
        "lease_token": child["lease_token"],
    }
    upstream = ledger.ok("task_get", task_id="parent")
    ledger.ok("task_reopen", task_id="parent", expected_revision=upstream["revision"])
    ledger.error(
        "stale_input",
        "task_complete",
        **lease,
        outputs=[{"key": "result", "expected_version": 0, "value": 1}],
    )
    ledger.ok("task_release", **lease)
    parent2 = ledger.ok("task_claim", task_id="parent")
    ledger.complete(
        {
            "task_id": "parent",
            "fence": parent2["task"]["fence"],
            "lease_token": parent2["lease_token"],
        },
        key="spec",
        version=1,
    )
    child2 = ledger.ok("task_claim", task_id="child")
    ledger.complete(
        {
            "task_id": "child",
            "fence": child2["task"]["fence"],
            "lease_token": child2["lease_token"],
        },
        key="result",
    )
    assert ledger.ok("task_get", task_id="grandchild")["display_status"] == "ready"
    upstream = ledger.ok("task_get", task_id="parent")
    ledger.ok("task_reopen", task_id="parent", expected_revision=upstream["revision"])
    assert not ledger.ok("task_get", task_id="child")["result_freshness"]["fresh"]
    assert ledger.ok("task_get", task_id="grandchild")["display_status"] == "blocked"


def test_task_edit_cycles_missing_inputs_search_and_pagination(ledger):
    ledger.ok("task_create", task_id="a", title="Design % API", priority=3, inputs=["spec"])
    ledger.ok("task_create", task_id="b", title="Build", depends_on=["a"])
    ledger.error(
        "dependency_cycle",
        "task_update",
        task_id="a",
        title="Cycle",
        depends_on=["b"],
        expected_revision=1,
    )
    ledger.error("not_found", "task_create", task_id="c", title="Unknown", depends_on=["absent"])
    ledger.error(
        "revision_conflict", "task_update", task_id="a", title="Conflict", expected_revision=2
    )
    page = ledger.ok("tasks_list", status="blocked", limit=1)
    assert page["tasks"][0]["task_id"] == "a" and page["next_cursor"] == "a"
    assert ledger.ok("tasks_list", status="blocked", after="a")["tasks"][0]["task_id"] == "b"
    assert len(ledger.ok("tasks_list", query="% api")["tasks"]) == 1
    assert ledger.ok("tasks_list", status="ready")["tasks"] == []
    ledger.ok("state_put", key="spec", expected_version=0, value={})
    assert ledger.ok("tasks_list", status="ready")["tasks"][0]["task_id"] == "a"
    edited = ledger.ok(
        "task_update",
        task_id="a",
        title="Revised",
        expected_revision=1,
        priority=2,
        due_at=1800000001,
        description="Acceptance criteria",
    )
    assert edited["priority"] == 2 and edited["due_at"] == 1800000001
    lease = ledger.ok("task_claim", task_id="a")
    ledger.error(
        "invalid_transition",
        "task_update",
        task_id="a",
        title="Live",
        expected_revision=lease["task"]["revision"],
    )


def test_two_operator_actions_compare_and_swap(ledger):
    ledger.ok("task_create", task_id="t", title="Work")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda action: ledger.request(
                    "task_control", task_id="t", expected_revision=1, action=action
                ),
                ["pause", "cancel"],
            )
        )
    assert sum(r["ok"] for r in results) == 1
    assert [r["error"]["code"] for r in results if not r["ok"]] == ["revision_conflict"]


def test_expired_work_needs_explicit_retry_and_management_needs_admin(ledger):
    ledger.claim(lease_seconds=1)
    ledger.clock.advance(2)
    assert control(ledger, "task", "retry")["status"] == "ready"
    principal, _ = register(ledger)
    result = as_agent(
        ledger, principal, "task_control", task_id="task", expected_revision=3, action="cancel"
    )
    assert result["error"]["code"] == "admin_required"
    assert as_agent(ledger, principal, "runner_heartbeat", runner_id="local")["ok"]
    assert ledger.ok("runners_list")["runners"][0]["online"]
    ledger.clock.advance(21)
    assert not ledger.ok("runners_list")["runners"][0]["online"]


def test_schema_2_live_claim_survives_migration(tmp_path):
    path = tmp_path / "v2.sqlite3"
    with sqlite3.connect(path) as db:
        for name in ("schema.sql", "migration_2.sql"):
            db.executescript(files("relayfence").joinpath(name).read_text())
        from relayfence.store import digest

        db.execute(
            "INSERT INTO tasks(project,task_id,title,description,inputs,status,owner,lease_hash,expires_at,fence,created_at,updated_at) VALUES ('default','t','Old','','[]','running','worker',?,9999999999,8,1,1)",
            (digest("secret" * 5),),
        )
    store = Store(path)
    response, _ = Service(store).call(
        "task_complete",
        {
            "actor": "worker",
            "request_id": uuid.uuid4().hex,
            "task_id": "t",
            "fence": 8,
            "lease_token": "secret" * 5,
            "outputs": [{"key": "x", "value": 1, "expected_version": 0}],
        },
    )
    assert response["ok"] and response["data"]["task"]["fence"] == 8


@pytest.mark.parametrize(
    "extra",
    [
        {"priority": True},
        {"priority": 4},
        {"due_at": -1},
        {"due_at": float("inf")},
        {"depends_on": ["x", "x"]},
    ],
)
def test_invalid_task_management_fields(ledger, extra):
    ledger.error("invalid_request", "task_create", task_id="t", title="Work", **extra)
