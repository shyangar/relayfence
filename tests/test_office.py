import copy
import uuid

import pytest

from relayfence.identity import Principal
from relayfence.store import Store, digest


def group(agent_id=None, software="codex", prefix="c"):
    return {
        "id": prefix,
        "name": "工程组",
        "software": software,
        "members": [
            {
                "id": prefix + "1",
                "name": "组长",
                "position": "backend",
                "avatar": 4,
                "gender": "female",
                "relationship": "leader",
                "agent_id": agent_id,
                "provider": "openai" if software == "codex" else "isoft",
                "model": "model-a",
                "parameters": {} if software == "codex" else {"provider": "isoft"},
            }
        ],
    }


def register(ledger, name="coder", software="codex", models=None):
    profile = ledger.ok(
        "agent_put",
        agent_id=name,
        expected_revision=0,
        label=name,
        runtime=software,
        mode="command",
        models=models
        or [
            {"id": "model-a", "parameters": {} if software == "codex" else {"provider": ["isoft"]}}
        ],
    )
    token = ledger.ok("agent_token_rotate", agent_id=name, expected_revision=profile["revision"])[
        "token"
    ]
    return Principal(token_hash=digest(token))


def as_agent(ledger, principal, actor, operation, **extra):
    result, status = ledger.service.call(
        operation, {"actor": actor, "request_id": uuid.uuid4().hex, **extra}, principal
    )
    assert status == 200, result
    return result["data"]


def observe(ledger, route_id, status="available", **extra):
    return ledger.ok(
        "quota_observe",
        route_id=route_id,
        status=status,
        source="provider_api",
        observed_at=ledger.clock.now,
        expires_at=ledger.clock.now + 300,
        **extra,
    )


def test_office_get_exposes_the_exact_role_for_long_member_ids(ledger):
    data = group()
    member_id = "a" * 128
    data["members"][0]["id"] = member_id

    ledger.ok("office_put", groups=[data], expected_revision=0)

    roles = ledger.ok("office_get")["member_roles"]
    assert roles[member_id] == "office/" + digest(member_id)[:32]


def test_office_revision_idempotency_restart_and_validation(ledger):
    assert ledger.ok("office_get")["revision"] == 0
    payload = {"groups": [group()], "expected_revision": 0, "request_id": "save"}
    first = ledger.request("office_put", **payload)
    assert first["ok"]
    assert ledger.request("office_put", **payload)["replayed"]
    restarted = Store(ledger.store.path)
    with restarted.connection() as db:
        assert restarted._office_config(db, "default")["groups"][0]["members"][0]["avatar"] == 4
    ledger.error("revision_conflict", "office_put", groups=[], expected_revision=0)
    bad = group()
    bad["members"][0]["relationship"] = "native_subagent"
    ledger.error("invalid_request", "office_put", groups=[bad], expected_revision=1)
    bad = group()
    bad["members"].append(copy.deepcopy(bad["members"][0]))
    ledger.error("invalid_request", "office_put", groups=[bad], expected_revision=1)
    assert len(ledger.ok("office_get")["positions"]) == 8


def test_binding_validation_and_atomic_briefing(ledger):
    register(ledger)
    g = group("coder")
    ledger.ok("office_put", groups=[g], expected_revision=0)
    bad = copy.deepcopy(g)
    bad["software"] = "dsh"
    ledger.error("runtime_mismatch", "office_put", groups=[bad], expected_revision=1)
    bad = copy.deepcopy(g)
    bad["members"][0]["model"] = "unavailable"
    ledger.error("unsupported_model", "office_put", groups=[bad], expected_revision=1)
    draft = group(prefix="draft")
    ledger.ok("office_put", groups=[g, draft], expected_revision=1)
    ledger.error(
        "unbound_member",
        "office_dispatch",
        expected_revision=2,
        briefing_id="meeting",
        title="Build",
        description="Acceptance",
        member_ids=["c1", "draft1"],
    )
    assert ledger.ok("tasks_list")["tasks"] == []
    assert ledger.ok("roles_list")["roles"] == []
    result = ledger.ok(
        "office_dispatch",
        expected_revision=2,
        briefing_id="meeting",
        title="Build",
        description="Acceptance",
        member_ids=["c1"],
    )
    assert result["tasks"][0]["task_id"] == "meeting/1"
    assert result["tasks"][0]["role_id"].startswith("office-task/")
    ledger.error(
        "not_found",
        "office_dispatch",
        expected_revision=2,
        briefing_id="other",
        title="Build",
        description="Acceptance",
        member_ids=["gone"],
    )


def test_quota_validation_freshness_and_preference(ledger):
    register(ledger)
    register(ledger, "dsh", "dsh")
    ledger.ok("office_put", groups=[group("coder"), group("dsh", "dsh", "d")], expected_revision=0)
    assert ledger.ok("office_route_preview", current_route_id="c1")["route_id"] is None
    observe(ledger, "c1", "exhausted", remaining=0, unit="tokens")
    observe(ledger, "d1", remaining=500, unit="tokens")
    assert ledger.ok("office_route_preview", current_route_id="c1")["route_id"] == "d1"
    ledger.error(
        "stale_observation",
        "quota_observe",
        route_id="d1",
        status="unknown",
        source="operator",
        observed_at=ledger.clock.now,
        expires_at=ledger.clock.now + 100,
    )
    ledger.error(
        "not_found",
        "quota_observe",
        route_id="missing",
        status="unknown",
        source="operator",
        observed_at=ledger.clock.now,
        expires_at=ledger.clock.now + 100,
    )
    ledger.error(
        "invalid_observation",
        "quota_observe",
        route_id="d1",
        status="unknown",
        source="operator",
        observed_at=ledger.clock.now + 10,
        expires_at=ledger.clock.now + 100,
    )
    ledger.clock.advance(301)
    assert all(o["stale"] for o in ledger.ok("quota_list")["observations"])
    assert ledger.ok("office_route_preview", current_route_id="c1")["route_id"] is None


def test_handoff_preserves_checkpoint_fences_and_role(ledger):
    principal = register(ledger)
    register(ledger, "dsh", "dsh")
    ledger.ok("office_put", groups=[group("coder"), group("dsh", "dsh", "d")], expected_revision=0)
    task = ledger.ok(
        "office_dispatch",
        expected_revision=1,
        briefing_id="meeting",
        title="Build",
        description="Acceptance",
        member_ids=["c1"],
    )["tasks"][0]
    claim = as_agent(
        ledger,
        principal,
        "coder",
        "task_claim",
        task_id=task["task_id"],
        selection={"model": "model-a", "parameters": {}},
    )
    lease = {
        "task_id": task["task_id"],
        "fence": claim["task"]["fence"],
        "lease_token": claim["lease_token"],
    }
    request = {
        "task_id": task["task_id"],
        "expected_revision": claim["task"]["revision"],
        "current_route_id": "c1",
        "checkpoint_key": "progress",
        "stopped_confirmed": True,
    }
    ledger.error("unsafe_handoff", "office_handoff", **request)
    failed = as_agent(
        ledger,
        principal,
        "coder",
        "task_fail",
        **lease,
        error_code="quota_exhausted",
        message="Provider confirmed quota exhaustion; subprocess stopped",
    )
    request["expected_revision"] = failed["revision"]
    ledger.ok(
        "state_put",
        key="progress",
        expected_version=0,
        value={"completed": ["design"], "next": "implementation"},
    )
    observe(ledger, "c1", "exhausted", remaining=0, unit="tokens")
    observe(ledger, "d1", remaining=1000, unit="tokens")
    moved = ledger.ok("office_handoff", **request)
    assert moved["status"] == "ready" and moved["owner"] is None
    assert moved["inputs"] == ["progress"]
    assert moved["revision"] == failed["revision"] + 1
    assert moved["execution"] == claim["task"]["execution"]
    assert moved["fence"] == claim["task"]["fence"]
    role = ledger.ok("role_get", role_id=moved["role_id"])
    assert role["agent_id"] == "dsh"
    assert role["instructions"] == claim["task"]["execution"]["role"]["instructions"]
    for field in ("label", "deliverables", "required_capabilities"):
        assert role[field] == claim["task"]["execution"]["role"][field]
    ledger.error("revision_conflict", "office_handoff", **request)
    result, status = ledger.service.call(
        "task_complete",
        {
            "actor": "coder",
            "request_id": uuid.uuid4().hex,
            **lease,
            "outputs": [{"key": "late", "expected_version": 0, "value": True}],
        },
        principal,
    )
    assert not result["ok"] and status == 409
    ledger.error("not_found", "item_get", key="late")
    assert ledger.ok("task_get", task_id=task["task_id"])["status"] == "ready"


def test_agent_cannot_forge_office_or_quota(ledger):
    principal = register(ledger)
    for operation, fields in [
        ("office_put", {"expected_revision": 0, "groups": []}),
        (
            "quota_observe",
            {
                "route_id": "c1",
                "status": "available",
                "source": "provider_api",
                "observed_at": ledger.clock.now,
                "expires_at": ledger.clock.now + 60,
            },
        ),
    ]:
        response, status = ledger.service.call(
            operation, {"actor": "coder", "request_id": uuid.uuid4().hex, **fields}, principal
        )
        assert status == 403 and response["error"]["code"] == "admin_required"


def failed_office_task(ledger, error_code="quota_exhausted"):
    principal = register(ledger)
    register(ledger, "dsh", "dsh")
    ledger.ok("office_put", groups=[group("coder"), group("dsh", "dsh", "d")], expected_revision=0)
    task = ledger.ok(
        "office_dispatch",
        expected_revision=1,
        briefing_id="meeting",
        title="Build",
        description="Acceptance",
        member_ids=["c1"],
    )["tasks"][0]
    claim = as_agent(
        ledger,
        principal,
        "coder",
        "task_claim",
        task_id=task["task_id"],
        selection={"model": "model-a", "parameters": {}},
    )
    failed = as_agent(
        ledger,
        principal,
        "coder",
        "task_fail",
        task_id=task["task_id"],
        fence=claim["task"]["fence"],
        lease_token=claim["lease_token"],
        error_code=error_code,
        message="Stopped",
    )
    ledger.ok("state_put", key="progress", expected_version=0, value={"next": "implementation"})
    observe(ledger, "c1", "exhausted", remaining=0, unit="tokens")
    observe(ledger, "d1", remaining=100, unit="tokens")
    request = dict(
        task_id=task["task_id"],
        expected_revision=failed["revision"],
        current_route_id="c1",
        checkpoint_key="progress",
        stopped_confirmed=True,
    )
    return principal, failed, request


def test_project_scoped_reads_and_admin_only_operations(ledger):
    principal = register(ledger)
    for operation, fields in [
        ("office_get", {}),
        ("quota_list", {}),
        ("office_route_preview", {"current_route_id": "c1"}),
    ]:
        response, status = ledger.service.call(
            operation, {"project": "private", **fields}, principal
        )
        assert status == 403 and response["error"]["code"] == "forbidden"
    for operation, fields in [
        (
            "office_dispatch",
            dict(
                expected_revision=0,
                briefing_id="b",
                title="Build",
                description="Acceptance",
                member_ids=["c1"],
            ),
        ),
        (
            "office_handoff",
            dict(
                task_id="t",
                expected_revision=0,
                current_route_id="c1",
                checkpoint_key="progress",
                stopped_confirmed=True,
            ),
        ),
    ]:
        response, status = ledger.service.call(
            operation, dict(actor="coder", request_id=uuid.uuid4().hex, **fields), principal
        )
        assert status == 403 and response["error"]["code"] == "admin_required"
    response, status = ledger.service.call("team_groups_list", {"project": "default"}, principal)
    assert status == 403 and response["error"]["code"] == "admin_required"
    assert ledger.ok("office_get", project="private")["revision"] == 0
    assert ledger.ok("quota_list", project="private")["observations"] == []


def test_provider_binding_and_invalid_fallback_rollback(ledger):
    register(ledger, "dsh", "dsh")
    g = group("dsh", "dsh", "d")
    ledger.ok("office_put", groups=[g], expected_revision=0)
    invalid = copy.deepcopy(g)
    invalid["members"][0]["provider"] = "other"
    ledger.error("provider_mismatch", "office_put", groups=[invalid], expected_revision=1)
    invalid = copy.deepcopy(g)
    invalid["members"][0]["fallback_models"] = [
        {"provider": "isoft", "model": "unavailable", "parameters": {"provider": "isoft"}}
    ]
    ledger.error("unsupported_model", "office_put", groups=[invalid], expected_revision=1)
    stored = ledger.ok("office_get")
    assert stored["revision"] == 1
    assert stored["groups"][0]["members"][0]["provider"] == "isoft"
    assert stored["groups"][0]["members"][0]["fallback_models"] == []


def test_same_software_fallback_precedes_other_software(ledger):
    register(ledger, models=[{"id": "model-a"}, {"id": "model-b"}])
    register(ledger, "dsh", "dsh")
    g = group("coder")
    g["members"][0]["fallback_models"] = [{"provider": "openai", "model": "model-b"}]
    ledger.ok("office_put", groups=[group("dsh", "dsh", "d"), g], expected_revision=0)
    observe(ledger, "c1", "exhausted", remaining=0, unit="tokens")
    observe(ledger, "d1")
    observe(ledger, "c1/fallback/1")
    preview = ledger.ok("office_route_preview", current_route_id="c1")
    assert preview["route_id"] == "c1/fallback/1"
    assert not preview["requires_probe"]
    assert preview["considered"] == ["c1", "c1/fallback/1", "d1"]


@pytest.mark.parametrize("error_code", ["rate_limited", "runtime_error", "execution_failed"])
def test_handoff_rejects_non_quota_failures_without_mutation(ledger, error_code):
    _, failed, request = failed_office_task(ledger, error_code)
    ledger.error("unsafe_handoff", "office_handoff", **request)
    assert ledger.ok("task_get", task_id=request["task_id"]) == failed
    assert len(ledger.ok("roles_list")["roles"]) == 1


@pytest.mark.parametrize(
    "source,status",
    [("operator", "available"), ("provider_api", "unknown"), ("runtime_error", "rate_limited")],
)
def test_handoff_requires_verified_alternate(ledger, source, status):
    _, failed, request = failed_office_task(ledger)
    ledger.clock.advance(1)
    ledger.ok(
        "quota_observe",
        route_id="d1",
        source=source,
        status=status,
        observed_at=ledger.clock.now,
        expires_at=ledger.clock.now + 300,
    )
    ledger.error("no_available_route", "office_handoff", **request)
    assert ledger.ok("task_get", task_id=request["task_id"]) == failed


def test_handoff_rejects_expired_availability_and_wrong_owner(ledger):
    _, failed, request = failed_office_task(ledger)
    ledger.error("route_mismatch", "office_handoff", **{**request, "current_route_id": "d1"})
    ledger.clock.advance(301)
    ledger.error("no_available_route", "office_handoff", **request)
    assert ledger.ok("task_get", task_id=request["task_id"]) == failed


def test_handoff_requires_explicit_stopped_confirmation(ledger):
    _, failed, request = failed_office_task(ledger)
    ledger.error("invalid_request", "office_handoff", **{**request, "stopped_confirmed": False})
    assert ledger.ok("task_get", task_id=request["task_id"]) == failed


def test_quota_lifetime_limit_does_not_replace_previous_observation(ledger):
    register(ledger)
    ledger.ok("office_put", groups=[group("coder")], expected_revision=0)
    original = observe(ledger, "c1")
    ledger.clock.advance(1)
    ledger.error(
        "invalid_observation",
        "quota_observe",
        route_id="c1",
        status="available",
        source="provider_api",
        observed_at=ledger.clock.now,
        expires_at=ledger.clock.now + 86401,
    )
    assert ledger.ok("quota_list")["observations"] == [{**original, "stale": False}]


def test_fallback_route_id_collision_is_rejected(ledger):
    g = group()
    g["members"][0]["fallback_models"] = [{"provider": "openai", "model": "model-b"}]
    second = group(prefix="c1/fallback/")
    ledger.error("invalid_request", "office_put", groups=[g, second], expected_revision=0)
    assert ledger.ok("office_get")["revision"] == 0


def test_three_handoff_limit_stops_further_automatic_retries(ledger):
    coder, _, request = failed_office_task(ledger)
    profile = ledger.ok("agent_get", agent_id="dsh")
    token = ledger.ok("agent_token_rotate", agent_id="dsh", expected_revision=profile["revision"])[
        "token"
    ]
    principals = {"c1": coder, "d1": Principal(token_hash=digest(token))}
    for _index in range(3):
        moved = ledger.ok("office_handoff", **request)
        next_route = "d1" if request["current_route_id"] == "c1" else "c1"
        actor = "dsh" if next_route == "d1" else "coder"
        parameters = {"provider": "isoft"} if next_route == "d1" else {}
        claim = as_agent(
            ledger,
            principals[next_route],
            actor,
            "task_claim",
            task_id=request["task_id"],
            selection={"model": "model-a", "parameters": parameters},
        )
        assert claim["task"]["fence"] == moved["fence"] + 1
        failed = as_agent(
            ledger,
            principals[next_route],
            actor,
            "task_fail",
            task_id=request["task_id"],
            fence=claim["task"]["fence"],
            lease_token=claim["lease_token"],
            error_code="quota_exhausted",
            message="Stopped",
        )
        ledger.clock.advance(1)
        observe(ledger, next_route, "exhausted", remaining=0, unit="tokens")
        observe(ledger, request["current_route_id"])
        request = {
            **request,
            "current_route_id": next_route,
            "expected_revision": failed["revision"],
        }
    ledger.error("handoff_limit", "office_handoff", **request)
    assert ledger.ok("task_get", task_id=request["task_id"]) == failed
    events = ledger.ok("events_list", subject=request["task_id"])["events"]
    assert sum(event["kind"] == "office.handoff" for event in events) == 3


def test_handoff_stale_checkpoint_rejected_without_mutation(ledger):
    _, failed, request = failed_office_task(ledger)
    ledger.ok("state_put", key="source", expected_version=0, value=1)
    # A checkpoint with an upstream dependency becomes stale when that source changes.
    dependency_lease = ledger.claim("dependent", inputs=["source"])
    ledger.complete(dependency_lease, key="checkpoint", value={"next": "implementation"})
    ledger.ok("state_put", key="source", expected_version=1, value=2)
    ledger.error(
        "stale_checkpoint", "office_handoff", **{**request, "checkpoint_key": "checkpoint"}
    )
    assert ledger.ok("task_get", task_id=request["task_id"]) == failed


def test_dispatch_creates_parallel_tasks_and_replay_has_no_duplicates(ledger):
    principal = register(ledger)
    register(ledger, "dsh", "dsh")
    ledger.ok("office_put", groups=[group("coder"), group("dsh", "dsh", "d")], expected_revision=0)
    request = dict(
        expected_revision=1,
        briefing_id="chain",
        title="Build",
        description="Acceptance",
        member_ids=["c1", "d1"],
        request_id="briefing-once",
    )
    first = ledger.request("office_dispatch", **request)
    assert first["ok"]
    replay = ledger.request("office_dispatch", **request)
    assert replay["replayed"] and replay["data"] == first["data"]
    tasks = first["data"]["tasks"]
    assert [task["depends_on"] for task in tasks] == [[], []]
    assert len(ledger.ok("tasks_list")["tasks"]) == 2
    assert len(ledger.ok("roles_list")["roles"]) == 2
    as_agent(
        ledger,
        principal,
        "coder",
        "task_claim",
        task_id="chain/1",
        selection={"model": "model-a", "parameters": {}},
    )
    logs = ledger.ok("group_logs_list", group_id="c")["logs"]
    by_kind = {entry["kind"]: entry for entry in logs}
    assert by_kind["dispatch.created"]["task_id"] == "chain/1"
    assert by_kind["task.claimed"]["member_id"] == "c1"


def test_handoff_rejects_changed_source_model_configuration(ledger):
    _, failed, request = failed_office_task(ledger)
    profile = ledger.ok("agent_get", agent_id="coder")
    ledger.ok(
        "agent_put",
        agent_id="coder",
        expected_revision=profile["revision"],
        label="coder",
        runtime="codex",
        mode="command",
        models=[{"id": "model-a"}, {"id": "model-b"}],
    )
    g = group("coder")
    g["members"][0]["model"] = "model-b"
    ledger.ok("office_put", groups=[g, group("dsh", "dsh", "d")], expected_revision=1)
    ledger.error("route_mismatch", "office_handoff", **request)
    assert ledger.ok("task_get", task_id=request["task_id"]) == failed


def test_handoff_input_limit_rolls_back_role_update(ledger):
    _, failed, request = failed_office_task(ledger)
    # Populate the existing task's bounded input list to reach the late failure
    # after the alternate role has been prepared inside the transaction.
    with ledger.store.connection() as db:
        db.execute(
            "UPDATE tasks SET inputs=? WHERE project=? AND task_id=?",
            (ledger.store.encode([f"input/{i}" for i in range(32)]), "default", request["task_id"]),
        )
    before = ledger.ok("task_get", task_id=request["task_id"])
    roles = ledger.ok("roles_list")["roles"]
    ledger.error("input_limit", "office_handoff", **request)
    assert ledger.ok("roles_list")["roles"] == roles
    assert ledger.ok("task_get", task_id=request["task_id"]) == before
    assert before["execution"] == failed["execution"]


@pytest.mark.parametrize("fallback", [False, True])
def test_codex_provider_identity_cannot_be_forged(ledger, fallback):
    register(ledger)
    g = group("coder")
    if fallback:
        g["members"][0]["fallback_models"] = [{"provider": "other-vendor", "model": "model-a"}]
    else:
        g["members"][0]["provider"] = "other-vendor"
    ledger.error("provider_mismatch", "office_put", groups=[g], expected_revision=0)
    assert ledger.ok("office_get")["revision"] == 0


@pytest.mark.parametrize("changed_field", ["model", "provider", "parameters", "agent_id"])
def test_route_identity_change_invalidates_only_affected_quota(ledger, changed_field):
    models = [
        {"id": model, "parameters": {"provider": ["isoft", "other"], "effort": ["medium"]}}
        for model in ["model-a", "model-b"]
    ]
    register(ledger, "dsh", "dsh", models=models)
    register(ledger, "second", "dsh", models=models)
    g = group("dsh", "dsh", "d")
    g["members"][0]["fallback_models"] = [
        {"provider": "isoft", "model": "model-b", "parameters": {"provider": "isoft"}}
    ]
    ledger.ok("office_put", groups=[g], expected_revision=0)
    observe(ledger, "d1")
    observe(ledger, "d1/fallback/1")
    member = g["members"][0]
    if changed_field == "model":
        member["model"] = "model-b"
    elif changed_field == "provider":
        member["provider"] = "other"
        member["parameters"] = {"provider": "other"}
    elif changed_field == "parameters":
        member["parameters"]["effort"] = "medium"
    else:
        member["agent_id"] = "second"
    ledger.ok("office_put", groups=[g], expected_revision=1)
    observations = ledger.ok("quota_list")["observations"]
    expected_ids = [] if changed_field == "agent_id" else ["d1/fallback/1"]
    assert [item["route_id"] for item in observations] == expected_ids
    preview = ledger.ok("office_route_preview", current_route_id="d1")
    assert preview["route_id"] == (expected_ids[0] if expected_ids else None)


def test_cosmetic_edit_retains_observations_and_retired_routes_are_removed(ledger):
    register(ledger, models=[{"id": "model-a"}, {"id": "model-b"}])
    g = group("coder")
    g["members"][0]["fallback_models"] = [{"provider": "openai", "model": "model-b"}]
    ledger.ok("office_put", groups=[g], expected_revision=0)
    observe(ledger, "c1")
    observe(ledger, "c1/fallback/1")
    original = ledger.ok("quota_list")["observations"]
    g["name"] = "Renamed team"
    g["members"][0].update(name="Renamed person", avatar=12, position="reviewer")
    ledger.ok("office_put", groups=[g], expected_revision=1)
    assert ledger.ok("quota_list")["observations"] == original
    g["members"][0]["fallback_models"] = []
    ledger.ok("office_put", groups=[g], expected_revision=2)
    assert [item["route_id"] for item in ledger.ok("quota_list")["observations"]] == ["c1"]
    ledger.ok("office_put", groups=[], expected_revision=3)
    assert ledger.ok("quota_list")["observations"] == []


def test_reordered_fallbacks_cannot_reuse_another_models_observation(ledger):
    register(ledger, models=[{"id": model} for model in ["model-a", "model-b", "model-c"]])
    g = group("coder")
    g["members"][0]["fallback_models"] = [
        {"provider": "openai", "model": model} for model in ["model-b", "model-c"]
    ]
    ledger.ok("office_put", groups=[g], expected_revision=0)
    observe(ledger, "c1", "exhausted")
    observe(ledger, "c1/fallback/1")
    observe(ledger, "c1/fallback/2")
    g["members"][0]["fallback_models"].reverse()
    ledger.ok("office_put", groups=[g], expected_revision=1)
    assert [item["route_id"] for item in ledger.ok("quota_list")["observations"]] == ["c1"]
    assert ledger.ok("office_route_preview", current_route_id="c1")["route_id"] is None


def test_rejected_configuration_preserves_quota_evidence(ledger):
    register(ledger)
    g = group("coder")
    ledger.ok("office_put", groups=[g], expected_revision=0)
    observe(ledger, "c1")
    original = ledger.ok("quota_list")["observations"]
    g["members"][0]["model"] = "unsupported"
    ledger.error("unsupported_model", "office_put", groups=[g], expected_revision=1)
    assert ledger.ok("quota_list")["observations"] == original
    assert ledger.ok("office_route_preview", current_route_id="c1")["route_id"] == "c1"
