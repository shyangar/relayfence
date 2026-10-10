import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files

import pytest

from relayfence.identity import Principal
from relayfence.service import Service
from relayfence.store import Store, digest, encode

SELECTION = {"model": "model-a", "parameters": {"effort": "high"}}
MODELS = [{"id": "model-a", "parameters": {"effort": ["low", "high"]}}, {"id": "model-b"}]


def register(ledger, agent_id="coder", **extra):
    profile = ledger.ok(
        "agent_put",
        agent_id=agent_id,
        expected_revision=0,
        label=agent_id,
        runtime="custom",
        capabilities=["code"],
        models=MODELS,
        **extra,
    )
    token = ledger.ok(
        "agent_token_rotate", agent_id=agent_id, expected_revision=profile["revision"]
    )["token"]
    return Principal(token_hash=digest(token)), token


def bind(ledger, **extra):
    return ledger.ok(
        "role_put",
        **{
            "role_id": "developer",
            "expected_revision": 0,
            "label": "Developer",
            "agent_id": "coder",
            "selection": SELECTION,
            "required_capabilities": ["code"],
            "instructions": "Implement with tests",
            "deliverables": ["Result"],
            **extra,
        },
    )


def as_agent(ledger, principal, operation, **extra):
    payload = {"actor": "coder", "request_id": uuid.uuid4().hex, **extra}
    result, _ = ledger.service.call(operation, payload, principal)
    return result


def claim(ledger, principal, **extra):
    result = as_agent(ledger, principal, "task_claim", task_id="t", selection=SELECTION, **extra)
    assert result["ok"], result
    data = result["data"]
    return data, {
        "task_id": "t",
        "fence": data["task"]["fence"],
        "lease_token": data["lease_token"],
    }


@pytest.fixture
def assigned(ledger):
    principal, token = register(ledger)
    bind(ledger)
    ledger.ok("task_create", task_id="t", title="Work", role_id="developer")
    return ledger, principal, token


def test_identity_model_selection_and_immutable_running_snapshot(assigned):
    ledger, principal, _ = assigned
    assert (
        ledger.error("assignment_mismatch", "task_claim", task_id="t")["details"]["agent_id"]
        == "coder"
    )
    ledger.error(
        "agent_credential_required", "task_claim", actor="coder", task_id="t", selection=SELECTION
    )
    for selection in [
        None,
        {"model": "model-b"},
        {"model": "model-a", "parameters": {"effort": "low"}},
    ]:
        result = as_agent(ledger, principal, "task_claim", task_id="t", selection=selection)
        assert result["error"]["code"] == "selection_mismatch"
    first, lease = claim(ledger, principal)
    assert first["task"]["execution"]["role"]["instructions"] == "Implement with tests"
    bind(
        ledger, expected_revision=1, selection={"model": "model-b"}, instructions="New instructions"
    )
    done = as_agent(
        ledger,
        principal,
        "task_complete",
        **lease,
        outputs=[{"key": "result", "expected_version": 0, "value": "ok"}],
        reported_model="model-a",
    )
    assert done["ok"], done
    execution = done["data"]["task"]["execution"]
    assert execution["requested"] == SELECTION
    assert execution["role"]["revision"] == 1
    assert (
        execution["reported_model"] == "model-a"
        and execution["model_evidence"] == "worker_reported"
    )
    task = done["data"]["task"]
    ledger.ok("task_reopen", task_id="t", expected_revision=task["revision"])
    result = as_agent(ledger, principal, "task_claim", task_id="t", selection={"model": "model-b"})
    assert result["data"]["task"]["execution"]["role"]["revision"] == 2


def test_credential_scope_spoofing_revocation_and_receipt_access(assigned):
    ledger, principal, token = assigned
    result = as_agent(
        ledger, principal, "state_put", actor="pretend", key="x", expected_version=0, value=1
    )
    assert result["error"]["code"] == "actor_mismatch"
    assert (
        as_agent(
            ledger, principal, "state_put", project="other", key="x", expected_version=0, value=1
        )["error"]["code"]
        == "forbidden"
    )
    assert (
        as_agent(ledger, principal, "agent_token_rotate", agent_id="coder", expected_revision=2)[
            "error"
        ]["code"]
        == "admin_required"
    )
    data, lease = claim(ledger, principal, request_id="same")
    assert as_agent(
        ledger, principal, "task_claim", task_id="t", selection=SELECTION, request_id="same"
    )["replayed"]
    replacement = ledger.ok("agent_token_rotate", agent_id="coder", expected_revision=2)
    assert not ledger.store.credential_valid(digest(token))
    assert (
        as_agent(
            ledger, principal, "task_claim", task_id="t", selection=SELECTION, request_id="same"
        )["error"]["code"]
        == "unauthorized"
    )
    principal = Principal(token_hash=digest(replacement["token"]))
    assert as_agent(ledger, principal, "task_heartbeat", **lease)["ok"]
    profile = ledger.ok("agent_get", agent_id="coder")
    ledger.ok(
        "agent_put",
        agent_id="coder",
        expected_revision=profile["revision"],
        label="Disabled",
        runtime="custom",
        enabled=False,
    )
    assert as_agent(ledger, principal, "task_release", **lease)["error"]["code"] == "unauthorized"
    public = (
        encode(ledger.ok("agents_list"))
        + encode(ledger.ok("events_list"))
        + encode(ledger.ok("task_get", task_id="t"))
    )
    assert (
        token not in public
        and replacement["token"] not in public
        and data["lease_token"] not in public
    )
    assert "token_hash" not in public


@pytest.mark.parametrize(
    "selection,capabilities,code",
    [
        ({"model": "unknown"}, ["code"], "unsupported_model"),
        ({"model": "model-a", "parameters": {"effort": "ultra"}}, [], "unsupported_parameters"),
        ({"model": "model-a", "parameters": {"temperature": "1"}}, [], "unsupported_parameters"),
        ({"model": None, "parameters": {"effort": "high"}}, [], "model_required"),
        (SELECTION, ["deploy"], "capability_mismatch"),
    ],
)
def test_role_selection_rejects_unsupported_combinations(ledger, selection, capabilities, code):
    register(ledger)
    ledger.error(
        code,
        "role_put",
        role_id="role",
        expected_revision=0,
        label="Role",
        agent_id="coder",
        selection=selection,
        required_capabilities=capabilities,
    )
    assert ledger.ok("roles_list")["roles"] == []


def test_external_unknown_model_and_reporting_are_honest(ledger):
    principal, _ = register(ledger)
    bind(ledger, selection={"model": None})
    ledger.ok("task_create", task_id="t", title="Test", role_id="developer")
    result = as_agent(ledger, principal, "task_claim", task_id="t", selection={})["data"]
    assert result["task"]["execution"]["model_evidence"] == "unverified"
    lease = {"task_id": "t", "lease_token": result["lease_token"], "fence": result["task"]["fence"]}
    done = as_agent(
        ledger,
        principal,
        "task_complete",
        **lease,
        outputs=[{"key": "x", "expected_version": 0, "value": None}],
    )
    assert done["data"]["task"]["execution"]["reported_model"] is None


def test_mismatched_report_cannot_publish_outputs(assigned):
    ledger, principal, _ = assigned
    _, lease = claim(ledger, principal)
    result = as_agent(
        ledger,
        principal,
        "task_complete",
        **lease,
        reported_model="model-b",
        outputs=[{"key": "x", "expected_version": 0, "value": 1}],
    )
    assert result["error"]["code"] == "model_mismatch"
    assert ledger.ok("items_list")["items"] == []
    assert ledger.ok("task_get", task_id="t")["status"] == "running"


def test_reassign_requires_revision_and_no_live_lease(assigned):
    ledger, principal, _ = assigned
    data, lease = claim(ledger, principal, lease_seconds=1)
    rev = data["task"]["revision"]
    ledger.error("revision_conflict", "task_assign", task_id="t", expected_revision=1, role_id=None)
    ledger.error(
        "invalid_transition", "task_assign", task_id="t", expected_revision=rev, role_id=None
    )
    ledger.clock.advance(2)
    task = ledger.ok("task_assign", task_id="t", expected_revision=rev, role_id=None)
    assert task["status"] == "ready" and task["execution"] is None
    assert as_agent(ledger, principal, "task_heartbeat", **lease)["error"]["code"] == "lease_lost"
    lease2 = ledger.ok("task_claim", task_id="t")
    assert lease2["task"]["fence"] == lease["fence"] + 1


def test_role_rebinding_does_not_revoke_current_owner(assigned):
    ledger, principal, _ = assigned
    _, lease = claim(ledger, principal)
    other, _ = register(ledger, agent_id="reviewer")
    bind(ledger, expected_revision=1, agent_id="reviewer")
    assert as_agent(ledger, principal, "task_release", **lease)["ok"]
    assert (
        as_agent(ledger, principal, "task_claim", task_id="t", selection=SELECTION)["error"]["code"]
        == "assignment_mismatch"
    )
    next_claim = as_agent(
        ledger, other, "task_claim", actor="reviewer", task_id="t", selection=SELECTION
    )
    assert next_claim["data"]["task"]["owner"] == "reviewer"


def test_agent_edit_invalidates_future_claim_until_role_fixed(assigned):
    ledger, principal, _ = assigned
    ledger.ok(
        "agent_put",
        agent_id="coder",
        expected_revision=2,
        label="Changed",
        runtime="custom",
        capabilities=["code"],
        models=[{"id": "model-b"}],
    )
    assert (
        as_agent(ledger, principal, "task_claim", task_id="t", selection=SELECTION)["error"]["code"]
        == "unsupported_model"
    )
    ledger.ok(
        "agent_put",
        agent_id="coder",
        expected_revision=3,
        label="Changed",
        runtime="custom",
        enabled=False,
    )
    ledger.error(
        "agent_disabled",
        "role_put",
        role_id="developer",
        expected_revision=1,
        label="Role",
        agent_id="coder",
    )


def test_registry_pagination_missing_and_simultaneous_cas(ledger):
    register(ledger, agent_id="a")
    register(ledger, agent_id="b")
    page = ledger.ok("agents_list", limit=1)
    assert page["agents"][0]["agent_id"] == "a" and page["next_cursor"] == "a"
    assert ledger.ok("agents_list", after="a")["agents"][0]["agent_id"] == "b"
    bind(ledger, role_id="a", agent_id="a")
    bind(ledger, role_id="b", agent_id="b")
    assert ledger.ok("roles_list", limit=1)["next_cursor"] == "a"
    ledger.error("not_found", "role_get", role_id="missing")
    ledger.error("not_found", "task_create", task_id="t", title="X", role_id="missing")
    payload = dict(
        role_id="a", expected_revision=1, label="Race", agent_id="b", selection=SELECTION
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ledger.request("role_put", **payload), range(2)))
    assert sum(r["ok"] for r in results) == 1
    assert next(r for r in results if not r["ok"])["error"]["code"] == "revision_conflict"


@pytest.mark.parametrize(
    "extra",
    [
        {"mode": "command", "models": []},
        {"models": [{"id": "x"}, {"id": "x"}]},
        {"capabilities": ["x", "x"]},
        {"models": [{"id": "x", "parameters": {"effort": []}}]},
        {"models": [{"id": "x", "parameters": {"effort": ["high", "high"]}}]},
        {"models": [{"id": "--inject-option"}]},
    ],
)
def test_invalid_profile_contract(ledger, extra):
    ledger.error(
        "invalid_request",
        "agent_put",
        agent_id="a",
        label="A",
        runtime="custom",
        expected_revision=0,
        **extra,
    )


def test_command_selection_requires_model_and_legacy_actors_are_reserved(ledger):
    register(ledger, mode="command")
    ledger.error(
        "model_required", "role_put", role_id="r", label="R", agent_id="coder", expected_revision=0
    )
    ledger.ok("state_put", key="x", expected_version=0, value=None)
    ledger.error(
        "actor_in_use",
        "agent_put",
        agent_id="worker",
        actor="admin",
        expected_revision=0,
        label="Old",
        runtime="custom",
    )
    ledger.error(
        "actor_in_use",
        "agent_put",
        agent_id="new",
        actor="new",
        expected_revision=0,
        label="Same",
        runtime="custom",
    )
    ledger.ok("task_create", task_id="t", title="X")
    ledger.error("role_required", "task_claim", task_id="t", selection=SELECTION)
    lease = ledger.ok("task_claim", task_id="t")
    ledger.error(
        "role_required",
        "task_complete",
        task_id="t",
        fence=lease["task"]["fence"],
        lease_token=lease["lease_token"],
        reported_model="model-a",
        outputs=[{"key": "out", "expected_version": 0, "value": 1}],
    )


def test_v1_migration_preserves_fences_null_values_and_receipts(tmp_path):
    path = tmp_path / "old.sqlite3"
    old_payload = {
        "project": "default",
        "actor": "old",
        "request_id": "create",
        "task_id": "t",
        "title": "Old task",
        "description": "",
        "inputs": [],
    }
    old_response = {"ok": True, "data": {"task_id": "t", "legacy": True}, "replayed": False}
    with sqlite3.connect(path) as db:
        db.executescript(files("relayfence").joinpath("schema.sql").read_text())
        db.execute(
            "INSERT INTO tasks(project,task_id,title,description,inputs,status,fence,created_at,updated_at) VALUES ('default','t','Old task','','[]','ready',12,1,1)"
        )
        db.execute(
            "INSERT INTO receipts VALUES (?,?,?,?,?,?)",
            (
                "default",
                "old",
                "create",
                digest(encode({"operation": "task_create", "request": old_payload})),
                encode(old_response),
                1,
            ),
        )
        payload = {
            "project": "default",
            "actor": "old",
            "request_id": "complete",
            "task_id": "t",
            "fence": 12,
            "lease_token": "x" * 32,
            "outputs": [{"key": "null", "expected_version": 0, "value": None}],
        }
        db.execute(
            "INSERT INTO receipts VALUES (?,?,?,?,?,?)",
            (
                "default",
                "old",
                "complete",
                digest(encode({"operation": "task_complete", "request": payload})),
                encode(old_response),
                1,
            ),
        )
    service = Service(Store(path))
    assert service.call("task_create", old_payload)[0] == {**old_response, "replayed": True}
    assert service.call("task_complete", payload)[0]["replayed"]
    task = service.call("task_get", {"task_id": "t"})[0]["data"]
    assert task["fence"] == 12 and task["role_id"] is None
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: Store(path), range(3)))
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
