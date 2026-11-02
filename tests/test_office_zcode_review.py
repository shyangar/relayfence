import json

from relayfence.models import OfficeGroup


def group(agent_id, software="codex", prefix="c"):
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


def register(ledger, name, software="codex"):
    ledger.ok(
        "agent_put",
        agent_id=name,
        expected_revision=0,
        label=name,
        runtime=software,
        mode="command",
        models=[
            {
                "id": "model-a",
                "parameters": {} if software == "codex" else {"provider": ["isoft", "other"]},
            }
        ],
    )


def test_chinese_and_html_labels_roundtrip_as_inert_json(ledger):
    register(ledger, "coder")
    member_name = '<img src=x onerror=alert(1)>组长"引号" & 中文 <b>加粗</b>'
    g = group("coder")
    g["name"] = '工程组<script>alert("xss")</script>'
    g["members"][0]["name"] = member_name
    ledger.ok("office_put", groups=[g], expected_revision=0)
    expected = [OfficeGroup.model_validate(g).model_dump(mode="json")]
    stored = ledger.ok("office_get")
    assert stored["groups"] == expected
    with ledger.store.connection() as db:
        raw = db.execute(
            "SELECT definition FROM office_configs WHERE project='default'"
        ).fetchone()["definition"]
    assert "<script>" in raw and "<img src=x onerror=alert(1)>" in raw
    assert json.loads(raw) == expected
    title = '<iframe src="javascript:1">中文简报</iframe>'
    result = ledger.ok(
        "office_dispatch",
        expected_revision=1,
        briefing_id="briefing-1",
        title=title,
        description="验收中文与HTML标签按原样存储",
        member_ids=["c1"],
    )
    role = ledger.ok("role_get", role_id=result["tasks"][0]["role_id"])
    assert role["label"] == "后端工程师 · " + member_name
    task = ledger.ok("task_get", task_id=result["tasks"][0]["task_id"])
    assert task["title"] == title + " · " + member_name


def test_office_rejects_mismatched_provider_identity(ledger):
    register(ledger, "coder")
    register(ledger, "zrun", "zcode")
    ledger.ok(
        "office_put", groups=[group("coder"), group("zrun", "zcode", "z")], expected_revision=0
    )
    baseline = ledger.ok("office_get")
    forged_codex = group("coder")
    forged_codex["members"][0]["provider"] = "other-vendor"
    ledger.error("provider_mismatch", "office_put", groups=[forged_codex], expected_revision=1)
    mismatched_selection = group("zrun", "zcode", "z")
    mismatched_selection["members"][0]["parameters"] = {"provider": "other"}
    ledger.error(
        "provider_mismatch", "office_put", groups=[mismatched_selection], expected_revision=1
    )
    mismatched_fallback = group("zrun", "zcode", "z")
    mismatched_fallback["members"][0]["fallback_models"] = [
        {"provider": "other", "model": "model-a", "parameters": {"provider": "isoft"}}
    ]
    ledger.error(
        "provider_mismatch", "office_put", groups=[mismatched_fallback], expected_revision=1
    )
    assert ledger.ok("office_get") == baseline


def test_office_rejects_model_binding_outside_runner_catalogue(ledger):
    register(ledger, "coder")
    ledger.ok("office_put", groups=[group("coder")], expected_revision=0)
    baseline = ledger.ok("office_get")
    unknown_model = group("coder")
    unknown_model["members"][0]["model"] = "model-z"
    ledger.error("unsupported_model", "office_put", groups=[unknown_model], expected_revision=1)
    unknown_fallback = group("coder")
    unknown_fallback["members"][0]["fallback_models"] = [{"provider": "openai", "model": "model-z"}]
    ledger.error("unsupported_model", "office_put", groups=[unknown_fallback], expected_revision=1)
    unknown_parameter = group("coder")
    unknown_parameter["members"][0]["parameters"] = {"effort": "high"}
    ledger.error(
        "unsupported_parameters", "office_put", groups=[unknown_parameter], expected_revision=1
    )
    assert ledger.ok("office_get") == baseline
