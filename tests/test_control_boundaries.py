"""Exercise UI control paths through HTTP and the real ledger."""

import threading
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from test_meetings import setup

from relayfence.group_chat import GroupChat
from relayfence.member_chat import MemberChat
from relayfence.server import create_app


@pytest.fixture
def controls(ledger):
    _, meeting = setup(ledger)
    dispatcher = SimpleNamespace(task_running=lambda p, t: False, interrupt_task=lambda p, t: None)
    manager = SimpleNamespace(
        service=ledger.service,
        _lock=threading.RLock(),
        _closed=False,
        _project="default",
        _dispatcher=dispatcher,
        dispatcher_for=lambda p: dispatcher,
        start_discovery=lambda: None,
        close=lambda: None,
    )
    manager.member_chat = MemberChat(ledger.service, manager)
    manager.group_chat = GroupChat(ledger.service)
    app = create_app(ledger.store, "t" * 40, workspace_manager=manager, local_ui=True)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as client:
        client.headers.update(
            {
                "X-RelayFence-Local": "1",
                "Sec-Fetch-Site": "same-origin",
                "Origin": "http://127.0.0.1",
            }
        )
        yield client, manager, dispatcher, meeting


def request(task, action, **extra):
    return dict(
        project="default",
        actor="operator",
        request_id=action + "-" + str(task["revision"]),
        task_id=task["task_id"],
        expected_revision=task["revision"],
        action=action,
        **extra,
    )


def test_http_pause_resume_transfer_and_cas(controls, ledger):
    client, manager, dispatcher, meeting = controls
    task = ledger.ok("task_get", task_id=meeting["waiting"][0])
    paused = client.post("/local/task-control", json=request(task, "pause"))
    assert paused.status_code == 200
    task = paused.json()["task"]
    dispatcher.task_running = lambda p, t: True
    assert client.post("/local/task-control", json=request(task, "resume")).status_code == 400
    dispatcher.task_running = lambda p, t: False
    resumed = client.post("/local/task-control", json=request(task, "resume"))
    assert resumed.status_code == 200
    assert client.post("/local/task-control", json=request(task, "cancel")).status_code == 409
    task = resumed.json()["task"]
    transferred = client.post(
        "/local/task-control",
        json=request(task, "handoff", member_id="b1", note="Keep existing changes; verify tests"),
    )
    assert transferred.status_code == 200 and transferred.json()["task"]["status"] == "paused"
    assert (
        client.post(
            "/local/task-control",
            json=request(transferred.json()["task"], "handoff", member_id="missing", note="Notes"),
        ).status_code
        == 404
    )
    manager.dispatcher_for = lambda p: None
    assert (
        client.post(
            "/local/task-control", json=request(transferred.json()["task"], "resume")
        ).status_code
        == 400
    )


@pytest.mark.parametrize("outcome", ["timeout", "finished_error"])
def test_http_pause_retains_fence_during_cleanup(controls, ledger, outcome):
    client, _, dispatcher, meeting = controls

    class Pending:
        def result(self, timeout):
            raise TimeoutError

    future = Future()
    future.set_exception(RuntimeError("revoked execution finished"))
    dispatcher.interrupt_task = lambda p, t: Pending() if outcome == "timeout" else future
    task = ledger.ok("task_get", task_id=meeting["waiting"][0])
    result = client.post("/local/task-control", json=request(task, "pause"))
    assert result.status_code == 200
    assert result.json()["stopping"] == (outcome == "timeout")
    assert result.json()["task"]["status"] == "paused"


def test_http_delete_refuses_active_process_and_keeps_receipt(controls, ledger):
    client, manager, dispatcher, _ = controls
    task = ledger.ok("task_create", task_id="standalone", title="Delete after cleanup")
    payload = {k: v for k, v in request(task, "delete").items() if k != "action"}
    dispatcher.task_running = lambda p, t: True
    assert client.post("/local/task-delete", json=payload).status_code == 400
    dispatcher.task_running = lambda p, t: False
    response = client.post("/local/task-delete", json=payload)
    assert response.status_code == 200 and response.json()["deleted"]
    assert client.post("/local/task-delete", json=payload).status_code == 200
    manager.dispatcher_for = lambda p: None
    assert client.post("/local/task-delete", json=payload).status_code == 400


@pytest.mark.parametrize("outcome", ["timeout", "finished_error"])
def test_http_meeting_pause_waits_for_all_cleanup(controls, outcome):
    client, manager, dispatcher, meeting = controls

    class Pending:
        def result(self, timeout):
            raise TimeoutError

    done = Future()
    done.set_exception(RuntimeError("finished"))
    dispatcher.interrupt_task = lambda p, t: Pending() if outcome == "timeout" else done
    payload = dict(
        project="default",
        actor="operator",
        request_id="pause",
        meeting_id=meeting["id"],
        action="pause",
    )
    response = client.post("/local/meeting-control", json=payload)
    assert response.status_code == 200 and response.json()["stopping"] == (outcome == "timeout")
    payload.update(request_id="resume", action="resume")
    assert client.post("/local/meeting-control", json=payload).status_code == 200
    payload.update(request_id="missing", meeting_id="missing")
    assert client.post("/local/meeting-control", json=payload).status_code == 404
    manager.dispatcher_for = lambda p: None
    assert client.post("/local/meeting-control", json=payload).status_code == 400


def test_http_member_history_and_group_history_keep_project_scope(controls):
    client, _, _, _ = controls
    assert (
        client.post(
            "/local/member-chat", json={"project": "default", "member_id": "a1"}
        ).status_code
        == 200
    )
    assert (
        client.post("/local/group-chat", json={"project": "default", "group_id": "a"}).status_code
        == 200
    )
    assert (
        client.post("/local/member-chat", json={"project": "other", "member_id": "a1"}).status_code
        == 404
    )
    assert (
        client.post(
            "/local/member-guide",
            json={
                "project": "default",
                "member_id": "a1",
                "task_id": "missing",
                "message": "Guide",
                "request_id": "guide",
            },
        ).status_code
        == 404
    )
    response = client.post(
        "/local/task-control", json={}, headers={"Origin": "https://foreign.example"}
    )
    assert response.status_code == 403


def test_http_agent_activity_is_authenticated_and_fenced(controls, ledger):
    client, _, _, meeting = controls
    agent = ledger.ok("agent_get", agent_id="coder")
    issued = ledger.ok("agent_token_rotate", agent_id="coder", expected_revision=agent["revision"])
    headers = {"Authorization": "Bearer " + issued["token"]}
    payload = dict(
        project="default",
        actor="coder",
        request_id="http-claim",
        task_id=meeting["waiting"][0],
        selection={"model": "model-a", "parameters": {}},
    )
    claimed = client.post("/v1/task_claim", json=payload, headers=headers)
    assert claimed.status_code == 200
    claim = claimed.json()["data"]
    activity = dict(
        project="default",
        actor="coder",
        request_id="http-activity",
        task_id=payload["task_id"],
        fence=claim["task"]["fence"],
        lease_token=claim["lease_token"],
        entries=[{"kind": "note", "text": "Inspecting files"}],
    )
    response = client.post("/v1/task_activity", json=activity, headers=headers)
    assert response.status_code == 200 and response.json()["data"]["recorded"] == 1
    assert issued["token"] not in response.text and claim["lease_token"] not in response.text
    bad = client.post(
        "/v1/task_activity", json=activity, headers={"Authorization": "Bearer invalid"}
    )
    assert bad.status_code == 401
    task = ledger.ok("task_get", task_id=payload["task_id"])
    assert client.post("/local/task-control", json=request(task, "pause")).status_code == 200
    activity["request_id"] = "stale-http-activity"
    rejected = client.post("/v1/task_activity", json=activity, headers=headers)
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "lease_lost"
