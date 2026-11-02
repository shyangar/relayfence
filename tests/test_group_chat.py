import json

import pytest

from relayfence.errors import CoordinationError
from relayfence.group_chat import GroupChat


def configure(ledger, groups=None):
    groups = groups or [
        {
            "id": "group-a",
            "name": "交付组",
            "software": "codex",
            "members": [{"id": "alice", "name": "Alice"}],
        }
    ]
    with ledger.store.connection() as db:
        db.execute(
            "INSERT INTO office_configs VALUES (?,?,?,?)",
            ("p", 1, json.dumps(groups), ledger.clock()),
        )


@pytest.fixture
def group_chat(ledger):
    configure(ledger)
    return GroupChat(ledger.service)


def record(ledger, kind, message, **extra):
    with ledger.store.connection() as db:
        ledger.clock.advance(1)
        return ledger.store.record_db(db, "p", "group-a", kind, message, **extra)


def test_group_chat_projects_guidance_results_and_failures(group_chat, ledger):
    ledger.ok("task_create", project="p", task_id="task", title="Implement", description="Original")
    with ledger.store.connection() as db:
        cursor = db.execute(
            "INSERT INTO member_messages(project,member_id,task_id,request_id,content,status,"
            "detail,paused_revision,created_at,updated_at) "
            "VALUES ('p','alice','task','r1','Use blue','applied','',NULL,?,?)",
            (ledger.clock() + 1, ledger.clock() + 1),
        )
        message_id = cursor.lastrowid
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
                ledger.clock() + 2,
            ),
        )
        db.execute(
            "UPDATE tasks SET status='done',outputs=? WHERE project='p' AND task_id='task'",
            (json.dumps([{"key": "answer", "version": 1}]),),
        )
    record(
        ledger,
        "chat.guidance",
        "收到成员引导",
        member_id="alice",
        task_id="task",
        details={"content": "Use blue", "status": "applied", "message_id": message_id},
    )
    record(
        ledger,
        "task.completed",
        "任务已完成",
        member_id="alice",
        task_id="task",
        details={"outputs": [{"key": "answer", "version": 1}]},
    )
    record(
        ledger,
        "task.failed",
        "任务执行失败",
        level="error",
        member_id="alice",
        task_id="task",
        details={"message": "model quota exhausted"},
    )

    history = group_chat.history("p", "group-a")
    assert history["group"] == {
        "id": "group-a",
        "name": "交付组",
        "software": "codex",
        "member_count": 1,
    }
    assert [message["role"] for message in history["messages"]] == [
        "user",
        "assistant",
        "system",
    ]
    assert history["messages"][0]["content"] == "Use blue"
    assert history["messages"][0]["status_label"] == "已应用"
    assert history["messages"][0]["event_kind"] == "chat.guidance"
    assert history["messages"][0]["guidance_id"] == message_id
    assert history["messages"][1]["content"] == "Implemented and verified"
    assert history["messages"][1]["task_title"] == "Implement"
    assert history["messages"][2]["content"] == "model quota exhausted"
    assert history["messages"][2]["level"] == "error"
    assert history["next_after"] == history["messages"][-1]["log_id"]


def test_group_chat_keeps_full_guidance_after_task_deletion(group_chat, ledger):
    task = ledger.ok("task_create", project="p", task_id="task", title="Implement")
    content = "Guidance " + "x" * 1500
    with ledger.store.connection() as db:
        db.execute("UPDATE tasks SET role_id='office/alice' WHERE project='p' AND task_id='task'")
        cursor = db.execute(
            "INSERT INTO member_messages(project,member_id,task_id,request_id,content,"
            "status,detail,paused_revision,created_at,updated_at) "
            "VALUES ('p','alice','task','r1',?,'pending','',NULL,?,?)",
            (content, ledger.clock(), ledger.clock()),
        )
        message_id = cursor.lastrowid
    record(
        ledger,
        "chat.guidance",
        "收到成员引导",
        member_id="alice",
        task_id="task",
        details={"status": "pending", "content": content, "message_id": message_id},
    )
    record(
        ledger,
        "chat.applied",
        "成员引导已应用，任务继续执行",
        member_id="alice",
        task_id="task",
        details={"status": "applied", "message_id": message_id},
    )
    ledger.ok("task_delete", project="p", task_id="task", expected_revision=task["revision"])

    messages = group_chat.history("p", "group-a")["messages"]
    guidance = [message for message in messages if message["kind"] == "guidance"]

    assert guidance
    assert guidance[0]["content"] == content
    assert guidance[0]["status"] == "applied"
    assert guidance[0]["status_label"] == "已应用"
    assert guidance[0]["task_title"] == "Implement"
    outcome = next(message for message in messages if message["event_kind"] == "chat.applied")
    assert outcome["guidance_id"] == message_id


def test_independent_office_tasks_mirror_lifecycle_to_the_group(group_chat, ledger):
    ledger.ok("task_create", project="p", task_id="task", title="Implement")
    with ledger.store.connection() as db:
        db.execute("UPDATE tasks SET role_id='office/alice' WHERE project='p' AND task_id='task'")
        ledger.store.record_event(
            db,
            "p",
            "task.updated",
            "task",
            {"revision": 2},
            ledger.clock(),
        )

    messages = group_chat.history("p", "group-a")["messages"]

    assert messages[-1]["content"] == "任务说明已更新"
    assert messages[-1]["member_name"] == "Alice"


def test_group_chat_labels_a_deleted_task_as_a_warning(group_chat, ledger):
    task = ledger.ok("task_create", project="p", task_id="task", title="Implement")
    with ledger.store.connection() as db:
        db.execute("UPDATE tasks SET role_id='office/alice' WHERE project='p' AND task_id='task'")
    ledger.ok("task_delete", project="p", task_id="task", expected_revision=task["revision"])

    messages = group_chat.history("p", "group-a")["messages"]
    deleted = [message for message in messages if message["content"] == "任务已删除"]

    assert deleted
    assert deleted[0]["level"] == "warning"
    assert deleted[0]["task_title"] == "Implement"


def test_group_chat_labels_failed_activity_separately(group_chat, ledger):
    record(
        ledger,
        "run.activity",
        "执行命令",
        task_id="task",
        details={"kind": "command", "text": "exit 1", "status": "failed"},
    )

    message = group_chat.history("p", "group-a")["messages"][0]

    assert message["kind"] == "activity"
    assert message["entry_kind"] == "command"
    assert message["status"] == "failed"
    assert message["status_label"] == "失败"


def test_group_chat_uses_the_outputs_that_each_completion_published(group_chat, ledger):
    ledger.ok("task_create", project="p", task_id="task", title="Implement")
    with ledger.store.connection() as db:
        db.execute("INSERT INTO items VALUES ('p','answer','artifact',2)")
        for version, summary in ((1, "First result"), (2, "Second result")):
            db.execute(
                "INSERT INTO revisions VALUES (?,?,?,?,?,?,?)",
                (
                    "p",
                    "answer",
                    version,
                    json.dumps({"summary": summary}),
                    "[]",
                    "worker",
                    ledger.clock() + version,
                ),
            )
        db.execute(
            "UPDATE tasks SET status='done',outputs=? WHERE project='p' AND task_id='task'",
            (json.dumps([{"key": "answer", "version": 2}]),),
        )
    record(
        ledger,
        "task.completed",
        "任务已完成",
        task_id="task",
        details={"outputs": [{"key": "answer", "version": 1}]},
    )
    record(
        ledger,
        "task.completed",
        "任务已完成",
        task_id="task",
        details={"outputs": [{"key": "answer", "version": 2}]},
    )

    history = group_chat.history("p", "group-a")

    assert [message["content"] for message in history["messages"]] == [
        "First result",
        "Second result",
    ]


def test_group_chat_never_rewrites_history_with_current_outputs(group_chat, ledger):
    ledger.ok("task_create", project="p", task_id="task", title="Implement")
    with ledger.store.connection() as db:
        db.execute("INSERT INTO items VALUES ('p','answer','artifact',2)")
        for version, summary in ((1, "First result"), (2, "Current result")):
            db.execute(
                "INSERT INTO revisions VALUES (?,?,?,?,?,?,?)",
                (
                    "p",
                    "answer",
                    version,
                    json.dumps({"summary": summary}),
                    "[]",
                    "worker",
                    float(version),
                ),
            )
        db.execute(
            "UPDATE tasks SET status='done',outputs=? WHERE project='p' AND task_id='task'",
            (json.dumps([{"key": "answer", "version": 2}]),),
        )
    record(ledger, "task.completed", "任务已完成", task_id="task", details={})

    history = group_chat.history("p", "group-a")

    assert [message["content"] for message in history["messages"]] == ["任务已完成"]


def test_group_chat_recovers_outputs_from_the_event_ledger(group_chat, ledger):
    ledger.ok("task_create", project="p", task_id="task", title="Implement")
    with ledger.store.connection() as db:
        db.execute("INSERT INTO items VALUES ('p','answer','artifact',1)")
        db.execute(
            "INSERT INTO revisions VALUES (?,?,?,?,?,?,?)",
            (
                "p",
                "answer",
                1,
                json.dumps({"summary": "Ledger result"}),
                "[]",
                "worker",
                ledger.clock() + 1,
            ),
        )
    log_id = record(ledger, "task.completed", "任务已完成", task_id="task")
    with ledger.store.connection() as db:
        log = db.execute("SELECT created_at FROM group_logs WHERE id=?", (log_id,)).fetchone()
        db.execute(
            "INSERT INTO events(project,actor,kind,subject,details,created_at) "
            "VALUES ('p','worker','task.completed','task',?,?)",
            (json.dumps({"outputs": [{"key": "answer", "version": 1}]}), log["created_at"]),
        )

    history = group_chat.history("p", "group-a")

    assert [message["content"] for message in history["messages"]] == ["Ledger result"]


def test_group_chat_disambiguates_guidance_written_in_one_clock_tick(group_chat, ledger):
    ledger.ok("task_create", project="p", task_id="task", title="Implement")
    now = ledger.clock()
    with ledger.store.connection() as db:
        ids = []
        for request_id, content in (("r1", "First guidance"), ("r2", "Second guidance")):
            cursor = db.execute(
                "INSERT INTO member_messages(project,member_id,task_id,request_id,content,status,"
                "detail,paused_revision,created_at,updated_at) "
                "VALUES ('p','alice','task',?,?,'applied','',NULL,?,?)",
                (request_id, content, now, now),
            )
            ids.append(cursor.lastrowid)
        for message_id, content in zip(ids, ("First guidance", "Second guidance"), strict=True):
            ledger.store.record_db(
                db,
                "p",
                "group-a",
                "chat.guidance",
                "收到成员引导",
                member_id="alice",
                task_id="task",
                details={
                    "status": "applied",
                    "content": content,
                    "message_id": message_id,
                },
                now=now,
            )

    history = group_chat.history("p", "group-a")

    assert [message["content"] for message in history["messages"]] == [
        "First guidance",
        "Second guidance",
    ]


def test_group_chat_paginates_and_polls_incrementally(group_chat, ledger):
    for index in range(1, 7):
        record(ledger, "task.claimed", f"step {index}", task_id="task")

    latest = group_chat.history("p", "group-a", limit=2)
    assert [message["content"] for message in latest["messages"]] == ["step 5", "step 6"]
    assert latest["has_more"] is True
    assert latest["next_before"] == latest["messages"][0]["log_id"]

    older = group_chat.history("p", "group-a", before=latest["next_before"], limit=2)
    assert [message["content"] for message in older["messages"]] == ["step 3", "step 4"]

    newest = latest["next_after"]
    newer = group_chat.history("p", "group-a", after=newest)
    assert newer["messages"] == []
    assert newer["next_after"] == newest

    record(ledger, "task.claimed", "step 7", task_id="task")
    newer = group_chat.history("p", "group-a", after=newest)
    assert [message["content"] for message in newer["messages"]] == ["step 7"]
    assert newer["has_more"] is False


def test_group_chat_rejects_unknown_group_and_bad_cursors(group_chat):
    with pytest.raises(CoordinationError, match="小组"):
        group_chat.history("p", "missing")
    with pytest.raises(ValueError, match="游标"):
        group_chat.history("p", "group-a", after=True)
    with pytest.raises(ValueError, match="分页"):
        group_chat.history("p", "group-a", limit=0)
    with pytest.raises(ValueError, match="同时"):
        group_chat.history("p", "group-a", after=1, before=2)
