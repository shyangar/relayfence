import uuid

import pytest

from relayfence.models import OPERATIONS, Mutation
from relayfence.service import Service
from relayfence.store import Store


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Ledger:
    def __init__(self, path):
        self.clock = Clock()
        self.store = Store(path, clock=self.clock)
        self.service = Service(self.store)

    def request(self, operation, **payload):
        if issubclass(OPERATIONS[operation][0], Mutation):
            payload = {"actor": "worker", "request_id": str(uuid.uuid4()), **payload}
        return self.service.call(operation, payload)[0]

    def ok(self, operation, **payload):
        result = self.request(operation, **payload)
        assert result["ok"], result
        return result["data"]

    def error(self, code, operation, **payload):
        result = self.request(operation, **payload)
        assert not result["ok"], result
        assert result["error"]["code"] == code, result
        return result["error"]

    def claim(self, task_id="task", inputs=None, actor="worker", lease_seconds=300):
        self.ok("task_create", task_id=task_id, title=task_id, inputs=inputs or [])
        result = self.ok("task_claim", task_id=task_id, actor=actor, lease_seconds=lease_seconds)
        return {
            "task_id": task_id,
            "actor": actor,
            "fence": result["task"]["fence"],
            "lease_token": result["lease_token"],
        }

    def complete(self, lease, key="artifact", version=0, value=None):
        return self.ok(
            "task_complete",
            **lease,
            outputs=[
                {"key": key, "expected_version": version, "value": value or {"result": "ok"}},
            ],
        )


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger.sqlite3")
