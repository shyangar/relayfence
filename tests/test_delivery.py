import json

from test_meetings import advance, finish, plan, setup

from relayfence.service import Service
from relayfence.store import Store


def create_delivery(ledger, task_id="delivery"):
    ledger.ok("task_create", task_id=task_id, title="Implement change", review_required=True)
    claim = ledger.ok("task_claim", task_id=task_id, actor="builder")
    lease = dict(
        task_id=task_id,
        actor="builder",
        fence=claim["task"]["fence"],
        lease_token=claim["lease_token"],
    )
    return ledger.complete(lease, key="result/" + task_id)["task"], lease


def review(ledger, task, action="approve", **extra):
    return ledger.ok(
        "task_review",
        task_id=task["task_id"],
        actor="reviewer",
        expected_revision=task["revision"],
        action=action,
        note="Verified the result" if action == "approve" else "Add the missing validation",
        evidence=["pytest: checks passed"],
        **extra,
    )


def test_pending_delivery_blocks_dependents_and_survives_restart(ledger):
    task, lease = create_delivery(ledger)
    assert task["status"] == "done" and task["display_status"] == "awaiting_review"
    ledger.ok("task_create", task_id="next", title="Follow up", depends_on=["delivery"])
    dependent = ledger.ok("task_get", task_id="next")
    assert dependent["blockers"] == [
        {"kind": "review", "key": "delivery", "reason": "awaiting_review"}
    ]
    ledger.error("task_blocked", "task_claim", task_id="next")
    reopened = Store(ledger.store.path, clock=ledger.clock)
    assert (
        Service(reopened).call("task_get", {"task_id": "delivery"})[0]["data"]["review"]["status"]
        == "pending"
    )
    ledger.error(
        "independent_review_required",
        "task_review",
        task_id="delivery",
        actor="builder",
        expected_revision=task["revision"],
        action="approve",
        note="Self review",
    )
    accepted = review(ledger, task, request_id="accept")
    assert accepted["review"]["status"] == "approved" and accepted["display_status"] == "done"
    assert not ledger.ok("task_get", task_id="next")["blockers"]
    ledger.ok("task_claim", task_id="next")
    assert review(ledger, task, request_id="accept") == accepted
    ledger.error(
        "revision_conflict",
        "task_review",
        task_id="delivery",
        actor="reviewer",
        expected_revision=task["revision"],
        action="reject",
        note="old",
    )


def test_rejection_is_paused_and_next_attempt_retains_prior_review(ledger):
    task, _ = create_delivery(ledger)
    rejected = review(ledger, task, "reject")
    assert rejected["status"] == "paused" and "missing validation" in rejected["description"]
    assert rejected["review"]["status"] == "rejected"
    ledger.error("invalid_transition", "task_claim", task_id="delivery")
    resumed = ledger.ok(
        "task_control", task_id="delivery", expected_revision=rejected["revision"], action="resume"
    )
    claimed = ledger.ok("task_claim", task_id="delivery", actor="builder")
    lease = dict(
        task_id="delivery",
        actor="builder",
        fence=claimed["task"]["fence"],
        lease_token=claimed["lease_token"],
    )
    second = ledger.complete(lease, key="result/delivery", version=1)["task"]
    assert second["fence"] == 2 and second["review"]["status"] == "pending"
    assert resumed["status"] == "ready"
    history = ledger.ok("task_inspect", task_id="delivery", limit=1)
    assert history["attempts"][0]["fence"] == 2 and history["attempts"][0]["status"] == "completed"
    assert history["next_before"]
    older = ledger.ok("task_inspect", task_id="delivery", before=history["next_before"], limit=1)
    assert older["attempts"][0]["fence"] == 1 and older["next_before"] is None
    assert {r["status"] for r in history["reviews"]} == {"pending", "rejected"}
    assert lease["lease_token"] not in json.dumps(history)


def test_stale_delivery_cannot_be_accepted(ledger):
    ledger.ok("state_put", key="spec", expected_version=0, value=1)
    ledger.ok("task_create", task_id="task", title="Build", inputs=["spec"], review_required=True)
    claim = ledger.ok("task_claim", task_id="task", actor="builder")
    task = ledger.complete(
        dict(
            task_id="task",
            actor="builder",
            fence=claim["task"]["fence"],
            lease_token=claim["lease_token"],
        )
    )["task"]
    ledger.ok("state_put", key="spec", expected_version=1, value=2)
    ledger.error(
        "stale_input",
        "task_review",
        task_id="task",
        actor="reviewer",
        expected_revision=task["revision"],
        action="approve",
        note="Checked",
    )
    assert review(ledger, task, "reject")["status"] == "paused"


def test_attempt_history_distinguishes_failure_takeover_and_cancellation(ledger):
    lease = ledger.claim(lease_seconds=1)
    ledger.clock.advance(2)
    assert ledger.ok("task_inspect", task_id="task")["attempts"][0]["status"] == "lease_expired"
    second = ledger.ok("task_claim", task_id="task", actor="replacement")
    inspect = ledger.ok("task_inspect", task_id="task")
    assert [a["status"] for a in inspect["attempts"]] == ["running", "superseded"]
    ledger.ok(
        "task_fail",
        task_id="task",
        actor="replacement",
        fence=second["task"]["fence"],
        lease_token=second["lease_token"],
        error_code="model_error",
        message="Quota exhausted",
    )
    failed = ledger.ok("task_get", task_id="task")
    ledger.ok("task_control", task_id="task", expected_revision=failed["revision"], action="retry")
    third = ledger.ok("task_claim", task_id="task")
    ledger.ok(
        "task_control", task_id="task", expected_revision=third["task"]["revision"], action="cancel"
    )
    attempts = ledger.ok("task_inspect", task_id="task")["attempts"]
    assert [a["status"] for a in attempts] == ["cancel", "failed", "superseded"]
    assert attempts[1]["outcome"]["message"] == "Quota exhausted"
    assert lease["lease_token"] not in json.dumps(attempts)


def test_review_controls_and_filters_are_strict(ledger):
    task, _ = create_delivery(ledger)
    assert len(ledger.ok("tasks_list", status="awaiting_review")["tasks"]) == 1
    assert not ledger.ok("tasks_list", status="done")["tasks"]
    ledger.error(
        "invalid_request",
        "task_review",
        task_id="delivery",
        expected_revision=task["revision"],
        action="approve",
        note=" ",
    )
    review(ledger, task)
    latest = ledger.ok("task_get", task_id="delivery")
    ledger.error(
        "invalid_transition",
        "task_review",
        task_id="delivery",
        actor="other",
        expected_revision=latest["revision"],
        action="approve",
        note="again",
    )
    assert ledger.ok("tasks_list", status="done")["tasks"][0]["task_id"] == "delivery"
    ledger.ok("task_create", task_id="plain", title="No review")
    ledger.error(
        "invalid_transition",
        "task_review",
        task_id="plain",
        expected_revision=1,
        action="reject",
        note="not a delivery",
    )
    assert not ledger.ok("task_inspect", task_id="plain")["attempts"]


def test_task_edit_preserves_existing_review_policy_when_omitted(ledger):
    ledger.ok("task_create", task_id="task", title="Work", review_required=True)
    edited = ledger.ok("task_update", task_id="task", title="Renamed", expected_revision=1)
    assert edited["review_required"]
    edited = ledger.ok(
        "task_update",
        task_id="task",
        title="Explicit opt out",
        expected_revision=edited["revision"],
        review_required=False,
    )
    assert not edited["review_required"]


def test_delivery_review_gates_meeting_completion_and_team_workload(ledger):
    principal, meeting = setup(ledger)
    with ledger.store.connection() as db:
        row = db.execute("SELECT definition FROM meetings WHERE meeting_id='meeting'").fetchone()
        definition = json.loads(row[0])
        definition["review_required"] = True
        db.execute(
            "UPDATE meetings SET definition=? WHERE meeting_id='meeting'", (json.dumps(definition),)
        )
    finish(ledger, principal, meeting["waiting"][0], plan())
    meeting = advance(ledger)
    for tid in meeting["waiting"]:
        task = ledger.ok("task_get", task_id=tid)
        member = "a1" if "本组ID：a" in task["description"] else "b1"
        finish(
            ledger,
            principal,
            tid,
            {
                "tasks": [
                    {
                        "id": "build",
                        "depends_on": [],
                        "member_id": member,
                        "title": "Implement",
                        "description": "Implement and verify",
                    }
                ]
            },
        )
    meeting = advance(ledger)
    assert meeting["stage"] == "executing"
    for tid in meeting["waiting"]:
        finish(ledger, principal, tid, {"summary": "implemented"})
    assert advance(ledger)["stage"] == "executing"
    groups = ledger.ok("team_groups_list")["groups"]
    assert sum(w["awaiting_review"] for g in groups for w in g["workload"]) == 2
    for tid in meeting["waiting"]:
        task = ledger.ok("task_get", task_id=tid)
        review(ledger, task)
    assert advance(ledger)["stage"] == "done"


def test_review_is_administrative_even_for_producing_agent(ledger):
    principal, meeting = setup(ledger)
    result, code = ledger.service.call(
        "task_review",
        dict(
            project="default",
            actor="coder",
            request_id="no",
            task_id=meeting["waiting"][0],
            expected_revision=1,
            action="approve",
            note="checked",
        ),
        principal,
    )
    assert code == 403 and result["error"]["code"] == "admin_required"


def test_rejected_review_rolls_back_when_feedback_cannot_fit(ledger):
    task, _ = create_delivery(ledger)
    with ledger.store.connection() as db:
        db.execute("UPDATE tasks SET description=? WHERE task_id='delivery'", ("x" * 7999,))
    ledger.error(
        "description_limit",
        "task_review",
        task_id="delivery",
        actor="reviewer",
        expected_revision=task["revision"],
        action="reject",
        note="Please revise",
    )
    current = ledger.ok("task_get", task_id="delivery")
    assert current["status"] == "done" and current["review"]["status"] == "pending"
    assert current["revision"] == task["revision"]


def test_handoff_input_limit_does_not_partially_transfer_work(ledger):
    _, meeting = setup(ledger)
    tid = meeting["waiting"][0]
    with ledger.store.connection() as db:
        db.execute(
            "UPDATE tasks SET inputs=? WHERE task_id=?",
            (json.dumps(["input/" + str(i) for i in range(32)]), tid),
        )
    task = ledger.ok("task_get", task_id=tid)
    ledger.error(
        "input_limit",
        "office_transfer",
        task_id=tid,
        expected_revision=task["revision"],
        member_id="b1",
        note="Review retained files",
    )
    assert ledger.ok("task_get", task_id=tid)["revision"] == task["revision"]
    assert not ledger.ok("task_inspect", task_id=tid)["handoffs"]
