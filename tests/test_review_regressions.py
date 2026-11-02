"""Regression coverage for review findings through public/authenticated boundaries."""

import asyncio
import copy
import json

import pytest
from test_meetings import advance, finish, plan, setup
from test_office import as_agent, group, register

from relayfence.worker import report_activity


def delegated(ledger, entries_a, entries_b):
    principal, meeting = setup(ledger)
    finish(ledger, principal, meeting["waiting"][0], plan())
    meeting = advance(ledger)
    for tid, entries in zip(meeting["waiting"], [entries_a, entries_b], strict=True):
        finish(ledger, principal, tid, {"tasks": entries})
    return principal, advance(ledger)


def entry(mid, ident, dependencies):
    return {
        "member_id": mid,
        "id": ident,
        "depends_on": dependencies,
        "title": ident,
        "description": "Implement and verify " + ident,
    }


def test_member_dependencies_are_enforced_across_groups(ledger):
    principal, meeting = delegated(
        ledger,
        [entry("a1", "schema", []), entry("a1", "api", ["schema"])],
        [entry("b1", "test", ["a/api"])],
    )
    assert meeting["stage"] == "executing" and not meeting["error"]
    tasks = {t["title"].split(" · ")[0]: t for t in ledger.ok("tasks_list")["tasks"]}
    work = [ledger.ok("task_get", task_id=tid) for tid in meeting["waiting"]]
    schema, api, test = work
    assert not schema["blockers"]
    assert api["display_status"] == test["display_status"] == "blocked"
    assert schema["task_id"] in api["depends_on"]
    assert api["task_id"] in test["depends_on"]
    finish(ledger, principal, schema["task_id"], {"schema": "actual"})
    assert not ledger.ok("task_get", task_id=api["task_id"])["blockers"]
    claim = as_agent(
        ledger,
        principal,
        "coder",
        "task_claim",
        task_id=api["task_id"],
        selection={"model": "model-a", "parameters": {}},
    )
    assert any(i["key"] == f"tasks/{schema['task_id']}/result" for i in claim["inputs"])
    assert tasks


@pytest.mark.parametrize(
    "entries",
    [
        [entry("a1", "one", ["missing"])],
        [entry("a1", "one", ["two"]), entry("a1", "two", ["one"])],
        [entry("a1", "one", []), entry("a1", "one", [])],
        [entry("a1", "../bad", [])],
        [entry("a1", "one", "not-a-list")],
        [entry("a1", "one", ["one", "one"])],
        [{**entry("a1", "one", []), "title": ""}],
    ],
)
def test_bad_dependency_graph_creates_no_partial_work(ledger, entries):
    _, meeting = delegated(ledger, entries, [entry("b1", "test", [])])
    assert meeting["error"] and meeting["stage"] == "delegating"
    assert len(ledger.ok("tasks_list")["tasks"]) == 3


def test_independent_nodes_are_ready_and_old_ordered_plans_stay_ordered(ledger):
    _, meeting = delegated(ledger, [entry("a1", "independent", [])], [entry("b1", "other", [])])
    assert all(not ledger.ok("task_get", task_id=tid)["blockers"] for tid in meeting["waiting"])


@pytest.mark.parametrize("leader", [None, "c1"])
def test_queued_meeting_retains_prompt_model_and_reasoning(ledger, leader):
    principal = register(
        ledger,
        models=[
            {"id": "model-a", "parameters": {"reasoning_effort": ["medium"]}},
            {"id": "model-b", "parameters": {"reasoning_effort": ["high"]}},
        ],
    )
    g = group("coder")
    g["members"][0].update(prompt="A instructions", parameters={"reasoning_effort": "medium"})
    saved = ledger.ok("office_put", groups=[g], expected_revision=0)
    a = ledger.ok(
        "office_dispatch",
        expected_revision=saved["revision"],
        briefing_id="a",
        title="A",
        description="A goal",
        member_ids=["c1"],
        leader_id=leader,
    )["tasks"][0]
    g = copy.deepcopy(g)
    g["members"][0].update(
        prompt="B instructions", model="model-b", parameters={"reasoning_effort": "high"}
    )
    saved = ledger.ok("office_put", groups=[g], expected_revision=saved["revision"])
    b = ledger.ok(
        "office_dispatch",
        expected_revision=saved["revision"],
        briefing_id="b",
        title="B",
        description="B goal",
        member_ids=["c1"],
        leader_id=leader,
    )["tasks"][0]
    assert a["role_id"] != b["role_id"]
    claim = as_agent(
        ledger,
        principal,
        "coder",
        "task_claim",
        task_id=a["task_id"],
        selection={"model": "model-a", "parameters": {"reasoning_effort": "medium"}},
    )
    assert claim["task"]["execution"]["role"]["instructions"] == "A instructions"
    assert claim["task"]["execution"]["requested"]["model"] == "model-a"
    assert ledger.ok("role_get", role_id=b["role_id"])["instructions"] == "B instructions"


def test_project_switch_does_not_break_background_skilled_planning(ledger, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-home"))
    a, b = tmp_path / "a", tmp_path / "b"
    b.mkdir()
    skill = a / ".codex" / "skills" / "only-a"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: only-a\ndescription: Only A\n---\nAnalyze A", encoding="utf-8"
    )
    ledger.store.set_skill_workspace(a, "default")
    principal = register(ledger)
    g = group("coder")
    g["members"][0]["skills"] = ["only-a"]
    ledger.ok("office_put", groups=[g], expected_revision=0)
    meeting = ledger.ok(
        "office_dispatch",
        expected_revision=1,
        briefing_id="skilled",
        title="A",
        description="A goal",
        member_ids=["c1"],
        leader_id="c1",
    )["meeting"]
    tid = meeting["waiting"][0]
    role = ledger.ok("role_get", role_id=ledger.ok("task_get", task_id=tid)["role_id"])
    claim = as_agent(
        ledger, principal, "coder", "task_claim", task_id=tid, selection=role["selection"]
    )
    as_agent(
        ledger,
        principal,
        "coder",
        "task_complete",
        task_id=tid,
        fence=claim["task"]["fence"],
        lease_token=claim["lease_token"],
        outputs=[
            {
                "key": f"tasks/{tid}/result",
                "expected_version": 0,
                "value": {
                    "details": json.dumps(
                        {"assignments": [{"group_id": "c", "goal": "Implement A"}]}
                    )
                },
            }
        ],
    )
    ledger.store.set_skill_workspace(b, "other-project")
    result = advance(ledger)
    assert result["stage"] == "delegating" and not result["error"]
    assert ledger.ok("skill_list", project="other-project")["workspace"] == str(b)
    assert ledger.ok("skill_list")["workspace"] == str(a)


def test_activity_requires_current_lease_and_is_idempotent(ledger):
    principal, _ = setup(ledger)
    task = ledger.ok("tasks_list")["tasks"][0]
    claimed = as_agent(
        ledger,
        principal,
        "coder",
        "task_claim",
        task_id=task["task_id"],
        selection={"model": "model-a", "parameters": {}},
    )
    payload = dict(
        project="default",
        actor="coder",
        request_id="activity",
        task_id=task["task_id"],
        fence=claimed["task"]["fence"],
        lease_token=claimed["lease_token"],
        entries=[{"kind": "command", "text": "pytest"}],
    )
    result, code = ledger.service.call("task_activity", payload, principal)
    assert code == 200 and result["data"]["recorded"] == 1
    assert ledger.service.call("task_activity", payload, principal)[0]["replayed"]
    assert len(ledger.ok("task_activity_list")["entries"]) == 1
    missing = {k: v for k, v in payload.items() if k not in ("fence", "lease_token")}
    missing["request_id"] = "missing"
    assert (
        ledger.service.call("task_activity", missing, principal)[0]["error"]["code"]
        == "lease_required"
    )
    stolen = register(ledger, name="other")
    wrong = {**payload, "actor": "other", "request_id": "wrong"}
    assert ledger.service.call("task_activity", wrong, stolen)[0]["error"]["code"] == "lease_lost"
    paused = ledger.ok("task_get", task_id=task["task_id"])
    ledger.ok(
        "task_control",
        task_id=task["task_id"],
        expected_revision=paused["revision"],
        action="pause",
    )
    assert (
        ledger.service.call("task_activity", {**payload, "request_id": "stale"}, principal)[0][
            "error"
        ]["code"]
        == "lease_lost"
    )


def test_activity_retries_exact_failed_batch_before_new_events(tmp_path):
    path = tmp_path / "codex-events.jsonl"

    def events(count):
        path.write_text(
            "".join(
                json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": str(n)}}
                )
                + "\n"
                for n in range(count)
            ),
            encoding="utf-8",
        )

    events(1)
    attempts = []

    async def run():
        async def report(entries):
            attempts.append([e["text"] for e in entries])
            if len(attempts) == 1:
                events(3)
                raise OSError("unknown response")
            if len(attempts) == 3:
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await report_activity(tmp_path, report, 0.1)

    asyncio.run(run())
    assert attempts == [["0"], ["0"], ["1", "2"]]
