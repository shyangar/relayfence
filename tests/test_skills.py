"""Skill discovery, injection and selection rules.

The behaviours asserted here were verified against the installed Codex binary with
`codex debug prompt-input`: only flat child directories under a skill root are
loaded, a byte-order mark hides a SKILL.md, and a `skills.config` entry removes a
skill from default discovery.
"""

import json
from pathlib import Path

import pytest

from relayfence import skill_catalogue
from relayfence.errors import CoordinationError
from relayfence.models import OfficeMember, Selection, SkillList
from relayfence.skill_catalogue import (
    discover_skills,
    inject_skills,
    remove_injected,
    skills_config_value,
    unselected_skills,
    validate_skill_names,
)
from relayfence.store import Store
from relayfence.worker import WorkerConfig, adapter_command


def write_skill(directory, name, description, body="# Body\n", bom=False):
    directory.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name}\ndescription: {description}\n---\n{body}"
    path = directory / "SKILL.md"
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return path


@pytest.fixture
def homes(tmp_path, monkeypatch):
    codex = tmp_path / "codex-home"
    zcode = tmp_path / "zcode-home"
    dsh = tmp_path / "dsh-home"
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("ZCODE_HOME", str(zcode))
    monkeypatch.setenv("DSH_HOME", str(dsh))
    config = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return {
        "codex": codex,
        "opencode": config / "opencode",
        "zcode": zcode,
        "dsh": dsh,
        "workspace": workspace,
    }


def test_discovery_groups_skills_by_software(homes):
    write_skill(homes["codex"] / "skills" / "alpha", "alpha", "Alpha skill")
    write_skill(homes["zcode"] / "skills" / "beta", "beta", "Beta skill")
    write_skill(homes["dsh"] / "skills" / "gamma", "gamma", "Gamma skill")
    write_skill(homes["workspace"] / ".codex" / "skills" / "delta", "delta", "Delta skill")

    catalogue = discover_skills(homes["workspace"])

    by_software = {entry["software"]: entry for entry in catalogue["skills"]}
    assert [s["name"] for s in by_software["codex"]["skills"]] == ["alpha", "delta"]
    assert [s["name"] for s in by_software["zcode"]["skills"]] == ["beta"]
    assert [s["name"] for s in by_software["dsh"]["skills"]] == ["gamma"]
    assert by_software["codex"]["label"] == "Codex"
    assert catalogue["workspace"] == str(homes["workspace"])
    assert {s["name"]: s["origin"] for s in by_software["codex"]["skills"]} == {
        "alpha": "user",
        "delta": "project",
    }


def test_opencode_uses_its_own_user_and_project_roots(homes):
    write_skill(homes["opencode"] / "skills" / "alpha", "alpha", "Alpha skill")
    write_skill(homes["workspace"] / ".opencode" / "skills" / "beta", "beta", "Beta skill")

    catalogue = discover_skills(homes["workspace"])

    opencode = next(entry for entry in catalogue["skills"] if entry["software"] == "opencode")
    assert [skill["name"] for skill in opencode["skills"]] == ["alpha", "beta"]
    assert {skill["name"]: skill["origin"] for skill in opencode["skills"]} == {
        "alpha": "user",
        "beta": "project",
    }
    assert opencode["label"] == "OpenCode"

    injected = inject_skills(homes["workspace"], "opencode", ["alpha"])
    assert [entry["name"] for entry in injected] == ["alpha"]
    target = homes["workspace"] / ".opencode" / "skills" / skill_catalogue.INJECTED_DIR / "alpha"
    assert (target / "SKILL.md").is_file()
    assert not (homes["workspace"] / ".codex" / "skills" / skill_catalogue.INJECTED_DIR).exists()


def test_nested_container_directories_are_not_reported(homes):
    """`codex debug prompt-input` skips `.system/<name>`, so the catalogue must too."""
    write_skill(homes["codex"] / "skills" / ".system" / "hidden", "hidden", "Nested")

    catalogue = discover_skills(homes["workspace"])

    codex = next(e for e in catalogue["skills"] if e["software"] == "codex")
    assert codex["skills"] == []


def test_byte_order_mark_and_missing_frontmatter_are_rejected(homes):
    broken = homes["codex"] / "skills" / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")

    catalogue = discover_skills(homes["workspace"])

    codex = next(e for e in catalogue["skills"] if e["software"] == "codex")
    assert codex["skills"] == []


def test_discovery_reports_roots_and_availability(homes):
    write_skill(homes["codex"] / "skills" / "alpha", "alpha", "Alpha skill")

    catalogue = discover_skills(homes["workspace"])

    codex = next(e for e in catalogue["skills"] if e["software"] == "codex")
    roots = {root["path"]: root["exists"] for root in codex["roots"]}
    assert roots[str(homes["codex"] / "skills")] is True
    assert roots[str(homes["workspace"] / ".codex" / "skills")] is False


def test_symlinked_skills_are_skipped(homes, tmp_path):
    outside = tmp_path / "outside"
    write_skill(outside, "linked", "Outside the root")
    skills = homes["codex"] / "skills"
    skills.mkdir(parents=True)
    try:
        (skills / "linked").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    catalogue = discover_skills(homes["workspace"])

    codex = next(e for e in catalogue["skills"] if e["software"] == "codex")
    assert codex["skills"] == []


def test_injection_copies_selection_and_strips_byte_order_mark(homes):
    source = homes["codex"] / "skills" / "alpha"
    write_skill(source, "alpha", "Alpha skill")
    (source / "scripts").mkdir()
    (source / "scripts" / "run.py").write_text("print('x')\n", encoding="utf-8")
    write_skill(homes["codex"] / "skills" / "other", "other", "Other skill")

    injected = inject_skills(homes["workspace"], "codex", ["alpha"])

    assert [entry["name"] for entry in injected] == ["alpha"]
    target = homes["workspace"] / ".codex" / "skills" / skill_catalogue.INJECTED_DIR / "alpha"
    assert (target / "scripts" / "run.py").is_file()
    assert "alpha" in (target / "SKILL.md").read_text(encoding="utf-8")
    # The unselected skill stays where it was and is not copied.
    assert not (
        homes["workspace"] / ".codex" / "skills" / skill_catalogue.INJECTED_DIR / "other"
    ).exists()


def test_byte_order_mark_blocks_discovery_and_selection(homes):
    """The loader ignores such a file, so it is never offered or injectable."""
    write_skill(homes["codex"] / "skills" / "alpha", "alpha", "Alpha skill", bom=True)

    assert skill_catalogue._scan(homes["codex"] / "skills") == []
    with pytest.raises(CoordinationError) as error:
        inject_skills(homes["workspace"], "codex", ["alpha"])
    assert error.value.code == "skill_unknown"


def test_injection_replaces_previous_selection(homes):
    for name in ("alpha", "beta"):
        write_skill(homes["codex"] / "skills" / name, name, name.title())
    inject_skills(homes["workspace"], "codex", ["alpha", "beta"])
    staging = homes["workspace"] / ".codex" / "skills" / skill_catalogue.INJECTED_DIR

    inject_skills(homes["workspace"], "codex", ["beta"])

    assert sorted(p.name for p in staging.iterdir()) == [
        ".relayfence-injected",
        "beta",
    ]
    assert inject_skills(homes["workspace"], "codex", []) == []
    assert not staging.exists()


def test_injection_refuses_a_foreign_directory(homes):
    staging = homes["workspace"] / ".codex" / "skills" / skill_catalogue.INJECTED_DIR
    staging.mkdir(parents=True)
    (staging / "user-file").write_text("keep me", encoding="utf-8")
    write_skill(homes["codex"] / "skills" / "alpha", "alpha", "Alpha skill")

    with pytest.raises(ValueError, match="拒绝覆盖"):
        inject_skills(homes["workspace"], "codex", ["alpha"])
    assert (staging / "user-file").read_text(encoding="utf-8") == "keep me"


def test_selection_validation_rejects_traversal_and_unknown_names(homes):
    write_skill(homes["codex"] / "skills" / "alpha", "alpha", "Alpha skill")

    assert validate_skill_names("codex", ["alpha"]) == ["alpha"]
    for bad in (["../escape"], ["a/b"], ["a\\b"], ["."], ["missing"]):
        with pytest.raises(CoordinationError):
            validate_skill_names("codex", bad)
    with pytest.raises(CoordinationError, match="重复"):
        validate_skill_names("codex", ["alpha", "alpha"])


def test_skills_config_value_disables_only_unselected(homes):
    write_skill(homes["codex"] / "skills" / "alpha", "alpha", "Alpha skill")
    write_skill(homes["codex"] / "skills" / "beta", "beta", "Beta skill")

    remaining = unselected_skills("codex", ["alpha"], homes["workspace"])

    assert remaining == ["beta"]
    assert skills_config_value(remaining) == 'skills.config=[{name="beta",enabled=false}]'
    assert skills_config_value([]) == ""


def test_selection_keeps_skills_and_stays_backwards_compatible():
    assert "skills" not in Selection().model_dump(exclude={"skills"})
    assert Selection(skills=["alpha"]).skills == ["alpha"]
    with pytest.raises(ValueError):
        Selection(skills=["alpha", "alpha"])


def test_office_member_accepts_unique_skills_and_rejects_duplicates():
    base = {
        "id": "m1",
        "name": "Ada",
        "position": "lead",
        "provider": "openai",
        "model": "gpt-test",
    }
    assert OfficeMember(**base, skills=["alpha", "beta"]).skills == ["alpha", "beta"]
    with pytest.raises(ValueError):
        OfficeMember(**base, skills=["alpha", "alpha"])


def test_store_skill_list_uses_the_workspace(homes):
    write_skill(homes["codex"] / "skills" / "alpha", "alpha", "Alpha skill")
    store = Store(homes["workspace"] / "ledger.db")
    store.set_skill_workspace(homes["workspace"], project="p")

    data = store.execute("skill_list", SkillList(project="p"))["data"]

    codex = next(entry for entry in data["skills"] if entry["software"] == "codex")
    assert [skill["name"] for skill in codex["skills"]] == ["alpha"]
    assert data["workspace"] == str(homes["workspace"])


def test_role_put_rejects_uninstalled_skill_and_keeps_selection(homes):
    write_skill(homes["codex"] / "skills" / "alpha", "alpha", "Alpha skill")
    store = Store(homes["workspace"] / "ledger.db")
    store.set_skill_workspace(homes["workspace"], project="p")
    store.execute(
        "agent_put",
        _agent_put("codex-instance"),
    )
    role = {
        "project": "p",
        "actor": "admin",
        "role_id": "role",
        "expected_revision": 0,
        "label": "Role",
        "agent_id": "codex-instance",
        "selection": {"model": "model-a", "parameters": {}, "skills": ["alpha"]},
    }
    result = store.execute("role_put", _role_put(role))
    assert result["data"]["selection"]["skills"] == ["alpha"]

    role.update(
        request_id="second",
        expected_revision=1,
        selection={"model": "model-a", "parameters": {}, "skills": ["ghost"]},
    )
    with pytest.raises(CoordinationError) as error:
        store.execute("role_put", _role_put(role))
    assert error.value.code == "skill_unknown"


def test_empty_skill_selection_is_omitted_from_responses(homes):
    store = Store(homes["workspace"] / "ledger.db")
    store.execute("agent_put", _agent_put("codex-instance"))
    role = {
        "project": "p",
        "actor": "admin",
        "role_id": "role",
        "expected_revision": 0,
        "label": "Role",
        "agent_id": "codex-instance",
        "selection": {"model": "model-a", "parameters": {}},
    }
    result = store.execute("role_put", _role_put(role))
    assert result["data"]["selection"] == {"model": "model-a", "parameters": {}}


def test_worker_passes_skills_config_to_the_codex_adapter(homes, monkeypatch):
    write_skill(homes["codex"] / "skills" / "alpha", "alpha", "Alpha skill")
    write_skill(homes["codex"] / "skills" / "beta", "beta", "Beta skill")
    config = WorkerConfig(agent_id="codex-instance", adapter="codex")

    command = adapter_command(
        config,
        {"model": "model-a", "parameters": {}, "skills": ["alpha"]},
        homes["workspace"],
        "codex",
    )

    value = command[command.index("--skills-config") + 1]
    assert value == 'skills.config=[{name="beta",enabled=false}]'
    assert "--ignore-user-config" not in command


def test_worker_omits_skills_config_without_a_selection(homes):
    config = WorkerConfig(agent_id="codex-instance", adapter="codex")

    command = adapter_command(config, {"model": "model-a", "parameters": {}}, homes["workspace"])

    assert "--skills-config" not in command


def test_codex_adapter_forwards_skills_config_to_the_command(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from relayfence import codex_adapter

    seen = {}

    def run(command, **kwargs):
        seen["command"] = command
        (tmp_path / "codex-result.json").write_text(
            json.dumps(
                {"outcome": "completed", "summary": "s", "details": "", "files": [], "checks": []}
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(codex_adapter.subprocess, "run", run)
    codex_adapter.execute(
        "C:/codex.exe",
        "read-only",
        {"model": "model-a", "parameters": {}},
        {
            "task": {"task_id": "t", "outputs": [{"key": "tasks/t/result", "version": 1}]},
            "inputs": [],
        },
        tmp_path,
        'skills.config=[{name="beta",enabled=false}]',
    )

    command = seen["command"]
    index = command.index("-c", command.index('approval_policy="never"'))
    assert command[index + 1] == 'skills.config=[{name="beta",enabled=false}]'


def test_discovery_survives_plain_files_and_oversized_manifests(homes):
    skills = homes["codex"] / "skills"
    oversized = skills / "oversized"
    oversized.mkdir(parents=True)
    (oversized / "SKILL.md").write_bytes(
        b"---\nname: big\ndescription: x\n---\n" + b"a" * (skill_catalogue.FRONTMATTER_BYTES * 8)
    )
    write_skill(skills / "good", "good", "Good skill")
    (skills / "not-a-dir").write_text("plain file", encoding="utf-8")

    catalogue = discover_skills(homes["workspace"])

    codex = next(e for e in catalogue["skills"] if e["software"] == "codex")
    assert [s["name"] for s in codex["skills"]] == ["good"]


def test_frontmatter_values_are_single_line_and_bounded(homes):
    write_skill(
        homes["codex"] / "skills" / "noisy",
        "noisy",
        "line one\n  continuation\n\twith control \x07chars " + "x" * 2000,
    )
    write_skill(
        homes["codex"] / "skills" / "long",
        "long",
        "y" * 4000,
    )

    scanned = {s["name"]: s for s in skill_catalogue._scan(homes["codex"] / "skills")}

    # Only the first frontmatter line is the description; later lines are ignored.
    assert scanned["noisy"]["description"] == "line one"
    assert len(scanned["long"]["description"]) <= skill_catalogue.DESCRIPTION_LIMIT
    assert scanned["long"]["description"].endswith("…")

    # Control characters are normalised away before a value reaches the browser.
    (homes["codex"] / "skills" / "noisy" / "SKILL.md").write_bytes(
        b"---\nname: noisy\ndescription: keep \x07 out\n---\n"
    )
    rescanned = {s["name"]: s for s in skill_catalogue._scan(homes["codex"] / "skills")}
    assert rescanned["noisy"]["description"] == "keep out"


def test_skill_without_description_still_loads(homes):
    directory = homes["codex"] / "skills" / "bare"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("---\nname: bare\n---\nbody\n", encoding="utf-8")

    catalogue = discover_skills(homes["workspace"])

    codex = next(e for e in catalogue["skills"] if e["software"] == "codex")
    assert codex["skills"][0]["name"] == "bare"
    assert codex["skills"][0]["description"]


def test_disabled_entries_are_reported_from_the_runtime_config(homes):
    write_skill(homes["codex"] / "skills" / "off", "off", "Turned off locally")
    write_skill(homes["codex"] / "skills" / "on", "on", "Left on")
    (homes["codex"] / "config.toml").write_text(
        "[[skills.config]]\nname = 'off'\nenabled = false\n", encoding="utf-8"
    )

    catalogue = discover_skills(homes["workspace"])

    codex = next(e for e in catalogue["skills"] if e["software"] == "codex")
    flags = {s["name"]: s["disabled_by_config"] for s in codex["skills"]}
    assert flags == {"off": True, "on": False}


def test_skill_home_falls_back_when_the_variable_is_unset(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)

    roots = [str(root) for root, _origin in skill_catalogue.skill_roots("codex")]

    assert roots == [str(Path.home() / ".codex" / "skills")]


def test_injection_rejects_a_skill_with_too_many_files(homes):
    source = homes["codex"] / "skills" / "huge"
    write_skill(source, "huge", "Too many files")
    for index in range(skill_catalogue.FILE_LIMIT + 1):
        (source / f"f{index}").write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="文件过多"):
        inject_skills(homes["workspace"], "codex", ["huge"])


def _agent_put(agent_id):
    from relayfence.models import AgentPut

    return AgentPut(
        project="p",
        actor="admin",
        request_id="a-" + agent_id,
        agent_id=agent_id,
        expected_revision=0,
        label="Codex",
        runtime="codex",
        mode="command",
        models=[{"id": "model-a"}],
    )


def _role_put(payload):
    from relayfence.models import RolePut

    return RolePut(**{"request_id": "first", **payload})


def test_office_dispatch_carries_member_skills_into_the_role(tmp_path, monkeypatch):
    """The office backend must not drop the member's selection when it builds a role."""
    from relayfence.store import Store as _Store

    write_skill(tmp_path / "home" / "skills" / "alpha", "alpha", "Alpha skill")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))

    class Ledger:
        def __init__(self):
            self.store = _Store(tmp_path / "ledger.db")
            self.clock = type("C", (), {"now": 0.0})()
            self.store.clock = lambda: self.clock.now

        def ok(self, operation, **extra):
            result = self.store.execute(
                operation,
                _operation(operation, {"project": "p", "actor": "admin", **extra}),
            )
            return result["data"]

        def request(self, operation, **extra):
            try:
                return {"ok": True, "data": self.ok(operation, **extra)}
            except CoordinationError as error:
                return {"ok": False, "error": error.as_dict()["error"]}

    ledger = Ledger()
    agent = ledger.ok(
        "agent_put",
        request_id="agent",
        agent_id="codex-instance",
        expected_revision=0,
        label="Codex",
        runtime="codex",
        mode="command",
        models=[{"id": "model-a"}],
    )
    assert agent["agent_id"] == "codex-instance"
    member = {
        "id": "m1",
        "name": "组长",
        "position": "backend",
        "relationship": "leader",
        "agent_id": "codex-instance",
        "provider": "openai",
        "model": "model-a",
        "skills": ["alpha"],
    }
    ledger.ok(
        "office_put",
        request_id="save",
        expected_revision=0,
        groups=[{"id": "g", "name": "工程组", "software": "codex", "members": [member]}],
    )
    saved = ledger.ok("office_get")
    assert saved["groups"][0]["members"][0]["skills"] == ["alpha"]

    ledger.ok(
        "office_dispatch",
        request_id="dispatch",
        expected_revision=saved["revision"],
        briefing_id="b",
        title="任务",
        description="完成工作",
        member_ids=["m1"],
    )
    roles = ledger.ok("roles_list")
    assert roles["roles"], "dispatch must project the member as a role"
    assert roles["roles"][0]["selection"]["skills"] == ["alpha"]


def _operation(operation, payload):
    from relayfence.models import OPERATIONS, Mutation

    model = OPERATIONS[operation][0]
    if not issubclass(model, Mutation):
        payload = {k: v for k, v in payload.items() if k != "actor"}
    return model(**payload)


def test_zcode_and_dsh_use_the_vendor_neutral_agents_root(homes):
    write_skill(homes["zcode"] / "skills" / "alpha", "alpha", "Alpha skill")
    write_skill(homes["dsh"] / "skills" / "beta", "beta", "Beta skill")

    assert [entry["name"] for entry in inject_skills(homes["workspace"], "zcode", ["alpha"])] == [
        "alpha"
    ]
    target = homes["workspace"] / ".agents" / "skills" / skill_catalogue.INJECTED_DIR / "alpha"
    assert (target / "SKILL.md").is_file()
    assert not (homes["workspace"] / ".codex").exists()

    assert [entry["name"] for entry in inject_skills(homes["workspace"], "dsh", ["beta"])] == [
        "beta"
    ]
    assert (
        homes["workspace"] / ".agents" / "skills" / skill_catalogue.INJECTED_DIR / "beta"
    ).is_dir()


def test_injection_rejects_two_skills_with_the_same_directory_name(homes):
    write_skill(homes["codex"] / "skills" / "shared", "first", "First skill")
    write_skill(homes["workspace"] / ".codex" / "skills" / "shared", "second", "Second skill")

    with pytest.raises(CoordinationError) as caught:
        inject_skills(homes["workspace"], "codex", ["first", "second"])

    assert caught.value.code == "skill_conflict"
    assert not (homes["workspace"] / ".codex" / "skills" / "relayfence-selected").exists()


def test_injection_targets_the_directory_name_not_frontmatter(homes):
    write_skill(homes["codex"] / "skills" / "folder-name", "display-name", "Display")

    injected = inject_skills(homes["workspace"], "codex", ["display-name"])

    assert [entry["name"] for entry in injected] == ["display-name"]
    target = homes["workspace"] / ".codex" / "skills" / skill_catalogue.INJECTED_DIR / "folder-name"
    assert (target / "SKILL.md").is_file()


def test_remove_injected_only_removes_a_marked_staging_directory(homes):
    write_skill(homes["codex"] / "skills" / "alpha", "alpha", "Alpha skill")
    inject_skills(homes["workspace"], "codex", ["alpha"])
    staging = homes["workspace"] / ".codex" / "skills" / skill_catalogue.INJECTED_DIR

    remove_injected(homes["workspace"], "codex")
    assert not staging.exists()

    staging.mkdir(parents=True)
    (staging / "user-file").write_text("keep", encoding="utf-8")
    remove_injected(homes["workspace"], "codex")
    assert (staging / "user-file").read_text(encoding="utf-8") == "keep"


def test_empty_codex_selection_disables_every_discovered_skill(homes):
    write_skill(homes["codex"] / "skills" / "alpha", "alpha", "Alpha skill")
    config = WorkerConfig(agent_id="codex-instance", adapter="codex")

    command = adapter_command(config, {"model": "model-a", "parameters": {}}, homes["workspace"])

    assert "--skills-config" in command
    value = command[command.index("--skills-config") + 1]
    assert value == 'skills.config=[{name="alpha",enabled=false}]'
