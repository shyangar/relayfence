import json
import sys
from concurrent.futures import Future
from threading import RLock
from types import SimpleNamespace

import pytest
from test_office import as_agent, group, register
from test_worker import LocalClient

from relayfence.local_task_control import control, control_meeting
from relayfence.member_chat import MemberChat
from relayfence.store import Store
from relayfence.worker import run_task


def test_real_subprocess_pipeline_receives_context_and_completes_work(ledger, tmp_path):
    principal, _ = setup(ledger)
    script = tmp_path / "meeting_worker.py"
    script.write_text(
        """import json,sys
from pathlib import Path
r=json.loads(sys.stdin.buffer.read())
t=r['task']; title=t['title']
assert r['inputs'] and r['selection']['model']=='model-a'
if '主导者拆解' in title:
    value={'assignments':[{'group_id':'a','goal':'Build'},{'group_id':'b','goal':'Test'}]}
elif '组长分工' in title:
    gid=t['description'].split('本组ID：',1)[1].splitlines()[0]
    member=next(g['members'][0]['id'] for g in r['inputs'][0]['value']['groups'] if g['id']==gid)
    value={'tasks':[{'member_id':member,'title':'Write marker','description':'Write and verify marker'}]}
else:
    Path(t['task_id'].replace('/','-')+'.txt').write_text('verified')
    value={'verified':True}
print(json.dumps({'outputs':[{'key':'tasks/'+t['task_id']+'/result','expected_version':0,'value':{'summary':'Local subprocess result','details':json.dumps(value)}}]}))
""",
        encoding="utf-8",
    )
    config = tmp_path / "worker.json"
    config.write_text(
        json.dumps(
            {
                "agent_id": "coder",
                "command": [sys.executable, str(script), "{model}"],
                "cwd": str(tmp_path),
                "timeout_seconds": 10,
            }
        ),
        encoding="utf-8",
    )
    client = LocalClient(ledger, principal)
    runs = 0
    for _ in range(10):
        meeting = advance(ledger)
        if meeting["stage"] == "done":
            break
        available = [
            t
            for t in ledger.ok("tasks_list")["tasks"]
            if t["status"] == "ready" and not t["blockers"]
        ]
        assert available, meeting
        task = available[0]
        run_task(client, config, task["task_id"], tmp_path / f"run-{runs}")
        runs += 1
    assert advance(ledger)["stage"] == "done" and runs == 5
    assert len(list(tmp_path.glob("meeting-*.txt"))) == 2


def setup(ledger, mode="execution"):
    principal = register(ledger)
    groups = [group("coder", prefix="a"), group("coder", prefix="b")]
    ledger.ok("office_put", groups=groups, expected_revision=0)
    created = ledger.ok(
        "office_dispatch",
        expected_revision=1,
        briefing_id="meeting",
        title="Feature",
        description="Implement and test feature",
        member_ids=["a1", "b1"],
        leader_id="a1",
        mode=mode,
    )
    return principal, created["meeting"]


def finish(ledger, principal, task_id, details):
    task = ledger.ok("task_get", task_id=task_id)
    claimed = as_agent(
        ledger,
        principal,
        "coder",
        "task_claim",
        project="default",
        task_id=task_id,
        selection={"model": "model-a", "parameters": {}},
    )
    return as_agent(
        ledger,
        principal,
        "coder",
        "task_complete",
        project="default",
        task_id=task_id,
        fence=claimed["task"]["fence"],
        lease_token=claimed["lease_token"],
        outputs=[
            {
                "key": f"tasks/{task_id}/result",
                "expected_version": task["outputs"][0]["version"] if task["outputs"] else 0,
                "value": {"summary": "actual result", "details": json.dumps(details)},
            }
        ],
    )


def advance(ledger):
    ledger.store.advance_meetings("default")
    return ledger.ok("meetings_list")["meetings"][0]


def plan():
    return {
        "assignments": [
            {"group_id": "a", "goal": "Build feature"},
            {"group_id": "b", "goal": "Test feature"},
        ]
    }


def test_execution_is_hierarchical_persistent_and_idempotent(ledger):
    principal, meeting = setup(ledger)
    assert len(ledger.ok("tasks_list")["tasks"]) == 1
    assert advance(ledger)["stage"] == "planning"
    finish(ledger, principal, meeting["waiting"][0], plan())
    meeting = advance(ledger)
    assert meeting["stage"] == "delegating"
    assert len(meeting["waiting"]) == 2
    for mid, tid in zip(["a1", "b1"], meeting["waiting"], strict=True):
        finish(
            ledger,
            principal,
            tid,
            {
                "tasks": [
                    {
                        "member_id": mid,
                        "title": "Work",
                        "description": "Concrete acceptance criteria",
                    }
                ]
            },
        )
    meeting = advance(ledger)
    assert meeting["stage"] == "executing"
    assert len(ledger.ok("tasks_list")["tasks"]) == 5
    advance(ledger)
    assert len(ledger.ok("tasks_list")["tasks"]) == 5
    for tid in meeting["waiting"]:
        finish(ledger, principal, tid, {"completed": True})
    assert advance(ledger)["stage"] == "done"
    restored = Store(ledger.store.path)
    restored.advance_meetings("default")
    assert len(ledger.ok("meetings_list")["assignments"]) == 5
    assert ledger.ok("meetings_list", project="other") == {
        "meetings": [],
        "assignments": [],
        "assignments_truncated": False,
    }


def test_revision_keeps_objective_history_and_rejects_parallel_branches(ledger):
    principal, meeting = setup(ledger)
    ledger.error(
        "iteration_active",
        "meeting_revision",
        meeting_id="meeting",
        briefing_id="rev2",
        changes="Add filter",
    )
    finish(ledger, principal, meeting["waiting"][0], plan())
    meeting = advance(ledger)
    for mid, tid in zip(["a1", "b1"], meeting["waiting"], strict=True):
        finish(
            ledger,
            principal,
            tid,
            {"tasks": [{"member_id": mid, "title": "Work", "description": "Implement"}]},
        )
    meeting = advance(ledger)
    for tid in meeting["waiting"]:
        finish(ledger, principal, tid, {"done": True})
    assert advance(ledger)["stage"] == "done"
    last = ledger.ok("task_get", task_id=meeting["waiting"][-1])
    ledger.ok("task_reopen", task_id=last["task_id"], expected_revision=last["revision"])
    ledger.error(
        "iteration_active",
        "meeting_revision",
        meeting_id="meeting",
        briefing_id="rev2",
        changes="Add filter",
    )
    finish(ledger, principal, last["task_id"], {"done": True})
    revised = ledger.ok(
        "meeting_revision",
        meeting_id="meeting",
        briefing_id="rev2",
        changes="Add filter",
        request_id="revision-request",
    )
    assert revised["meeting"]["root_id"] == "meeting" and revised["meeting"]["iteration"] == 2
    context = ledger.ok("item_get", key=revised["meeting"]["context"])
    assert context["value"]["changes"] == "Add filter"
    assert context["value"]["goal"] == "Implement and test feature"
    assert context["value"]["previous_results"] and context["dependencies"]
    assert ledger.request(
        "meeting_revision",
        meeting_id="meeting",
        briefing_id="rev2",
        changes="Add filter",
        request_id="revision-request",
    )["replayed"]
    ledger.error(
        "newer_iteration",
        "meeting_revision",
        meeting_id="meeting",
        briefing_id="rev3",
        changes="Other change",
    )
    assert len(ledger.ok("tasks_list")["tasks"]) == 6


def test_live_activity_shows_published_results_and_excludes_cancelled_work(ledger):
    principal, meeting = setup(ledger)
    tid = meeting["waiting"][0]
    activity = ledger.ok("office_activity")["members"]
    assert activity[0]["task"]["task_id"] == tid and activity[1]["task"] is None
    finish(ledger, principal, tid, plan())
    activity = ledger.ok("office_activity")["members"]
    assert activity[0]["messages"][0]["text"] == "actual result"
    assert activity[0]["messages"][0]["role"] == "assistant"
    meeting = advance(ledger)
    for task_id in meeting["waiting"]:
        task = ledger.ok("task_get", task_id=task_id)
        ledger.ok(
            "task_control", task_id=task_id, expected_revision=task["revision"], action="cancel"
        )
    assert all(
        m["task"] is None or m["task"]["status"] != "cancelled"
        for m in ledger.ok("office_activity")["members"]
    )
    assert ledger.ok("office_activity", project="other")["members"] == []


def test_discussion_reassigns_then_waits_for_explicit_execution(ledger):
    principal, meeting = setup(ledger, "discussion")
    finish(ledger, principal, meeting["waiting"][0], plan())
    meeting = advance(ledger)
    assert meeting["stage"] == "reviewing"
    for i, tid in enumerate(meeting["waiting"]):
        finish(
            ledger,
            principal,
            tid,
            {"can_complete": bool(i), "reason": "Need a specialist" if not i else "Can test"},
        )
    meeting = advance(ledger)
    assert meeting["stage"] == "planning" and meeting["round"] == 2
    task = ledger.ok("task_get", task_id=meeting["waiting"][0])
    assert len(task["depends_on"]) == 2
    finish(ledger, principal, task["task_id"], plan())
    meeting = advance(ledger)
    for tid in meeting["waiting"]:
        finish(ledger, principal, tid, {"can_complete": True, "reason": "Scope is now feasible"})
    meeting = advance(ledger)
    assert meeting["stage"] == "ready_for_execution"
    assert advance(ledger)["stage"] == "ready_for_execution"
    meeting = ledger.ok("meeting_execute", meeting_id=meeting["id"])
    assert meeting["stage"] == "delegating"
    ledger.error("invalid_transition", "meeting_execute", meeting_id=meeting["id"])


def test_negotiation_is_bounded(ledger):
    principal, meeting = setup(ledger, "discussion")
    for _ in range(3):
        finish(ledger, principal, meeting["waiting"][0], plan())
        meeting = advance(ledger)
        for tid in meeting["waiting"]:
            finish(
                ledger, principal, tid, {"can_complete": False, "reason": "Missing required skill"}
            )
        meeting = advance(ledger)
    assert meeting["stage"] == "needs_attention"
    assert meeting["error"]
    ledger.error("invalid_transition", "meeting_execute", meeting_id=meeting["id"])


def test_whole_meeting_pause_blocks_claims_and_preserves_manual_pause(ledger):
    principal, meeting = setup(ledger)
    finish(ledger, principal, meeting["waiting"][0], plan())
    meeting = advance(ledger)
    first, second = meeting["waiting"]
    one = ledger.ok("task_get", task_id=first)
    ledger.ok("task_control", task_id=first, expected_revision=one["revision"], action="pause")
    paused = ledger.ok("meeting_control", meeting_id=meeting["id"], action="pause")
    assert paused["task_ids"] == [second]
    assert advance(ledger)["paused"] is True
    # Even an individual API resume cannot bypass the whole-meeting pause.
    two = ledger.ok("task_get", task_id=second)
    ledger.ok("task_control", task_id=second, expected_revision=two["revision"], action="resume")
    assert ledger.ok("task_get", task_id=second)["blockers"][0]["kind"] == "meeting"
    ledger.ok("meeting_control", meeting_id=meeting["id"], action="resume")
    assert ledger.ok("task_get", task_id=first)["status"] == "paused"
    assert not ledger.ok("task_get", task_id=second)["blockers"]


def test_local_meeting_control_interrupts_and_waits_before_resume(ledger):
    _, meeting = setup(ledger)
    interrupted = []
    future = Future()
    future.set_result(None)

    def interrupt(project, tid):
        interrupted.append((project, tid))
        return future

    dispatcher = SimpleNamespace(interrupt_task=interrupt, task_running=lambda p, t: True)
    manager = SimpleNamespace(service=ledger.service, dispatcher_for=lambda p: dispatcher)
    payload = dict(
        project="default",
        actor="operator",
        request_id="pause-all",
        meeting_id=meeting["id"],
        action="pause",
    )
    result = control_meeting(manager, payload)
    assert result["meeting"]["paused"] and not result["stopping"]
    assert interrupted == [("default", meeting["waiting"][0])]
    payload.update(request_id="resume-all", action="resume")
    with pytest.raises(ValueError, match="仍在退出"):
        control_meeting(manager, payload)
    dispatcher.task_running = lambda p, t: False
    assert not control_meeting(manager, payload)["meeting"]["paused"]


@pytest.mark.parametrize(
    "result",
    [
        [],
        None,
        {"assignments": [None, None]},
        {},
        {"assignments": [{"group_id": "a", "goal": "x"}]},
        {"assignments": [{"group_id": "a", "goal": "x"}, {"group_id": "unknown", "goal": "x"}]},
    ],
)
def test_invalid_plan_stops_and_can_be_regenerated(ledger, result):
    principal, meeting = setup(ledger)
    tid = meeting["waiting"][0]
    finish(ledger, principal, tid, result)
    meeting = advance(ledger)
    assert meeting["error"] and len(ledger.ok("tasks_list")["tasks"]) == 1
    ledger.ok("meeting_retry", meeting_id=meeting["id"])
    finish(ledger, principal, tid, plan())
    assert advance(ledger)["stage"] == "delegating"


def test_invalid_group_delegation_does_not_partially_create_work(ledger):
    principal, meeting = setup(ledger)
    finish(ledger, principal, meeting["waiting"][0], plan())
    meeting = advance(ledger)
    for tid in meeting["waiting"]:
        finish(
            ledger,
            principal,
            tid,
            {"tasks": [{"member_id": "a1", "title": "Work", "description": "Do it"}]},
        )
    meeting = advance(ledger)
    assert meeting["error"] and len(ledger.ok("tasks_list")["tasks"]) == 3


def test_transfer_preserves_requirements_and_changes_chat_ownership(ledger):
    _, meeting = setup(ledger)
    tid = meeting["waiting"][0]
    original = ledger.ok("task_get", task_id=tid)
    changed = ledger.ok(
        "office_transfer",
        task_id=tid,
        expected_revision=original["revision"],
        member_id="b1",
        note="Files retained; inspect tests",
    )
    assert changed["status"] == "paused" and changed["inputs"][:-1] == original["inputs"]
    checkpoint = ledger.ok("item_get", key=changed["inputs"][-1])["value"]
    assert checkpoint["from_member"] == "a1" and checkpoint["to_member"] == "b1"
    assert checkpoint["note"] == "Files retained; inspect tests"
    history = ledger.ok("task_inspect", task_id=tid)
    assert history["handoffs"][0]["details"]["checkpoint"] == changed["inputs"][-1]
    role = ledger.ok("role_get", role_id=changed["role_id"])
    old = ledger.ok("role_get", role_id=original["role_id"])
    assert role["instructions"] == old["instructions"]
    manager = SimpleNamespace(_lock=RLock(), _closed=False, _project="default", _dispatcher=None)
    chat = MemberChat(ledger.service, manager)
    assert not chat.history("default", "a1")["tasks"]
    assert chat.history("default", "b1")["tasks"][0]["task_id"] == tid
    with pytest.raises(ValueError, match="不属于"):
        chat.send(
            dict(
                project="default",
                member_id="a1",
                task_id=tid,
                message="wrong owner",
                request_id="wrong",
            )
        )


def test_local_pause_waits_and_resume_rejects_active_execution(ledger):
    principal, meeting = setup(ledger)
    tid = meeting["waiting"][0]
    as_agent(
        ledger,
        principal,
        "coder",
        "task_claim",
        project="default",
        task_id=tid,
        selection={"model": "model-a", "parameters": {}},
    )
    future = Future()
    future.set_result(None)
    dispatcher = SimpleNamespace(interrupt_task=lambda p, t: future, task_running=lambda p, t: True)
    manager = SimpleNamespace(
        _lock=RLock(), service=ledger.service, dispatcher_for=lambda p: dispatcher
    )
    task = ledger.ok("task_get", task_id=tid)
    result = control(
        manager,
        dict(
            project="default",
            actor="operator",
            request_id="pause",
            task_id=tid,
            expected_revision=task["revision"],
            action="pause",
        ),
    )
    assert result["task"]["status"] == "paused" and not result["stopping"]
    with pytest.raises(ValueError, match="仍在退出"):
        control(
            manager,
            dict(
                project="default",
                actor="operator",
                request_id="resume",
                task_id=tid,
                expected_revision=result["task"]["revision"],
                action="resume",
            ),
        )


def test_same_member_rebinding_updates_future_meeting_route_without_staling_inputs(ledger):
    _, meeting = setup(ledger)
    tid = meeting["waiting"][0]
    original = ledger.ok("task_get", task_id=tid)
    config = ledger.ok("office_get")
    original_context = ledger.ok("item_get", key=meeting["context"])
    agent = ledger.ok("agent_get", agent_id="coder")
    ledger.ok(
        "agent_put",
        agent_id="coder",
        expected_revision=agent["revision"],
        label="coder",
        runtime="codex",
        mode="command",
        models=[{"id": "model-a", "parameters": {}}, {"id": "model-b", "parameters": {}}],
    )
    # Keep the provider valid while changing the task-selected model.
    config["groups"][0]["members"][0]["model"] = "model-b"
    ledger.ok("office_put", expected_revision=config["revision"], groups=config["groups"])
    changed = ledger.ok(
        "office_transfer",
        task_id=tid,
        expected_revision=original["revision"],
        member_id="a1",
        note="Use updated model for this member",
    )
    current = ledger.ok("meetings_list")["meetings"][0]
    assert current["groups"][0]["members"][0]["model"] == "model-b"
    assert ledger.ok("role_get", role_id=changed["role_id"])["selection"]["model"] == "model-b"
    assert ledger.ok("item_get", key=meeting["context"])["version"] == original_context["version"]
