import copy

import pytest
from pydantic import ValidationError
from test_office import as_agent, group, register

from relayfence.models import OfficeMember
from relayfence.office_roles import POSITIONS


@pytest.mark.parametrize("position", ["backend", "custom"])
def test_explicit_member_prompt_reaches_execution_snapshot(ledger, position):
    principal = register(ledger)
    office = group("coder")
    member = office["members"][0]
    member.update(
        position=position, custom_position="数据工程师", prompt="只检查数据管线。\n逐条报告证据。"
    )
    saved = ledger.ok("office_put", groups=[office], expected_revision=0)
    assert saved["groups"][0]["members"][0]["prompt"] == member["prompt"]
    task = ledger.ok(
        "office_dispatch",
        expected_revision=1,
        briefing_id="custom-work",
        title="Inspect",
        description="Check the data pipeline",
        member_ids=[member["id"]],
    )["tasks"][0]
    claim = as_agent(
        ledger,
        principal,
        "coder",
        "task_claim",
        task_id=task["task_id"],
        selection={"model": "model-a", "parameters": {}},
    )
    role = claim["task"]["execution"]["role"]
    assert role["instructions"] == member["prompt"]
    expected_label = "数据工程师" if position == "custom" else POSITIONS[position]["label"]
    assert role["label"] == expected_label + " · " + member["name"]


def test_old_member_uses_catalogue_default_and_null_restores_default(ledger):
    register(ledger)
    office = group("coder")
    # Existing saved definitions do not contain either newly introduced field.
    ledger.ok("office_put", groups=[office], expected_revision=0)
    first = ledger.ok(
        "office_dispatch",
        expected_revision=1,
        briefing_id="old-member",
        title="Build",
        description="Build the feature",
        member_ids=["c1"],
    )["tasks"][0]
    role = ledger.ok("role_get", role_id=first["role_id"])
    assert role["instructions"] == POSITIONS["backend"]["instructions"]
    member = office["members"][0]
    member["prompt"] = "Keep this exact prompt."
    ledger.ok("office_put", groups=[office], expected_revision=1)
    saved = ledger.ok("office_get")["groups"][0]["members"][0]
    assert saved["prompt"] == "Keep this exact prompt."
    member["prompt"] = None
    ledger.ok("office_put", groups=[office], expected_revision=2)
    ledger.ok(
        "office_dispatch",
        expected_revision=3,
        briefing_id="restored",
        title="Build",
        description="Build the feature",
        member_ids=["c1"],
    )
    assert ledger.ok("role_get", role_id=first["role_id"])["instructions"] == role["instructions"]


@pytest.mark.parametrize(
    "patch",
    [
        {"position": "custom"},
        {"position": "custom", "custom_position": "   "},
        {"position": "custom", "custom_position": "x" * 61},
        {"prompt": "x" * 8001},
        {"prompt": "   "},
        {"prompt": ""},
    ],
)
def test_custom_member_validation(patch):
    with pytest.raises(ValidationError):
        OfficeMember.model_validate({**group()["members"][0], **patch})


def test_custom_position_can_use_default_instructions(ledger):
    register(ledger)
    office = group("coder")
    office["members"][0].update(position="custom", custom_position="数据工程师")
    ledger.ok("office_put", groups=[office], expected_revision=0)
    task = ledger.ok(
        "office_dispatch",
        expected_revision=1,
        briefing_id="generic",
        title="Inspect",
        description="Check the data pipeline",
        member_ids=["c1"],
    )["tasks"][0]
    role = ledger.ok("role_get", role_id=task["role_id"])
    assert role["label"].startswith("数据工程师 · ")
    assert "自定义岗位" in role["instructions"]


def test_native_member_saves_as_unbound_draft_and_cannot_dispatch(ledger):
    register(ledger)
    office = group("coder")
    child = copy.deepcopy(office["members"][0])
    child.update(id="native-child", relationship="native_subagent", session_id="draft-session")
    office["members"].append(child)
    saved = ledger.ok("office_put", groups=[office], expected_revision=0)
    saved_child = saved["groups"][0]["members"][1]
    assert saved_child["agent_id"] is None
    assert saved_child["relationship"] == "native_subagent"
    assert saved_child["session_id"] == "draft-session"
    assert saved["capabilities"]["native_subagent"]["supported"] is False
    ledger.error(
        "native_binding_required",
        "office_dispatch",
        expected_revision=1,
        briefing_id="native",
        title="Build",
        description="Acceptance",
        member_ids=["c1", "native-child"],
    )
    assert ledger.ok("tasks_list")["tasks"] == []
    assert ledger.ok("roles_list")["roles"] == []
    assert ledger.ok("office_route_preview", current_route_id="native-child")["route_id"] is None
