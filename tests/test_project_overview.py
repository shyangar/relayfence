import json

from test_delivery import create_delivery, review
from test_task_management import control


def test_progress_distinguishes_cancelled_expired_review_and_stale(ledger):
    ledger.complete(ledger.claim("done"), key="good")
    ledger.claim("lost", lease_seconds=1)
    ledger.ok("task_create", task_id="cancel", title="Cancelled")
    control(ledger, "cancel", "cancel")
    delivery, _ = create_delivery(ledger)
    ledger.clock.advance(2)
    result = ledger.ok("project_overview")
    assert result["counts"] == {"done": 1, "cancelled": 1, "lease_expired": 1, "awaiting_review": 1}
    assert result["progress"] == {
        "total": 4,
        "eligible": 3,
        "completed": 1,
        "cancelled": 1,
        "stale": 0,
        "percent": 33,
    }
    assert {a["reason"] for a in result["attention"]} == {"lease_expired", "awaiting_review"}
    review(ledger, delivery)
    assert ledger.ok("project_overview")["progress"]["completed"] == 2
    ledger.ok("state_put", key="input", expected_version=0, value=1)
    ledger.complete(ledger.claim("dependent", inputs=["input"]), key="dependent-out")
    ledger.ok("state_put", key="input", expected_version=1, value=2)
    result = ledger.ok("project_overview")
    assert result["progress"]["stale"] == 1
    assert result["progress"]["completed"] == 2
    assert any(a["reason"] == "stale_result" for a in result["attention"])


def test_overview_bounds_attention_and_isolates_projects(ledger):
    for i in range(25):
        ledger.ok("task_create", task_id=f"task-{i}", title=f"Paused {i}")
        control(ledger, f"task-{i}", "pause")
    result = ledger.ok("project_overview")
    assert result["attention_total"] == 25 and len(result["attention"]) == 20
    assert ledger.ok("project_overview", project="other")["progress"]["total"] == 0
    assert "lease_token" not in json.dumps(result)
    assert "description" not in json.dumps(result)


def test_overview_empty_and_all_cancelled(ledger):
    assert ledger.ok("project_overview")["progress"]["percent"] == 0
    ledger.ok("task_create", task_id="gone", title="Gone")
    control(ledger, "gone", "cancel")
    result = ledger.ok("project_overview")
    assert result["progress"]["eligible"] == 0
    assert result["attention_total"] == 0
