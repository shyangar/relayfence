"""Human checkpoints are fenced, durable and cannot silently turn into execution."""

import json
import sys
from types import SimpleNamespace

import pytest
from test_meetings import advance, finish, plan, setup
from test_office import as_agent
from test_worker import LocalClient

from relayfence.codex_adapter import CodexResult, question_output
from relayfence.local_task_control import respond_decision
from relayfence.service import Service
from relayfence.store import Store
from relayfence.worker import run_task


def ask(ledger):
    principal, meeting = setup(ledger)
    tid = meeting["waiting"][0]
    claim = as_agent(
        ledger,
        principal,
        "coder",
        "task_claim",
        task_id=tid,
        selection={"model": "model-a", "parameters": {}},
    )
    result = as_agent(
        ledger,
        principal,
        "coder",
        "task_question",
        task_id=tid,
        fence=claim["task"]["fence"],
        lease_token=claim["lease_token"],
        question={
            "question": "需要兼容旧接口吗？",
            "context": "兼容方案成本更高。",
            "options": ["兼容", "不兼容"],
        },
    )
    return principal, meeting, result, claim


def answer(ledger, decision, action="continue", text="兼容旧接口", **extra):
    return ledger.ok(
        "decision_respond",
        decision_id=decision,
        expected_revision=1,
        action=action,
        answer=text,
        **extra,
    )


def agreed(ledger):
    principal, meeting = setup(ledger, "discussion")
    finish(ledger, principal, meeting["waiting"][0], plan())
    meeting = advance(ledger)
    for tid in meeting["waiting"]:
        finish(ledger, principal, tid, {"can_complete": True, "reason": "可完成"})
    meeting = advance(ledger)
    return principal, meeting, ledger.ok("decisions_list")["decisions"][0]


def test_question_pauses_and_reply_is_fenced_and_persisted(ledger):
    principal, meeting, result, claim = ask(ledger)
    tid = claim["task"]["task_id"]
    decision = result["decision_id"]
    task = ledger.ok("task_get", task_id=tid)
    assert task["status"] == "paused" and task["display_status"] == "waiting_user"
    assert advance(ledger)["paused_for_decision"] == decision
    ledger.error(
        "decision_required",
        "task_control",
        task_id=tid,
        expected_revision=task["revision"],
        action="resume",
    )
    ledger.error("decision_required", "meeting_control", meeting_id=meeting["id"], action="resume")
    ledger.error(
        "answer_required",
        "decision_respond",
        decision_id=decision,
        expected_revision=1,
        action="continue",
    )
    assert not ledger.ok("decisions_list", project="other")["decisions"]
    restored = Store(ledger.store.path, clock=ledger.clock)
    assert (
        Service(restored).call("decisions_list", {})[0]["data"]["decisions"][0]["status"]
        == "pending"
    )
    answer(ledger, decision, request_id="answer")
    assert ledger.request(
        "decision_respond",
        decision_id=decision,
        expected_revision=1,
        action="continue",
        answer="兼容旧接口",
        request_id="answer",
    )["replayed"]
    ledger.error(
        "revision_conflict",
        "decision_respond",
        decision_id=decision,
        expected_revision=1,
        action="continue",
        answer="other",
    )
    task = ledger.ok("task_get", task_id=tid)
    assert task["status"] == "ready" and "兼容旧接口" in task["description"]
    assert not advance(ledger)["paused"]
    next_claim = as_agent(
        ledger,
        principal,
        "coder",
        "task_claim",
        task_id=tid,
        selection={"model": "model-a", "parameters": {}},
    )
    assert any(i["value"].get("answer") == "兼容旧接口" for i in next_claim["inputs"])
    bad, code = ledger.service.call(
        "task_question",
        dict(
            project="default",
            actor="coder",
            request_id="old",
            task_id=tid,
            fence=claim["task"]["fence"],
            lease_token=claim["lease_token"],
            question={"question": "old"},
        ),
        principal,
    )
    assert code == 409 and bad["error"]["code"] == "lease_lost"


@pytest.mark.parametrize(
    "action,stage",
    [
        ("finish_discussion", "discussion_done"),
        ("continue", "delegating"),
        ("revise", "planning"),
        ("cancel", "cancelled"),
    ],
)
def test_discussion_requires_explicit_next_step(ledger, action, stage):
    _, meeting, decision = agreed(ledger)
    assert meeting["stage"] == "ready_for_execution"
    assert len(ledger.ok("tasks_list")["tasks"]) == 3
    answer(ledger, decision["decision_id"], action=action, text="优先实现小范围方案")
    meeting = advance(ledger)
    assert meeting["stage"] == stage
    assert not [d for d in ledger.ok("decisions_list")["decisions"] if d["status"] == "pending"]
    if action == "revise":
        planner = ledger.ok("task_get", task_id=meeting["waiting"][0])
        assert any(key.startswith("decisions/") for key in planner["inputs"])
    if action == "finish_discussion":
        assert len(ledger.ok("tasks_list")["tasks"]) == 3
        assert (
            ledger.ok(
                "meeting_revision", meeting_id=meeting["id"], briefing_id="next", changes="补充方案"
            )["meeting"]["mode"]
            == "discussion"
        )


def test_legacy_execute_resolves_gate_and_manual_changes_cannot_be_overwritten(ledger):
    _, meeting, decision = agreed(ledger)
    ledger.ok("meeting_execute", meeting_id=meeting["id"])
    assert ledger.ok("decisions_list")["decisions"][0]["status"] == "answered"


def test_cancel_and_stale_reply_are_safe(ledger):
    _, meeting, result, claim = ask(ledger)
    with ledger.store.connection() as db:
        db.execute(
            "UPDATE tasks SET revision=revision+1 WHERE project='default' AND task_id=?",
            (claim["task"]["task_id"],),
        )
    ledger.error(
        "task_changed",
        "decision_respond",
        decision_id=result["decision_id"],
        expected_revision=1,
        action="continue",
        answer="继续",
    )
    answer(ledger, result["decision_id"], action="cancel", text="取消")
    assert advance(ledger)["stage"] == "cancelled"


def test_ui_waits_for_old_process_before_applying_answer(ledger):
    _, _, result, _ = ask(ledger)
    dispatcher = SimpleNamespace(task_running=lambda p, t: True, interrupt_task=lambda p, t: None)
    manager = SimpleNamespace(service=ledger.service, dispatcher_for=lambda p: dispatcher)
    payload = dict(
        project="default",
        actor="operator",
        request_id="reply",
        decision_id=result["decision_id"],
        expected_revision=1,
        action="continue",
        answer="兼容",
    )
    with pytest.raises(ValueError, match="仍在退出"):
        respond_decision(manager, payload)
    dispatcher.task_running = lambda p, t: False
    assert respond_decision(manager, payload)["status"] == "answered"


def test_real_worker_asks_then_receives_answer_on_next_attempt(ledger, tmp_path):
    principal, meeting = setup(ledger)
    script = tmp_path / "worker.py"
    script.write_text(
        "import json,sys\nr=json.loads(sys.stdin.buffer.read())\n"
        "if any(i['key'].startswith('decisions/') for i in r['inputs']):\n"
        " print(json.dumps({'outputs':[{'key':'tasks/'+r['task']['task_id']+'/result','expected_version':0,'value':{'summary':'Answer received'}}]}))\n"
        "else: print(json.dumps({'question':{'question':'Choose compatibility','options':['yes','no']}}))\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "agent_id": "coder",
                "cwd": str(tmp_path),
                "timeout_seconds": 5,
                "command": [sys.executable, str(script), "{model}"],
            }
        )
    )
    client = LocalClient(ledger, principal)
    tid = meeting["waiting"][0]
    result = run_task(client, config, tid, tmp_path / "run1")
    assert result["data"]["waiting_user"]
    answer(ledger, result["data"]["decision_id"], text="yes")
    assert run_task(client, config, tid, tmp_path / "run2")["data"]["task"]["status"] == "done"


def test_adapter_confirmation_result_does_not_claim_completed_work():
    value = CodexResult(
        outcome="needs_confirmation", summary="Need approval", details="Reason", files=[], checks=[]
    ).model_dump(exclude_none=True)
    assert question_output(value)["question"]["question"] == "Need approval"


def test_strict_result_schema_requires_nullable_question_and_bounded_fields():
    from relayfence.codex_adapter import result_schema

    schema = result_schema()
    assert set(schema["required"]) == set(schema["properties"])
    question = schema["$defs"]["QuestionPayload"]
    assert set(question["required"]) == {"question", "context", "options"}


def test_runtime_child_origin_reaches_group_and_member_history(ledger):
    from relayfence.group_chat import GroupChat
    from relayfence.member_chat import MemberChat
    from relayfence.run_activity import entries_from_events

    principal, meeting = setup(ledger)
    tid = meeting["waiting"][0]
    claimed = as_agent(
        ledger,
        principal,
        "coder",
        "task_claim",
        task_id=tid,
        selection={"model": "model-a", "parameters": {}},
    )
    entries = entries_from_events(
        "codex-events.jsonl",
        [
            {
                "type": "item.completed",
                "agent_id": "child-1",
                "parent_agent_id": "lead-session",
                "item": {"type": "agent_message", "text": "Checking API contracts"},
            }
        ],
    )
    assert entries[0]["source_agent"] == "child-1" and entries[0]["parent_agent"] == "lead-session"
    as_agent(
        ledger,
        principal,
        "coder",
        "task_activity",
        task_id=tid,
        fence=claimed["task"]["fence"],
        lease_token=claimed["lease_token"],
        entries=entries,
    )
    group = GroupChat(ledger.service).history("default", "a")["messages"]
    activity = next(m for m in group if m["kind"] == "activity")
    assert activity["source_agent"] == "child-1"
    assert activity["parent_agent"] == "lead-session"
    member = MemberChat(ledger.service, None).history("default", "a1")
    assert (
        next(m for m in member["messages"] if m["kind"] == "activity")["content"]
        == "Checking API contracts"
    )


def test_decisions_are_part_of_the_conversation(ledger):
    from relayfence.group_chat import GroupChat

    _, _, decision = agreed(ledger)
    answer(
        ledger, decision["decision_id"], action="finish_discussion", text="仅保存方案，后续再开发"
    )
    messages = GroupChat(ledger.service).history("default", "a")["messages"]
    assert any(
        m["kind"] == "decision" and m["role"] == "user" and "后续再开发" in m["content"]
        for m in messages
    )


@pytest.mark.parametrize("action", ["continue", "revise", "cancel"])
def test_local_decision_cancel_signals_process_and_rejects_unknown_decision(ledger, action):
    _, meeting, result, _ = ask(ledger)
    interrupted = []
    dispatcher = SimpleNamespace(
        task_running=lambda p, t: False,
        interrupt_task=lambda p, t: interrupted.append(t),
    )
    manager = SimpleNamespace(service=ledger.service, dispatcher_for=lambda p: dispatcher)
    payload = dict(
        project="default",
        actor="operator",
        request_id="local-decision",
        decision_id=result["decision_id"],
        expected_revision=1,
        action=action,
        answer="明确回复",
    )
    assert respond_decision(manager, payload)["status"] == (
        "cancelled" if action == "cancel" else "answered"
    )
    assert bool(interrupted) == (action == "cancel")
    payload.update(decision_id="missing", request_id="missing-decision")
    from relayfence.errors import CoordinationError

    with pytest.raises(CoordinationError, match="不存在"):
        respond_decision(manager, payload)
    manager.dispatcher_for = lambda p: None
    with pytest.raises(ValueError, match="先连接"):
        respond_decision(manager, payload)


@pytest.mark.parametrize("case", ["finish", "description", "inputs"])
def test_confirmation_rejects_unsafe_transition_without_consuming_answer(ledger, case):
    _, _, result, claim = ask(ledger)
    tid = claim["task"]["task_id"]
    if case in ("description", "inputs"):
        with ledger.store.connection() as db:
            if case == "description":
                db.execute("UPDATE tasks SET description=? WHERE task_id=?", ("x" * 7999, tid))
            else:
                db.execute(
                    "UPDATE tasks SET inputs=? WHERE task_id=?",
                    (json.dumps(["input/" + str(i) for i in range(32)]), tid),
                )
    ledger.error(
        {
            "finish": "invalid_transition",
            "description": "description_limit",
            "inputs": "input_limit",
        }[case],
        "decision_respond",
        decision_id=result["decision_id"],
        expected_revision=1,
        action="finish_discussion" if case == "finish" else "continue",
        answer="继续",
    )
    assert ledger.ok("decisions_list")["decisions"][0]["status"] == "pending"


def test_empty_gate_revision_preserves_pending_checkpoint(ledger):
    _, _, decision = agreed(ledger)
    ledger.error(
        "answer_required",
        "decision_respond",
        decision_id=decision["decision_id"],
        expected_revision=1,
        action="revise",
        answer="",
    )
    assert ledger.ok("decisions_list")["decisions"][0]["status"] == "pending"
