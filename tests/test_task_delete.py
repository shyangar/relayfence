"""Permanent task deletion.

Foreign keys are enforced in this store, so a delete that misses a child row fails
outright. These tests pin the child cleanup, the audit record, and the two refusals
that keep the dependency graph and a live worker consistent.
"""

from __future__ import annotations

import json

import pytest

from relayfence.errors import CoordinationError
from relayfence.models import TaskDelete
from relayfence.store import Store

PROJECT = "p"


@pytest.fixture
def store(tmp_path):
    instance = Store(str(tmp_path / "ledger.sqlite3"))
    with instance.connection() as db:
        for task_id, status, expires_at in (
            ("t-parent", "done", None),
            ("t-child", "failed", None),
            ("t-free", "cancelled", None),
            ("t-running", "running", 9_999.0),
        ):
            db.execute(
                "INSERT INTO tasks(project,task_id,title,description,inputs,status,"
                "revision,fence,snapshot,outputs,created_at,updated_at,expires_at,depends_on) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    PROJECT,
                    task_id,
                    task_id,
                    "",
                    "[]",
                    status,
                    1,
                    0,
                    "[]",
                    "[]",
                    1.0,
                    1.0,
                    expires_at,
                    '["t-parent"]' if task_id == "t-child" else "[]",
                ),
            )
        db.execute(
            "INSERT INTO meeting_tasks(project,group_id,member_id,task_id,meeting_id,stage) "
            "VALUES (?,?,?,?,?,?)",
            (PROJECT, "g", "m", "t-free", "meet", "s1"),
        )
        db.execute(
            "INSERT INTO member_messages(project,member_id,task_id,request_id,content,"
            "status,detail,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (PROJECT, "m", "t-free", "rq-1", "hi", "done", "", 1.0, 1.0),
        )
    return instance


def _delete(store, task_id, now=1_000.0):
    with store.connection() as db:
        revision = db.execute(
            "SELECT revision FROM tasks WHERE project=? AND task_id=?", (PROJECT, task_id)
        ).fetchone()[0]
        request = TaskDelete(
            project=PROJECT,
            task_id=task_id,
            expected_revision=revision,
            actor="admin",
            request_id="req-" + task_id,
        )
        return store._task_delete(db, request, now)


def _count(store, table, task_id):
    with store.connection() as db:
        return db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE project=? AND task_id=?", (PROJECT, task_id)
        ).fetchone()[0]


def test_delete_removes_the_task_and_its_child_rows(store):
    assert _delete(store, "t-free") == {"task_id": "t-free", "deleted": True}

    assert _count(store, "tasks", "t-free") == 0
    assert _count(store, "meeting_tasks", "t-free") == 0
    assert _count(store, "member_messages", "t-free") == 0


def test_deletion_is_recorded_in_the_audit_trail(store):
    _delete(store, "t-free")

    with store.connection() as db:
        row = db.execute(
            "SELECT details FROM events WHERE project=? AND subject=? AND kind='task.deleted'",
            (PROJECT, "t-free"),
        ).fetchone()

    assert row is not None
    assert "cancelled" in row[0]


def test_dependents_block_deletion(store):
    with pytest.raises(CoordinationError) as caught:
        _delete(store, "t-parent")

    assert caught.value.code == "task_depended_on"
    assert "t-child" in str(caught.value)
    assert _count(store, "tasks", "t-parent") == 1


def test_a_running_task_with_a_live_lease_is_refused(store):
    with pytest.raises(CoordinationError) as caught:
        _delete(store, "t-running", now=1_000.0)

    assert caught.value.code == "task_running"
    assert _count(store, "tasks", "t-running") == 1


def test_a_running_task_with_an_expired_lease_can_be_deleted(store):
    """The worker is gone, so nothing will publish into the deleted row."""
    _delete(store, "t-running", now=10_000.0)

    assert _count(store, "tasks", "t-running") == 0


def test_stale_revision_is_rejected(store):
    with pytest.raises(CoordinationError) as caught:
        with store.connection() as db:
            store._task_delete(
                db,
                TaskDelete(
                    project=PROJECT,
                    task_id="t-free",
                    expected_revision=99,
                    actor="admin",
                    request_id="req-stale",
                ),
                1_000.0,
            )

    assert caught.value.code == "revision_conflict"
    assert _count(store, "tasks", "t-free") == 1


def test_deletion_audit_uses_the_meeting_group_before_clearing_it(store):
    _delete(store, "t-free")

    with store.connection() as db:
        row = db.execute(
            "SELECT group_id,member_id FROM group_logs "
            "WHERE project=? AND task_id=? AND kind='task.deleted'",
            (PROJECT, "t-free"),
        ).fetchone()

    assert row is not None
    assert row["group_id"] == "g"
    assert row["member_id"] == "m"


def test_deleting_leaves_other_tasks_untouched(store):
    _delete(store, "t-free")

    with store.connection() as db:
        remaining = {
            row[0] for row in db.execute("SELECT task_id FROM tasks WHERE project=?", (PROJECT,))
        }

    assert remaining == {"t-parent", "t-child", "t-running"}


def test_claim_snapshot_reference_blocks_delete(ledger):
    ledger.ok("task_create", task_id="parent", title="Parent")
    ledger.ok("task_create", task_id="child", title="Child")
    with ledger.store.connection() as db:
        db.execute(
            "UPDATE tasks SET task_snapshot=? WHERE project='default' AND task_id='child'",
            (json.dumps([{"task_id": "parent", "fence": 0}]),),
        )
    parent = ledger.ok("task_get", task_id="parent")

    ledger.error(
        "task_depended_on",
        "task_delete",
        task_id="parent",
        expected_revision=parent["revision"],
    )


def test_meeting_waiting_reference_blocks_delete(ledger):
    ledger.ok("task_create", task_id="parent", title="Parent")
    with ledger.store.connection() as db:
        db.execute(
            "INSERT INTO meetings(project,meeting_id,definition) VALUES (?,?,?)",
            ("default", "meeting", json.dumps({"waiting": ["parent"]})),
        )
    parent = ledger.ok("task_get", task_id="parent")

    ledger.error(
        "task_depended_on",
        "task_delete",
        task_id="parent",
        expected_revision=parent["revision"],
    )


def test_a_deleted_task_id_cannot_be_reused(ledger):
    task = ledger.ok("task_create", task_id="once", title="Once")
    ledger.ok("task_delete", task_id="once", expected_revision=task["revision"])

    ledger.error("already_exists", "task_create", task_id="once", title="Again")


def test_an_unrelated_meeting_does_not_shadow_the_task_row(ledger):
    ledger.ok("task_create", task_id="parent", title="Parent")
    with ledger.store.connection() as db:
        db.execute(
            "INSERT INTO meetings(project,meeting_id,definition) VALUES (?,?,?)",
            ("default", "meeting", json.dumps({"waiting": ["someone-else"]})),
        )
    parent = ledger.ok("task_get", task_id="parent")

    ledger.ok("task_delete", task_id="parent", expected_revision=parent["revision"])

    events = ledger.ok("events_list")["events"]
    deleted = next(event for event in events if event["kind"] == "task.deleted")
    assert deleted["details"] == {"previous_status": "ready", "title": "Parent"}
