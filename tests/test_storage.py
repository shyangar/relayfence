import json

from relayfence.storage import ProjectStore, project_id
from relayfence.store import Store


def test_project_index_migrates_legacy_json_into_sqlite(ledger, tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    project = tmp_path / "demo"
    project.mkdir()
    ident = project_id(project)
    (private / "projects.json").write_text(
        json.dumps({ident: {"id": ident, "name": "Demo", "path": str(project)}})
    )

    projects = ProjectStore(ledger.store, private)
    projects.migrate_legacy()

    assert not (private / "projects.json").exists()
    assert projects.as_dict()[ident] == {
        "id": ident,
        "name": "demo",
        "path": str(project),
    }
    with ledger.store.connection() as db:
        assert db.execute("SELECT count(*) FROM local_projects").fetchone()[0] == 1


def test_partial_legacy_import_keeps_the_source_file(ledger, tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    project = tmp_path / "demo"
    project.mkdir()
    ident = project_id(project)
    (private / "projects.json").write_text(
        json.dumps(
            {
                ident: {"id": ident, "name": "Demo", "path": str(project)},
                "broken": {"id": "broken", "name": "Broken", "path": "relative/path"},
            }
        ),
        encoding="utf-8",
    )

    projects = ProjectStore(ledger.store, private)
    projects.migrate_legacy()

    assert (private / "projects.json").exists()
    assert ident in projects.as_dict()


def test_oversized_legacy_project_list_is_refused_not_truncated(ledger, tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    (private / "projects.json").write_text(
        json.dumps({str(index): {"path": str(tmp_path)} for index in range(1001)}),
        encoding="utf-8",
    )

    projects = ProjectStore(ledger.store, private)

    try:
        projects.migrate_legacy()
    except ValueError as error:
        assert "too large" in str(error)
    else:
        raise AssertionError("oversized project list must be refused")
    assert (private / "projects.json").exists()


def test_group_logs_are_paginated_filterable_and_analysed(ledger):
    with ledger.store.connection() as db:
        for index in range(7):
            ledger.clock.advance(1)
            ledger.store.record_db(
                db,
                "default",
                "group-a",
                "test.event",
                f"message {index}",
                level="error" if index == 5 else "info",
                member_id="member-a" if index < 2 else None,
                task_id=f"task-{index}",
                details={"index": index},
            )

    latest = ledger.ok("group_logs_list", group_id="group-a", limit=3)
    assert [entry["message"] for entry in latest["logs"]] == [
        "message 6",
        "message 5",
        "message 4",
    ]
    assert latest["has_more"] is True
    assert latest["next_cursor"] == latest["logs"][-1]["id"]
    assert latest["analysis"]["total_logs"] == 7
    assert latest["analysis"]["by_level"]["error"] == 1

    older = ledger.ok(
        "group_logs_list",
        group_id="group-a",
        limit=3,
        before=latest["next_cursor"],
    )
    assert [entry["message"] for entry in older["logs"]] == [
        "message 3",
        "message 2",
        "message 1",
    ]
    assert older["has_more"] is True

    errors = ledger.ok("group_logs_list", group_id="group-a", level="error")
    assert [entry["message"] for entry in errors["logs"]] == ["message 5"]
    searched = ledger.ok("group_logs_list", group_id="group-a", query="message 2")
    assert [entry["message"] for entry in searched["logs"]] == ["message 2"]


def test_migration_backfills_task_events_for_assigned_groups(ledger):
    ledger.ok("task_create", task_id="task-a", title="已有任务")
    with ledger.store.connection() as db:
        db.execute(
            "INSERT INTO meeting_tasks(project,task_id,meeting_id,member_id,group_id,stage) "
            "VALUES ('default','task-a','brief','member-a','group-a','执行')"
        )
        db.execute(
            "INSERT INTO events(project,actor,kind,subject,details,created_at) "
            "VALUES ('default','coder','task.claimed','task-a','{\"fence\":1}',2)"
        )
        db.execute("DROP TABLE group_logs")
        db.execute("PRAGMA user_version=6")

    reopened = Store(ledger.store.path, clock=ledger.clock)
    with reopened.connection() as db:
        rows = db.execute(
            "SELECT group_id,member_id,kind,level,message FROM group_logs ORDER BY id"
        ).fetchall()
        assert db.execute("PRAGMA user_version").fetchone()[0] == 12
    assert [(row["group_id"], row["member_id"]) for row in rows] == [
        ("group-a", "member-a"),
        ("group-a", "member-a"),
    ]
    assert [row["kind"] for row in rows] == ["task.created", "task.claimed"]
    assert [row["level"] for row in rows] == ["info", "info"]
    assert [row["message"] for row in rows] == ["任务已创建", "任务开始执行"]


def test_group_logs_hide_credential_fields(ledger):
    with ledger.store.connection() as db:
        ledger.store.record_db(
            db,
            "default",
            "group-a",
            "test.event",
            "safe",
            details={"token": "secret", "nested": {"lease_token": "secret"}, "kept": 1},
        )
    entry = ledger.ok("group_logs_list", group_id="group-a")["logs"][0]
    assert entry["details"] == {"nested": {}, "kept": 1}
