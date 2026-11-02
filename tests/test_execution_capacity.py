import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from relayfence.execution_capacity import ExecutionCapacity


@pytest.mark.parametrize("limit", [0, 17, True, 1.5])
def test_invalid_capacity(limit):
    with pytest.raises(ValueError):
        ExecutionCapacity(limit)


def test_global_limit_workspace_exclusion_and_fair_turns(tmp_path):
    capacity = ExecutionCapacity(1)
    first = capacity.reserve("a", "project-a", "t1", tmp_path / "a")
    assert first
    assert capacity.reserve("b", "project-b", "t2", tmp_path / "b") is None
    capacity.release(first)
    # A busy project cannot jump ahead of a different project already waiting.
    assert capacity.reserve("a", "project-a", "t3", tmp_path / "a") is None
    second = capacity.reserve("b", "project-b", "t2", tmp_path / "b")
    assert second
    view = capacity.snapshot("project-a")
    assert view["active_total"] == 1 and not view["active"]
    assert view["waiting"][0]["reason"] == "capacity_full"
    capacity.withdraw("a")
    capacity.release(second)
    capacity.release(second)
    assert not capacity.snapshot()["active"]


def test_same_directory_alias_is_exclusive_but_other_directory_runs(tmp_path):
    capacity = ExecutionCapacity(2)
    ticket = capacity.reserve("a", "one", "t1", tmp_path)
    assert capacity.reserve("b", "two", "t2", tmp_path / ".") is None
    assert capacity.snapshot("two")["waiting"][0]["reason"] == "workspace_busy"
    assert capacity.reserve("c", "three", "t3", tmp_path / "other")
    capacity.withdraw("a")
    # Withdrawing queued intent must never release an active process's permit.
    assert capacity.snapshot()["active_total"] == 2
    capacity.release(ticket)
    assert capacity.reserve("b", "two", "t2", tmp_path)


def test_dead_waiter_does_not_block_and_snapshot_has_no_private_paths(tmp_path):
    now = [0.0]
    capacity = ExecutionCapacity(1, clock=lambda: now[0])
    ticket = capacity.reserve("a", "one", "task", tmp_path)
    capacity.reserve("dead", "two", "task", tmp_path / "two")
    capacity.release(ticket)
    now[0] = 31
    assert capacity.reserve("live", "three", "task", tmp_path / "three")
    assert str(tmp_path) not in str(capacity.snapshot())
    assert not capacity.snapshot()["waiting"]


def test_parallel_admission_never_exceeds_limit(tmp_path):
    capacity = ExecutionCapacity(3)
    barrier = threading.Barrier(12)

    def reserve(i):
        barrier.wait()
        return capacity.reserve(str(i), str(i), "task", tmp_path / str(i))

    with ThreadPoolExecutor(max_workers=12) as pool:
        tickets = list(pool.map(reserve, range(12)))
    assert sum(ticket is not None for ticket in tickets) == 3
    assert capacity.snapshot()["active_total"] == 3
