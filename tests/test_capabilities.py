"""Capability discovery between the browser assets and the running server.

Static assets are re-read from disk on every request while Python modules are imported
once per process. A browser can therefore be newer than the server; it must ask what
the server supports instead of failing the whole view on an unknown operation.
"""

from __future__ import annotations

import pytest

from relayfence.models import OPERATIONS
from relayfence.service import Service
from relayfence.store import Store


@pytest.fixture
def service(tmp_path):
    return Service(Store(str(tmp_path / "ledger.sqlite3")))


def test_capabilities_lists_the_loaded_registry(service):
    result, status = service.call("capabilities", {})

    assert status == 200
    assert result["ok"] is True
    assert result["data"]["operations"] == sorted(OPERATIONS)
    assert "office_snapshot_list" in result["data"]["operations"]


def test_capabilities_uses_the_same_envelope_as_every_operation(service):
    """The browser reads ``result.data`` for all operations, so this must match."""
    result, _ = service.call("capabilities", {})

    assert set(result) == {"ok", "data", "replayed"}
    assert result["replayed"] is False


def test_capabilities_needs_no_project_or_actor(service):
    """A browser asks before it knows which project it will open."""
    result, status = service.call("capabilities", {})

    assert status == 200
    assert isinstance(result["data"]["operations"], list)
    assert result["data"]["schema"] >= 9


def test_capabilities_is_stable_across_calls(service):
    """The answer reflects the process's code, so repeated calls must not drift."""
    first = service.call("capabilities", {})[0]
    second = service.call("capabilities", {})[0]

    assert first == second


def test_every_registered_operation_is_announced(service):
    announced = set(service.call("capabilities", {})[0]["data"]["operations"])

    assert announced == set(OPERATIONS)


def test_announced_operations_are_callable(service):
    """Advertising an operation that cannot be dispatched would break the contract."""
    announced = service.call("capabilities", {})[0]["data"]["operations"]

    for operation in announced:
        model = OPERATIONS[operation][0]
        assert hasattr(Store, "_" + operation) or operation == "capabilities", operation
        # Every announced operation must also have a usable request model.
        assert model is not None


def test_unknown_operation_is_still_rejected(service):
    result, status = service.call("office_snapshot_nonexistent", {})

    assert status == 404
    assert result["error"]["code"] == "unknown_operation"
