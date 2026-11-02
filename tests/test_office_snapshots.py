"""Named office-configuration snapshots.

The live configuration is saved on every edit, so a snapshot is only useful if it
captures a known-good state that can be restored later and removed when stale.
"""

from __future__ import annotations

import pytest

from relayfence.errors import CoordinationError
from relayfence.models import (
    OfficePut,
    OfficeSnapshotDelete,
    OfficeSnapshotRestore,
    OfficeSnapshotSave,
    Request,
    SnapshotList,
)
from relayfence.store import Store

PROJECT = "p"


def _group(group_id, name="小组", members=1):
    people = [
        {
            "id": f"{group_id}-m{index}",
            "name": f"成员{index}",
            "position": "lead" if index == 0 else "backend",
            "relationship": "leader" if index == 0 else "independent_session",
            "provider": "codex",
            "model": "gpt-5",
        }
        for index in range(members)
    ]
    return {"id": group_id, "name": name, "software": "codex", "members": people}


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
                groups=[_group("g1", "产品", 2), _group("g2", "开发", 1)],
            ),
            1.0,
        )
    return instance


_counter = 0


def _save(store, label, revision=None):
    global _counter
    _counter += 1
    with store.connection() as db:
        if revision is None:
            revision = store._office_config(db, PROJECT)["revision"]
        return store._office_snapshot_save(
            db,
            OfficeSnapshotSave(
                project=PROJECT,
                actor="admin",
                request_id=f"save-{_counter}",
                label=label,
                expected_revision=revision,
            ),
            2.0,
        )


def test_saving_records_the_current_shape(store):
    result = _save(store, "  今天的配置  ")

    assert result["label"] == "今天的配置"
    assert result["group_count"] == 2
    assert result["member_count"] == 3


def test_the_list_is_newest_first(store):
    _save(store, "旧")
    _save(store, "新")

    with store.connection() as db:
        rows = store._office_snapshot_list(db, Request(project=PROJECT), 3.0)["snapshots"]

    assert [row["label"] for row in rows] == ["新", "旧"]


def test_the_list_is_bounded_and_reports_truncation(store):
    for index in range(4):
        _save(store, f"配置{index}")

    with store.connection() as db:
        result = store._office_snapshot_list(db, SnapshotList(project=PROJECT, limit=2), 3.0)

    assert len(result["snapshots"]) == 2
    assert result["truncated"] is True


def test_restoring_brings_back_an_earlier_configuration(store):
    first = _save(store, "原始")
    with store.connection() as db:
        config = store._office_config(db, PROJECT)
        store._office_put(
            db,
            OfficePut(
                project=PROJECT,
                actor="admin",
                request_id="put-2",
                expected_revision=config["revision"],
                groups=[_group("g9", "后来加的", 1)],
            ),
            4.0,
        )

    with store.connection() as db:
        current = store._office_config(db, PROJECT)
        assert [g["name"] for g in current["groups"]] == ["后来加的"]
        restored = store._office_snapshot_restore(
            db,
            OfficeSnapshotRestore(
                project=PROJECT,
                actor="admin",
                request_id="restore-1",
                snapshot_id=first["snapshot_id"],
                expected_revision=current["revision"],
            ),
            5.0,
        )

    assert [g["name"] for g in restored["groups"]] == ["产品", "开发"]
    assert restored["revision"] > current["revision"]


def test_restoring_a_missing_snapshot_is_reported(store):
    with store.connection() as db:
        revision = store._office_config(db, PROJECT)["revision"]
        with pytest.raises(CoordinationError) as caught:
            store._office_snapshot_restore(
                db,
                OfficeSnapshotRestore(
                    project=PROJECT,
                    actor="admin",
                    request_id="restore-missing",
                    snapshot_id="snap_nope",
                    expected_revision=revision,
                ),
                5.0,
            )

    assert caught.value.code == "snapshot_unknown"


def test_a_stale_restore_is_refused_so_concurrent_edits_survive(store):
    saved = _save(store, "原始")
    with store.connection() as db:
        stale = store._office_config(db, PROJECT)["revision"]
        config = store._office_config(db, PROJECT)
        store._office_put(
            db,
            OfficePut(
                project=PROJECT,
                actor="admin",
                request_id="put-3",
                expected_revision=config["revision"],
                groups=[_group("g9", "并发修改", 1)],
            ),
            6.0,
        )
        with pytest.raises(CoordinationError) as caught:
            store._office_snapshot_restore(
                db,
                OfficeSnapshotRestore(
                    project=PROJECT,
                    actor="admin",
                    request_id="restore-stale",
                    snapshot_id=saved["snapshot_id"],
                    expected_revision=stale,
                ),
                7.0,
            )

    assert caught.value.code == "revision_conflict"


def test_deleting_a_snapshot_leaves_the_live_config_alone(store):
    saved = _save(store, "临时")
    with store.connection() as db:
        before = store._office_config(db, PROJECT)
        remaining = store._office_snapshot_delete(
            db,
            OfficeSnapshotDelete(
                project=PROJECT,
                actor="admin",
                request_id="del-1",
                snapshot_id=saved["snapshot_id"],
            ),
            8.0,
        )
        after = store._office_config(db, PROJECT)

    assert remaining["snapshots"] == []
    assert after["groups"] == before["groups"]
    assert after["revision"] == before["revision"]


def test_saves_and_restores_are_audited(store):
    saved = _save(store, "留痕")
    with store.connection() as db:
        kinds = {
            row[0]
            for row in db.execute(
                "SELECT kind FROM events WHERE project=? AND kind LIKE 'office.snapshot%'",
                (PROJECT,),
            )
        }

    assert "office.snapshot_saved" in kinds
    assert saved["snapshot_id"]


def test_an_empty_label_is_rejected(store):
    with store.connection() as db:
        revision = store._office_config(db, PROJECT)["revision"]
        with pytest.raises(CoordinationError):
            store._office_snapshot_save(
                db,
                OfficeSnapshotSave(
                    project=PROJECT,
                    actor="admin",
                    request_id="save-blank",
                    label="   ",
                    expected_revision=revision,
                ),
                2.0,
            )
