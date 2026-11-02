"""Run activity reported by the dispatcher that is actually executing the task.

Only the dispatcher process can see a vendor's stream, so it forwards what happened
and the ledger stores it. These tests pin the accepting side of that contract.
"""

from __future__ import annotations

import pytest

from relayfence.errors import CoordinationError
from relayfence.models import (
    OfficePut,
    TaskActivity,
    TaskActivityEntry,
    TaskActivityList,
)
from relayfence.store import Store

PROJECT = "p"


def _group(group_id="g1", member_id="m1"):
    return {
        "id": group_id,
        "name": "小组",
        "software": "codex",
        "members": [
            {
                "id": member_id,
                "name": "甲",
                "position": "lead",
                "relationship": "leader",
                "provider": "codex",
                "model": "gpt-5",
            }
        ],
    }


@pytest.fixture
def store(tmp_path):
    instance = Store(str(tmp_path / "ledger.sqlite3"))
    with instance.connection() as db:
        instance._office_put(
            db,
            OfficePut(
                project=PROJECT,
                actor="admin",
                request_id="put-1",
                expected_revision=0,
                groups=[_group()],
            ),
            1.0,
        )
    return instance


def _insert_task(store, task_id, role_id):
    with store.connection() as db:
        db.execute(
            "INSERT INTO tasks(project,task_id,title,description,inputs,status,revision,"
            "fence,snapshot,outputs,created_at,updated_at,priority,depends_on,"
            "task_snapshot,role_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                PROJECT,
                task_id,
                "任务",
                "",
                "[]",
                "running",
                1,
                1,
                "{}",
                "[]",
                1.0,
                1.0,
                1,
                "[]",
                "[]",
                role_id,
            ),
        )


def _office_task(store, task_id="t1", group_id="g1"):
    """Create a task whose role_id points at the office member in ``group_id``."""
    _insert_task(store, task_id, "office/m1")
    with store.connection() as db:
        db.execute(
            "INSERT INTO meeting_tasks VALUES (?,?,?,?,?,?)",
            (PROJECT, task_id, "meet-1", "m1", group_id, "executing"),
        )


def _report(store, entries, task_id="t1", member_id="m1", request_id="act-1"):
    with store.connection() as db:
        return store._task_activity(
            db,
            TaskActivity(
                project=PROJECT,
                actor="admin",
                request_id=request_id,
                task_id=task_id,
                member_id=member_id,
                entries=entries,
            ),
            10.0,
        )


def test_activity_is_recorded_and_read_back_in_order(store):
    _office_task(store)
    _report(
        store,
        [
            TaskActivityEntry(kind="note", text="开始读取文件"),
            TaskActivityEntry(kind="command", title="Get-ChildItem", text="列出目录"),
        ],
    )

    with store.connection() as db:
        page = store._task_activity_list(db, TaskActivityList(project=PROJECT, limit=50), 11.0)

    assert [e["kind"] for e in page["entries"]] == ["note", "command"]
    assert page["entries"][1]["title"] == "Get-ChildItem"
    assert page["entries"][1]["text"] == "列出目录"
    assert page["has_more"] is False


def test_activity_is_visible_in_the_group_conversation(store):
    """The dashboard reads group_logs, so the entry must appear as a normal log."""
    _office_task(store)
    _report(store, [TaskActivityEntry(kind="command", title="shell", text="执行了命令")])

    with store.connection() as db:
        rows = db.execute(
            "SELECT kind,message FROM group_logs WHERE project=? AND task_id=?",
            (PROJECT, "t1"),
        ).fetchall()

    assert [r["kind"] for r in rows] == ["run.activity"]
    assert rows[0]["message"] == "shell"


def test_paging_uses_the_returned_cursor(store):
    _office_task(store)
    _report(store, [TaskActivityEntry(kind="note", text=f"第{i}步") for i in range(5)])

    with store.connection() as db:
        first = store._task_activity_list(db, TaskActivityList(project=PROJECT, limit=2), 11.0)
        assert first["has_more"] is True
        assert len(first["entries"]) == 2
        second = store._task_activity_list(
            db,
            TaskActivityList(project=PROJECT, limit=2, after_id=first["next_after"]),
            11.0,
        )

    assert len(second["entries"]) == 2
    assert second["entries"][0]["id"] > first["entries"][-1]["id"]


def test_activity_can_be_filtered_by_task(store):
    _office_task(store, "t1")
    _office_task(store, "t2")
    _report(store, [TaskActivityEntry(kind="note", text="属于t1")], task_id="t1", request_id="a1")
    _report(store, [TaskActivityEntry(kind="note", text="属于t2")], task_id="t2", request_id="a2")

    with store.connection() as db:
        page = store._task_activity_list(
            db, TaskActivityList(project=PROJECT, task_id="t2", limit=50), 11.0
        )

    assert [e["text"] for e in page["entries"]] == ["属于t2"]


def test_a_task_without_a_group_accepts_activity_quietly(store):
    """A standalone task has nowhere to show activity; that must not fail the run."""
    _insert_task(store, "solo", None)

    result = _report(store, [TaskActivityEntry(kind="note", text="无小组")], task_id="solo")

    assert result == {"task_id": "solo", "recorded": 0}


def test_a_cancelled_task_rejects_activity(store):
    _office_task(store)
    with store.connection() as db:
        db.execute(
            "UPDATE tasks SET status='cancelled' WHERE project=? AND task_id=?", (PROJECT, "t1")
        )

    with pytest.raises(CoordinationError) as caught:
        _report(store, [TaskActivityEntry(kind="note", text="迟到")])

    assert caught.value.code == "task_cancelled"


def test_unknown_task_is_rejected(store):
    with pytest.raises(CoordinationError) as caught:
        _report(store, [TaskActivityEntry(kind="note", text="x")], task_id="nope")

    assert caught.value.code == "not_found"


def test_error_entries_are_recorded_at_error_level(store):
    _office_task(store)
    _report(store, [TaskActivityEntry(kind="error", text="额度不足", status="failed")])

    with store.connection() as db:
        row = db.execute(
            "SELECT level FROM group_logs WHERE project=? AND task_id=?", (PROJECT, "t1")
        ).fetchone()

    assert row["level"] == "error"


def test_deleting_the_task_removes_its_activity(store):
    _office_task(store)
    _report(store, [TaskActivityEntry(kind="note", text="将被清理")])

    with store.connection() as db:
        db.execute("UPDATE tasks SET status='done' WHERE project=? AND task_id=?", (PROJECT, "t1"))
        from relayfence.models import TaskDelete

        store._task_delete(
            db,
            TaskDelete(
                project=PROJECT,
                actor="admin",
                request_id="del-1",
                expected_revision=1,
                task_id="t1",
            ),
            12.0,
        )
        left = db.execute(
            "SELECT COUNT(*) n FROM group_logs WHERE project=? AND kind='run.activity'",
            (PROJECT,),
        ).fetchone()

    assert left["n"] == 0
