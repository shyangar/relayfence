"""OpenCode adapter tests use local fixtures; no vendor credential or network call."""

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from relayfence import opencode_adapter
from relayfence.codex_adapter import AgentIncomplete
from relayfence.models import OfficeGroup
from relayfence.opencode_adapter import (
    execute,
    parse_model_metadata,
    parse_models,
    result_from_events,
    validate_selection,
    worker_command,
)

COMPLETED = {
    "outcome": "completed",
    "summary": "Done",
    "details": "Reviewed the workspace",
    "files": ["src/app.py"],
    "checks": ["pytest passed"],
}


def envelope(task_id="work", outputs=()):
    return {"task": {"task_id": task_id, "outputs": list(outputs)}}


def event(text, part_id="part-1"):
    return json.dumps({"type": "text", "part": {"id": part_id, "type": "text", "text": text}})


def test_selection_is_explicit_and_cannot_be_substituted():
    validate_selection("isoft/deepseek-flash", {"provider": "isoft"})
    validate_selection("openrouter/anthropic/claude-sonnet-4.5", {"provider": "openrouter"})
    for model, parameters in (
        ("", {"provider": "isoft"}),
        ("deepseek-flash", {"provider": "isoft"}),
        ("isoft/deepseek-flash", {}),
        ("isoft/deepseek-flash", {"provider": "other"}),
        ("isoft/deepseek-flash", {"provider": "isoft", "reasoning_effort": "high"}),
        ("isoft/deepseek-flash", {"provider": ["isoft"]}),
        ("isoft/deepseek-flash#high", {"provider": "isoft"}),
    ):
        with pytest.raises(ValueError):
            validate_selection(model, parameters)


def test_model_catalogue_ignores_diagnostics_and_deduplicates():
    text = (
        "\x1b[32misoft/deepseek-flash\x1b[0m\n"
        "\n"
        "  openrouter/anthropic/claude-sonnet-4.5  \n"
        "not-a-reference\n"
        "provider/model#variant\n"
        "# comment\n"
        "isoft/deepseek-flash\n"
    )
    assert parse_models(text) == [
        "isoft/deepseek-flash",
        "openrouter/anthropic/claude-sonnet-4.5",
    ]


def test_model_metadata_marks_only_fully_zero_cost_models_free():
    payload = {
        "data": [
            {
                "providerID": "opencode",
                "modelID": "beam-free",
                "name": "Beam Free",
                "enabled": True,
                "cost": [
                    {
                        "input": 0,
                        "output": 0,
                        "cache": {"read": 0, "write": 0},
                    }
                ],
            },
            {
                "providerID": "opencode",
                "modelID": "paid",
                "name": "Paid",
                "cost": {"input": 0.1, "output": 0.5, "cache": {"read": 0.01}},
            },
            {
                "providerID": "opencode",
                "modelID": "tiered",
                "cost": [
                    {"input": 0.1, "output": 0.5},
                    {"tier": {"type": "context", "size": 100000}, "input": 0.5, "output": 2},
                ],
            },
            {
                "providerID": "opencode",
                "modelID": "unknown-price",
                "name": "Unknown",
            },
            {
                "providerID": "opencode",
                "modelID": "disabled",
                "enabled": False,
                "cost": {"input": 0, "output": 0},
            },
        ]
    }
    metadata = parse_model_metadata(json.dumps(payload))

    assert metadata["opencode/beam-free"]["free"] is True
    assert metadata["opencode/beam-free"]["pricing"] == {"input": 0, "output": 0}
    assert metadata["opencode/beam-free"]["label"] == "Beam Free"
    assert metadata["opencode/paid"]["free"] is False
    assert metadata["opencode/paid"]["pricing"]["input"] == 0.1
    assert metadata["opencode/tiered"]["free"] is False
    assert metadata["opencode/tiered"]["pricing"] == {"input": 0.1, "output": 0.5}
    assert "free" not in metadata["opencode/unknown-price"]
    assert "opencode/disabled" not in metadata
    assert parse_model_metadata("not-json") == {}
    assert parse_model_metadata("") == {}


def test_catalogue_bound_and_office_software_literal():
    with pytest.raises(ValueError, match="limit"):
        parse_models("\n".join(f"p/m{i}" for i in range(opencode_adapter.CATALOGUE_LIMIT + 1)))
    group = {
        "id": "g",
        "name": "Group",
        "software": "opencode",
        "members": [
            {
                "id": "m1",
                "name": "Member",
                "position": "backend",
                "relationship": "leader",
                "provider": "isoft",
                "model": "isoft/deepseek-flash",
                "parameters": {"provider": "isoft"},
            }
        ],
    }
    assert OfficeGroup.model_validate(group).software == "opencode"


def test_final_text_must_be_one_unambiguous_result():
    assert result_from_events(event(json.dumps(COMPLETED))) == COMPLETED
    assert result_from_events(event("```json\n" + json.dumps(COMPLETED) + "\n```")) == COMPLETED
    assert result_from_events(event("Result follows: " + json.dumps(COMPLETED))) == COMPLETED
    ambiguous = json.dumps(COMPLETED) + "\n" + json.dumps({**COMPLETED, "summary": "Second"})
    with pytest.raises(RuntimeError, match="unambiguous"):
        result_from_events(event(ambiguous))
    with pytest.raises(RuntimeError, match="final text"):
        result_from_events(event(""))
    # Tool parts are not assistant answers.
    tool = json.dumps(
        {"type": "tool", "part": {"id": "t", "type": "tool", "state": {"output": "text"}}}
    )
    with pytest.raises(RuntimeError, match="final text"):
        result_from_events(tool)


def test_incremental_text_parts_are_joined_but_snapshots_are_not_duplicated():
    first = json.dumps(COMPLETED)[:20]
    second = json.dumps(COMPLETED)[20:]
    stream = "\n".join([event(first, "x"), event(second, "x")])
    assert result_from_events(stream) == COMPLETED
    stream = "\n".join([event(json.dumps(COMPLETED), "x"), event(json.dumps(COMPLETED), "x")])
    assert result_from_events(stream) == COMPLETED


def test_execute_uses_the_exact_model_agent_and_bounded_prompt(tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["stdout"].write((event(json.dumps(COMPLETED)) + "\n").encode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("RELAYFENCE_TOKEN", "private-test-value")
    monkeypatch.setattr("relayfence.opencode_adapter.subprocess.run", run)
    result = execute(
        "C:/OpenCode/opencode.exe",
        "workspace-write",
        "isoft/deepseek-flash",
        {"provider": "isoft"},
        envelope(outputs=[]),
        tmp_path,
    )

    command, kwargs = calls[0]
    assert command[:2] == ["C:/OpenCode/opencode.exe", "run"]
    assert "--standalone" in command
    assert command[command.index("--format") + 1] == "json"
    assert command[command.index("--model") + 1] == "isoft/deepseek-flash"
    assert command[command.index("--agent") + 1] == "build"
    prompt = Path(command[command.index("--file") + 1]).read_text(encoding="utf-8")
    assert '"task_id":"work"' in prompt
    assert "ONE JSON object" in prompt
    assert "RELAYFENCE_TOKEN" not in kwargs["env"]
    assert result["outputs"][0]["key"] == "tasks/work/result"
    assert result["outputs"][0]["expected_version"] == 0
    assert "reported_model" not in result


def test_read_only_maps_to_the_plan_agent_and_preserves_output_version(tmp_path, monkeypatch):
    def run(command, **kwargs):
        assert command[command.index("--agent") + 1] == "plan"
        kwargs["stdout"].write((event(json.dumps(COMPLETED)) + "\n").encode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("relayfence.opencode_adapter.subprocess.run", run)
    result = execute(
        "opencode",
        "read-only",
        "isoft/deepseek-flash",
        {"provider": "isoft"},
        envelope(outputs=[{"key": "tasks/work/result", "version": 4}]),
        tmp_path,
    )
    assert result["outputs"][0]["expected_version"] == 4


def test_blocked_outcome_and_runtime_failures_never_publish_success(tmp_path, monkeypatch):
    def blocked(command, **kwargs):
        kwargs["stdout"].write(
            (event(json.dumps({**COMPLETED, "outcome": "blocked"})) + "\n").encode()
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("relayfence.opencode_adapter.subprocess.run", blocked)
    with pytest.raises(AgentIncomplete) as error:
        execute(
            "opencode",
            "workspace-write",
            "isoft/deepseek-flash",
            {"provider": "isoft"},
            envelope(),
            tmp_path,
        )
    assert error.value.code == "agent_blocked"

    monkeypatch.setattr(
        "relayfence.opencode_adapter.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )
    second = tmp_path / "second"
    second.mkdir()
    with pytest.raises(RuntimeError, match="failed"):
        execute(
            "opencode",
            "workspace-write",
            "isoft/deepseek-flash",
            {"provider": "isoft"},
            envelope(),
            second,
        )


def test_batch_shims_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="batch"):
        execute(
            "opencode.cmd",
            "workspace-write",
            "isoft/deepseek-flash",
            {"provider": "isoft"},
            envelope(),
            tmp_path,
        )


def test_worker_command_is_a_separate_interpreter_with_placeholders():
    command = worker_command("C:/OpenCode/opencode.exe")
    assert command[:3] == [
        opencode_adapter.sys.executable,
        "-m",
        "relayfence.opencode_adapter",
    ]
    assert "{model}" in command and "{parameters_json}" in command
    assert command[command.index("--permission-mode") + 1] == "danger-full-access"


def test_executable_override_and_batch_shim_rejection(monkeypatch):
    monkeypatch.setenv("RELAYFENCE_OPENCODE_EXECUTABLE", "/explicit/opencode")
    assert opencode_adapter.find_executable() == "/explicit/opencode"
    monkeypatch.setenv("RELAYFENCE_OPENCODE_EXECUTABLE", "opencode.cmd")
    assert opencode_adapter.find_executable() == "opencode.cmd"


def test_find_executable_prefers_the_native_binary_beside_an_npm_shim(tmp_path, monkeypatch):
    shim = tmp_path / "opencode.cmd"
    native = tmp_path / "opencode.exe"
    shim.write_text("@echo off\n", encoding="utf-8")
    native.write_bytes(b"MZ")
    monkeypatch.delenv("RELAYFENCE_OPENCODE_EXECUTABLE", raising=False)
    monkeypatch.setattr(opencode_adapter.shutil, "which", lambda name: str(shim))
    assert opencode_adapter.find_executable() == str(native)


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop bundle")
def test_find_executable_discovers_the_desktop_bundle(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    resources = tmp_path / "local" / "Programs" / "@opencodedesktop" / "resources"
    resources.mkdir(parents=True)
    native = resources / "opencode-cli.exe"
    native.write_bytes(b"MZ")
    monkeypatch.delenv("RELAYFENCE_OPENCODE_EXECUTABLE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(opencode_adapter.shutil, "which", lambda name: None)
    assert opencode_adapter.find_executable() == str(native)


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop bundle")
def test_find_executable_discovers_a_versioned_desktop_cli(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    version = tmp_path / "roaming" / "ai.opencode.desktop" / "cli" / "2.0.32-x64"
    version.mkdir(parents=True)
    native = version / "opencode-cli.exe"
    native.write_bytes(b"MZ")
    monkeypatch.delenv("RELAYFENCE_OPENCODE_EXECUTABLE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(opencode_adapter.shutil, "which", lambda name: None)
    assert opencode_adapter.find_executable() == str(native)


def worker_arguments(monkeypatch, tmp_path, payload):
    monkeypatch.setenv("RELAYFENCE_RUN_DIRECTORY", str(tmp_path))
    monkeypatch.setattr(
        opencode_adapter.sys,
        "argv",
        [
            "relayfence.opencode_adapter",
            "--executable",
            "opencode",
            "--permission-mode",
            "read-only",
            "--model",
            "isoft/deepseek-flash",
            "--parameters",
            json.dumps({"provider": "isoft"}),
        ],
    )
    monkeypatch.setattr(
        opencode_adapter.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())),
    )


def test_worker_entrypoint_emits_the_expected_worker_result(tmp_path, monkeypatch, capsys):
    called = {}

    def run(executable, permission_mode, model, parameters, value, directory):
        called.update(
            executable=executable,
            permission_mode=permission_mode,
            model=model,
            parameters=parameters,
            envelope=value,
            directory=directory,
        )
        return {
            "outputs": [{"key": "tasks/work/result", "expected_version": 0, "value": COMPLETED}]
        }

    monkeypatch.setattr(opencode_adapter, "execute", run)
    worker_arguments(monkeypatch, tmp_path, envelope())
    assert opencode_adapter.main() == 0
    assert called["model"] == "isoft/deepseek-flash"
    assert called["parameters"] == {"provider": "isoft"}
    assert called["permission_mode"] == "read-only"
    assert called["directory"] == tmp_path
    assert json.loads(capsys.readouterr().out)["outputs"][0]["key"] == "tasks/work/result"


def test_worker_entrypoint_reports_incomplete_work_as_a_protocol_error(
    tmp_path, monkeypatch, capsys
):
    def run(*args):
        raise AgentIncomplete("blocked", "Missing test dependencies")

    monkeypatch.setattr(opencode_adapter, "execute", run)
    worker_arguments(monkeypatch, tmp_path, envelope())
    assert opencode_adapter.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outputs"] == []
    assert payload["error"] == {
        "code": "agent_blocked",
        "message": "Missing test dependencies",
    }
