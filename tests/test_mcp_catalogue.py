import json
from types import SimpleNamespace

import pytest
from test_office import as_agent, group, register

from relayfence.errors import CoordinationError
from relayfence.mcp_catalogue import (
    codex_overrides,
    discover_mcp,
    inventory,
    opencode_environment,
    read_config,
    validate_mcp_selection,
)
from relayfence.skill_catalogue import read_skill


@pytest.fixture
def local_homes(tmp_path, monkeypatch):
    for name in ("CODEX_HOME", "ZCODE_HOME", "DSH_HOME", "XDG_CONFIG_HOME"):
        path = tmp_path / name
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    return tmp_path


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def codex_config(root):
    return write(
        root / "CODEX_HOME/config.toml",
        '[mcp_servers.docs]\ncommand="docs-server"\nargs=["SECRET_ARG"]\n[mcp_servers.docs.env]\nAPI_KEY="SECRET_VALUE"\n[mcp_servers.other]\nurl="https://host.invalid/?token=PRIVATE"\nenabled=false\n',
    )


def test_inventory_redacts_secrets_and_plugins_are_declarations(local_homes):
    codex_config(local_homes)
    write(
        local_homes / "CODEX_HOME/plugins/cache/vendor/plugin/1/.mcp.json",
        '{"mcpServers":{"tool":{"command":"private-command","env":{"TOKEN":"NEVER_PUBLIC"}}}}',
    )
    data = inventory("codex")
    wire = json.dumps(data)
    for value in (
        "SECRET",
        "PRIVATE",
        "host.invalid",
        "API_KEY",
        "NEVER_PUBLIC",
        "private-command",
    ):
        assert value not in wire
    assert data["servers"][0]["connection"] == "not_probed"
    plugin = next(s for s in data["servers"] if s["origin"] == "plugin")
    assert plugin["selectable"] is False and plugin["enabled"] is None
    assert len(discover_mcp()) == 4


def test_codex_scope_override_and_fail_closed_on_config_errors(local_homes, tmp_path):
    config = codex_config(local_homes)
    original = config.read_bytes()
    assert 'mcp_servers."docs".enabled=true' in codex_overrides(["docs"])
    assert 'mcp_servers."other".enabled=false' in codex_overrides(["docs"])
    assert all(s.endswith("=false") for s in codex_overrides([]))
    assert codex_overrides(None) == []
    assert config.read_bytes() == original
    with pytest.raises(CoordinationError, match="所选 MCP"):
        validate_mcp_selection("codex", ["missing"])
    write(config, "[broken")
    with pytest.raises(CoordinationError, match="读取不完整"):
        codex_overrides([])
    for software in ("zcode", "dsh"):
        validate_mcp_selection(software, None)
        with pytest.raises(CoordinationError, match="沿用"):
            validate_mcp_selection(software, [])


@pytest.mark.parametrize("v2", [False, True])
def test_opencode_preserves_full_private_definition_and_other_env(local_homes, v2):
    entries = {
        "docs": {"type": "local", "command": ["docs"], "environment": {"KEY": "PRIVATE"}},
        "other": {"type": "remote", "url": "https://private.invalid"},
    }
    config = {"mcp": {"servers": entries} if v2 else entries}
    write(
        local_homes / "XDG_CONFIG_HOME/opencode/opencode.jsonc",
        json.dumps(config) + " // comment\n",
    )
    env = {"OTHER": "keep", "OPENCODE_CONFIG_CONTENT": '{"model":"original"}'}
    result = opencode_environment(["docs"], env)
    merged = json.loads(result["OPENCODE_CONFIG_CONTENT"])
    servers = merged["mcp"]["servers"] if v2 else merged["mcp"]
    assert servers["docs"]["command"] == ["docs"]
    assert servers["docs"]["environment"]["KEY"] == "PRIVATE"
    assert servers["other"]["disabled" if v2 else "enabled"] is v2
    assert merged["model"] == "original" and result["OTHER"] == "keep"
    assert "mcp" not in env["OPENCODE_CONFIG_CONTENT"]


def test_dsh_yaml_metadata_and_bounded_reader(local_homes):
    path = write(
        local_homes / "DSH_HOME/profiles/default/cordis.patch.yml",
        "- id: mcp-client\n  config:\n    serverName: docs\n    command: private\n    env:\n      SECRET: hidden\n",
    )
    data = inventory("dsh")
    assert data["servers"][0]["id"] == "docs"
    assert not data["selection_supported"]
    assert "hidden" not in json.dumps(data)
    path.write_bytes(b"x" * (1_048_576 + 1))
    assert read_config(path)[1] == "config_too_large"


def test_dispatch_capabilities_are_isolated_and_claimed_exactly(ledger, local_homes):
    codex_config(local_homes)
    principal = register(ledger)
    ledger.ok("office_put", groups=[group("coder")], expected_revision=0)
    created = ledger.ok(
        "office_dispatch",
        expected_revision=1,
        briefing_id="caps",
        title="Caps",
        description="read only",
        member_ids=["c1"],
        leader_id="c1",
        member_overrides={"c1": {"skills": [], "mcp_servers": ["docs"]}},
    )
    saved = ledger.ok("office_get")["groups"][0]["members"][0]
    assert saved.get("mcp_servers") is None
    assert created["meeting"]["groups"][0]["members"][0]["mcp_servers"] == ["docs"]
    task = created["tasks"][0]
    claim = as_agent(
        ledger,
        principal,
        "coder",
        "task_claim",
        task_id=task["task_id"],
        selection={"model": "model-a", "parameters": {}, "mcp_servers": ["docs"]},
    )
    assert claim["task"]["execution"]["requested"]["mcp_servers"] == ["docs"]
    ledger.error(
        "invalid_capability_member",
        "office_dispatch",
        expected_revision=1,
        briefing_id="bad",
        title="Bad",
        description="none",
        member_ids=["c1"],
        member_overrides={"foreign": {"mcp_servers": []}},
    )


def test_codex_adapter_delivers_mcp_overrides(local_homes, monkeypatch, tmp_path):
    from relayfence.codex_adapter import execute

    codex_config(local_homes)
    selection = {"model": "test-model", "parameters": {}, "mcp_servers": ["docs"]}
    envelope = {"task": {"task_id": "work", "outputs": []}, "inputs": [], "selection": selection}

    def native(command, **kwargs):
        assert 'mcp_servers."docs".enabled=true' in command
        assert 'mcp_servers."other".enabled=false' in command
        assert "SECRET_VALUE" not in json.dumps(command)
        (tmp_path / "codex-result.json").write_text(
            json.dumps(
                {
                    "outcome": "completed",
                    "summary": "Verified",
                    "details": "fixture",
                    "files": [],
                    "checks": [],
                }
            )
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("relayfence.codex_adapter.subprocess.run", native)
    assert execute("codex", "read-only", selection, envelope, tmp_path)["outputs"]


def test_folded_skill_description_is_read_in_full(tmp_path):
    write(
        tmp_path / "SKILL.md",
        "---\nname: folded\ndescription: >\n  First line\n  second line\n---\nBody",
    )
    assert read_skill(tmp_path)["description"] == "First line second line"


def test_public_mcp_endpoint_and_invalid_selection(ledger, local_homes):
    codex_config(local_homes)
    data = ledger.ok("mcp_list")
    assert data["capabilities_version"] == 1
    assert "SECRET_VALUE" not in json.dumps(data)
    for names in (["docs", "docs"], ["../not-configured"], ["invalid name"]):
        with pytest.raises(CoordinationError):
            validate_mcp_selection("codex", names)


def test_config_read_errors_are_generic(local_homes, monkeypatch):
    path = write(local_homes / "CODEX_HOME/config.toml", "invalid SECRET=not-a-value")
    assert read_config(path)[1] == "config_unreadable"
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", "{broken SECRET_VALUE")
    data = inventory("opencode")
    assert data["warnings"] == ["inline_config_unreadable"]
    assert "SECRET_VALUE" not in json.dumps(data)
    with pytest.raises(CoordinationError):
        validate_mcp_selection("opencode", [])


def test_dispatch_receipts_distinguish_absent_and_explicit_inherit(ledger, local_homes):
    codex_config(local_homes)
    register(ledger)
    team = group("coder")
    team["members"][0]["mcp_servers"] = ["docs"]
    ledger.ok("office_put", groups=[team], expected_revision=0)
    request = dict(
        expected_revision=1,
        briefing_id="receipt-caps",
        title="Caps",
        description="fixture",
        member_ids=["c1"],
        request_id="same-capability-request",
        member_overrides={"c1": {"skills": []}},
    )
    original = ledger.ok("office_dispatch", **request)
    assert ledger.ok("office_dispatch", **request) == original
    request["member_overrides"] = {"c1": {"skills": [], "mcp_servers": None}}
    ledger.error("idempotency_conflict", "office_dispatch", **request)
