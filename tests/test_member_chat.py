import json
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from relayfence.errors import CoordinationError
from relayfence.member_chat import MemberChat


@pytest.fixture
def chat(ledger):
    ledger.ok("task_create", project="p", task_id="task", title="Implement", description="Original")
    with ledger.store.connection() as db:
        db.execute(
            "INSERT INTO office_configs VALUES (?,?,?,?)",
            (
                "p",
                1,
                json.dumps([{"id": "group-a", "members": [{"id": "alice"}]}]),
                ledger.clock(),
            ),
        )
        db.execute("UPDATE tasks SET role_id='office/alice' WHERE project='p'")
    future = Future()
    dispatcher = SimpleNamespace(interrupt_task=lambda project, task: future)
    manager = SimpleNamespace(
        _lock=threading.RLock(), _project="p", _closed=False, _dispatcher=dispatcher
    )
    instance = MemberChat(ledger.service, manager)
    instance.test_future = future
    return instance


def send(chat, **extra):
    return chat.send(
        {
            "project": "p",
            "member_id": "alice",
            "task_id": "task",
            "message": "Use blue",
            "request_id": "guide-1",
            **extra,
        }
    )


def wait_applied(chat):
    for _ in range(100):
        history = chat.history("p", "alice")
        if next(m for m in history["messages"] if m["kind"] == "guidance")["status"] == "applied":
            return history
        time.sleep(0.01)
    pytest.fail("guidance did not apply")


def running(ledger):
    # The runtime claim is exercised separately; simulate its persisted lease here.
    with ledger.store.connection() as db:
        db.execute(
            "UPDATE tasks SET status='running',owner='worker',lease_hash='secret',expires_at=?,fence=2 WHERE project='p'",
            (ledger.clock() + 300,),
        )


def test_ready_guidance_persists_real_task_instruction_and_replays(chat, ledger):
    first = send(chat)
    assert first["message"]["status"] == "applied"
    assert send(chat)["replayed"]
    with pytest.raises(CoordinationError, match="request_id"):
        send(chat, message="Use red")
    restored = MemberChat(ledger.service, chat.manager).history("p", "alice")
    assert restored["tasks"][0]["description"] == "Original\n\n用户追加指导：\nUse blue"
    assert len([m for m in restored["messages"] if m["kind"] == "guidance"]) == 1


def test_escape_heavy_guidance_log_keeps_metadata_within_bound(chat, ledger):
    content = '"\\\n' * 2000
    send(chat, message=content, request_id="guide-escape")

    with ledger.store.connection() as db:
        row = db.execute(
            "SELECT details FROM group_logs WHERE project='p' AND kind='chat.guidance'"
        ).fetchone()
        message = db.execute("SELECT id FROM member_messages WHERE project='p'").fetchone()

    details = json.loads(row["details"])
    assert len(row["details"]) <= 12000
    assert details["message_id"] == message["id"]
    assert details["status"] == "applied"
    assert details["content"].startswith('"')
    assert details["content"].endswith(" …")


def test_history_paginates_newest_first_without_repeating(chat, ledger):
    base = ledger.clock()
    with ledger.store.connection() as db:
        for index in range(1, 8):
            db.execute(
                "INSERT INTO member_messages(project,member_id,task_id,request_id,content,status,"
                "detail,paused_revision,created_at,updated_at) "
                "VALUES ('p','alice','task',?,?,'applied','',NULL,?,?)",
                (f"r{index}", f"message {index}", base + index, base + index),
            )
    latest = chat.history("p", "alice", limit=3)
    assert [m["content"] for m in latest["messages"]] == [
        "message 5",
        "message 6",
        "message 7",
    ]
    assert latest["has_more"] is True
    assert latest["next_before"].startswith(f"{base + 5:.9f}:")

    older = chat.history("p", "alice", before=latest["next_before"], limit=3)
    assert [m["content"] for m in older["messages"]] == [
        "message 2",
        "message 3",
        "message 4",
    ]
    assert older["has_more"] is True
    assert older["next_before"].startswith(f"{base + 2:.9f}:")

    oldest = chat.history("p", "alice", before=older["next_before"], limit=3)
    assert [m["content"] for m in oldest["messages"]] == ["任务已创建", "message 1"]
    assert oldest["has_more"] is False
    assert oldest["next_before"] is None


def test_running_guidance_waits_for_old_worker_before_resume(chat, ledger):
    running(ledger)
    assert send(chat)["message"]["status"] == "pending"
    with ledger.store.connection() as db:
        row = db.execute("SELECT * FROM tasks WHERE project='p'").fetchone()
        assert row["status"] == "paused" and row["lease_hash"] is None
        assert row["description"] == "Original"
    assert send(chat)["replayed"]
    chat.test_future.set_result(None)
    history = wait_applied(chat)
    assert history["tasks"][0]["status"] == "ready"
    assert "Use blue" in history["tasks"][0]["description"]
    assert history["tasks"][0]["fence"] == 2  # next real claim increments the fence
    guidance = next(m for m in history["messages"] if m["kind"] == "guidance")
    with ledger.store.connection() as db:
        applied = db.execute(
            "SELECT details FROM group_logs WHERE project='p' AND kind='chat.applied'"
        ).fetchone()
    assert json.loads(applied["details"])["message_id"] == int(
        guidance["id"].removeprefix("guidance-")
    )


def test_history_uses_one_snapshot_while_guidance_commits(chat, ledger, monkeypatch):
    running(ledger)
    send(chat)
    original = ledger.store._task_view
    triggered = False

    def view(*args):
        nonlocal triggered
        task = original(*args)
        if not triggered:
            triggered = True
            chat.test_future.set_result(None)
            deadline = time.monotonic() + 3
            while True:
                with ledger.store.connection() as db:
                    status = db.execute("SELECT status FROM member_messages").fetchone()[0]
                if status == "applied":
                    break
                assert time.monotonic() < deadline
                time.sleep(0.01)
        return task

    monkeypatch.setattr(ledger.store, "_task_view", view)
    history = chat.history("p", "alice")
    guidance = next(m for m in history["messages"] if m["kind"] == "guidance")
    assert history["tasks"][0]["status"] == "paused"
    assert guidance["status"] == "pending"
    latest = chat.history("p", "alice")
    assert latest["tasks"][0]["status"] == "ready"
    assert next(m for m in latest["messages"] if m["kind"] == "guidance")["status"] == "applied"


@pytest.mark.parametrize(
    "cursor",
    [
        True,
        -1,
        float("nan"),
        float("inf"),
        "x" * 257,
        "no-separator",
        "bad:id",
        "1:",
        "nan:id",
        "1:" + "x" * 201,
        {},
    ],
)
def test_invalid_history_cursors_are_rejected(chat, cursor):
    with pytest.raises(ValueError):
        chat.history("p", "alice", before=cursor)


@pytest.mark.parametrize(
    "fields",
    [
        {"project": ""},
        {"project": False},
        {"member_id": ""},
        {"member_id": "x" * 201},
        {"limit": True},
        {"limit": 0},
        {"limit": 201},
    ],
)
def test_invalid_history_scope_and_limits_are_rejected(chat, fields):
    with pytest.raises(ValueError):
        chat.history(**{"project": "p", "member_id": "alice", **fields})


def test_legacy_timestamp_cursor_reads_only_older_messages(chat):
    send(chat)
    assert chat.history("p", "alice", before=0)["messages"] == []


def test_guidance_cleanup_timeout_keeps_task_paused(chat, ledger):
    running(ledger)

    class Timeout:
        def result(self, timeout):
            raise TimeoutError

    chat.manager._dispatcher.interrupt_task = lambda p, t: Timeout()
    send(chat)
    for _ in range(100):
        history = chat.history("p", "alice")
        guidance = next(m for m in history["messages"] if m["kind"] == "guidance")
        if guidance["status"] == "failed":
            break
        time.sleep(0.01)
    assert guidance["status"] == "failed" and "超时" in guidance["detail"]
    assert history["tasks"][0]["status"] == "paused"


def test_concurrent_edit_prevents_automatic_resume(chat, ledger):
    running(ledger)
    send(chat)
    with ledger.store.connection() as db:
        db.execute("UPDATE tasks SET revision=revision+1 WHERE project='p'")
    chat.test_future.set_result(None)
    for _ in range(100):
        messages = [m for m in chat.history("p", "alice")["messages"] if m["kind"] == "guidance"]
        if messages[0]["status"] == "failed":
            break
        time.sleep(0.01)
    assert messages[0]["status"] == "failed"
    assert chat.history("p", "alice")["tasks"][0]["status"] == "paused"


def test_missing_runtime_keeps_task_paused_and_reports_failure(chat, ledger):
    running(ledger)
    chat.manager._dispatcher.interrupt_task = lambda *args: None
    send(chat)
    history = chat.history("p", "alice")
    guidance = next(m for m in history["messages"] if m["kind"] == "guidance")
    assert guidance["status"] == "failed"
    assert history["tasks"][0]["status"] == "paused"
    with ledger.store.connection() as db:
        failed = db.execute(
            "SELECT details FROM group_logs WHERE project='p' AND kind='chat.failed'"
        ).fetchone()
    assert json.loads(failed["details"])["message_id"] == int(
        guidance["id"].removeprefix("guidance-")
    )


def test_scope_and_completed_task_cannot_receive_guidance(chat, ledger):
    with pytest.raises(ValueError, match="连接"):
        send(chat, project="other")
    with pytest.raises(CoordinationError):
        send(chat, member_id="bob")
    with ledger.store.connection() as db:
        db.execute("UPDATE tasks SET status='done' WHERE project='p'")
    with pytest.raises(ValueError, match="结束"):
        send(chat)


def test_history_reads_actual_published_artifact_and_project_scope(chat, ledger):
    with ledger.store.connection() as db:
        db.execute("INSERT INTO items VALUES ('p','answer','artifact',1)")
        db.execute(
            "INSERT INTO revisions VALUES (?,?,?,?,?,?,?)",
            (
                "p",
                "answer",
                1,
                json.dumps({"summary": "Implemented and verified"}),
                "[]",
                "worker",
                ledger.clock(),
            ),
        )
        db.execute(
            "UPDATE tasks SET status='done',outputs=? WHERE project='p'",
            (json.dumps([{"key": "answer", "version": 1}]),),
        )
    results = [m for m in chat.history("p", "alice")["messages"] if m["role"] == "assistant"]
    assert [m["content"] for m in results] == ["Implemented and verified"]


def test_oversize_cumulative_guidance_leaves_running_task_untouched(chat, ledger):
    running(ledger)
    with ledger.store.connection() as db:
        db.execute("UPDATE tasks SET description=? WHERE project='p'", ("x" * 7990,))
    with pytest.raises(ValueError, match="8000"):
        send(chat)
    with ledger.store.connection() as db:
        row = db.execute("SELECT * FROM tasks WHERE project='p'").fetchone()
        assert row["status"] == "running" and row["lease_hash"] == "secret"
        assert db.execute("SELECT count(*) FROM member_messages").fetchone()[0] == 0


def test_recovery_fails_pending_guidance_and_preserves_pause(chat, ledger):
    running(ledger)
    send(chat)
    restored = MemberChat(ledger.service, chat.manager)
    restored.recover_pending()
    messages = [m for m in restored.history("p", "alice")["messages"] if m["kind"] == "guidance"]
    assert messages[0]["status"] == "failed"
    assert "重启" in messages[0]["detail"]
    assert restored.history("p", "alice")["tasks"][0]["status"] == "paused"
    with ledger.store.connection() as db:
        failure = db.execute(
            "SELECT group_id,level,details FROM group_logs "
            "WHERE project='p' AND task_id='task' AND kind='chat.failed'"
        ).fetchone()
    assert failure is not None
    assert failure["group_id"] == "group-a"
    assert failure["level"] == "error"
    assert "重启" in json.loads(failure["details"])["detail"]
    # An old callback must not apply after startup recovery.
    chat.test_future.set_result(None)
