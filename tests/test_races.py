import multiprocessing
import os
from pathlib import Path

import pytest

from relayfence.models import TaskComplete
from relayfence.service import Service
from relayfence.store import Store


def contender(path, gate, ready, results, operation, payload):
    service = Service(Store(path))
    ready.put(True)
    if not gate.wait(20):
        return
    results.put(service.call(operation, payload)[0])


@pytest.mark.parametrize("operation", ["task_claim", "state_put", "replayed_state_put"])
def test_independent_process_races_have_one_committed_effect(ledger, operation):
    if operation == "task_claim":
        ledger.ok("task_create", task_id="task", title="Task")
        base = {"task_id": "task"}
    else:
        base = {"key": "spec", "expected_version": 0, "value": 1}
    context = multiprocessing.get_context("spawn")
    gate, ready, results = context.Event(), context.Queue(), context.Queue()
    processes = []
    for index in range(6):
        payload = {
            **base,
            "actor": "racer",
            "request_id": "same" if operation == "replayed_state_put" else f"race-{index}",
        }
        process = context.Process(
            target=contender,
            args=(
                str(ledger.store.path),
                gate,
                ready,
                results,
                "state_put" if operation == "replayed_state_put" else operation,
                payload,
            ),
        )
        processes.append(process)
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=30)
        gate.set()
        responses = [results.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        if operation == "replayed_state_put":
            assert all(r["ok"] for r in responses)
            assert sum(not r["replayed"] for r in responses) == 1
        else:
            assert sum(r["ok"] for r in responses) == 1, responses
            code = "task_busy" if operation == "task_claim" else "version_conflict"
            assert all(r["ok"] or r["error"]["code"] == code for r in responses)
        events = ledger.ok("events_list")["events"]
        assert len(events) == (2 if operation == "task_claim" else 1)
    finally:
        gate.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=10)
        ready.close()
        results.close()


class CrashDuringCommit(Store):
    @staticmethod
    def _event(db, request, now, kind, subject, details):
        Store._event(db, request, now, kind, subject, details)
        os._exit(23)  # Actual abrupt process loss after writes, before receipt/COMMIT.


def crash_during_complete(path, request, now):
    CrashDuringCommit(Path(path), clock=lambda: now).execute(
        "task_complete", TaskComplete(**request)
    )


def test_process_loss_before_commit_rolls_back_artifacts_task_and_events(ledger):
    lease = ledger.claim()
    request = {
        **lease,
        "request_id": "complete",
        "outputs": [
            {"key": "artifact", "expected_version": 0, "value": "result"},
        ],
    }
    before = ledger.ok("events_list")
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=crash_during_complete, args=(str(ledger.store.path), request, ledger.clock.now)
    )
    process.start()
    try:
        process.join(timeout=30)
        assert process.exitcode == 23
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
    recovered = Service(Store(ledger.store.path, clock=ledger.clock))
    assert recovered.call("item_get", {"key": "artifact"})[1] == 404
    assert recovered.call("task_get", {"task_id": "task"})[0]["data"]["status"] == "running"
    assert recovered.call("events_list", {})[0]["data"] == before
    result, status = recovered.call("task_complete", request)
    assert status == 200 and not result["replayed"]
