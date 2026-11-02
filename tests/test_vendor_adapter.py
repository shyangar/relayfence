import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from relayfence.codex_adapter import AgentIncomplete
from relayfence.vendor_adapter import execute_dsh, result_output, validate_selection


def test_dsh_adapter_preserves_explicit_route_and_runtime_settings(tmp_path, monkeypatch):
    envelope = {"task": {"task_id": "catalogue", "outputs": []}}

    def run(command, **kwargs):
        assert command[:3] == ["harness.exe", "--expose-internals", "-e"]
        assert "runDesktopCli" in command[3]
        assert command[4].endswith("cli.js")
        assert command[5] == "headless"
        assert command[-1] == "-"
        assert command[command.index("--patch") + 1] == "existing-provider.yml"
        assert kwargs["env"]["ELECTRON_RUN_AS_NODE"] == "1"
        assert "RELAYFENCE_TOKEN" not in kwargs["env"]
        assert "catalogue" in kwargs["input"].decode()
        overlay = (tmp_path / "vendor-selection.yml").read_text()
        assert 'provider: "isoft"' in overlay
        assert 'model: "deepseek-flash"' in overlay
        assert "security" not in overlay
        (tmp_path / "vendor-result.json").write_text(
            json.dumps(
                {
                    "outcome": "completed",
                    "summary": "Done",
                    "details": "Role catalogue",
                    "files": ["office_roles.py"],
                    "checks": ["pytest passed"],
                }
            )
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("RELAYFENCE_TOKEN", "private-test-value")
    monkeypatch.setattr("relayfence.vendor_adapter.subprocess.run", run)
    result = execute_dsh(
        "harness.exe",
        "cli.js",
        "deepseek-flash",
        {"provider": "isoft"},
        envelope,
        tmp_path,
        ["existing-provider.yml"],
    )
    assert result["outputs"][0]["key"] == "tasks/catalogue/result"
    assert "reported_model" not in result


@pytest.mark.parametrize(
    "vendor,model,parameters",
    [
        ("dsh", "", {"provider": "isoft"}),
        ("dsh", "deepseek-flash", {}),
        ("dsh", "deepseek-flash", {"provider": "isoft", "reasoning_effort": "medium"}),
        ("zcode", "glm-5.3", {"provider": "account:zai-start-plan"}),
        (
            "zcode",
            "glm-5.3",
            {"provider": "account:zai-start-plan", "reasoning_effort": "unrecognized"},
        ),
    ],
)
def test_unsupported_selection_cannot_be_silently_substituted(vendor, model, parameters):
    with pytest.raises(ValueError):
        validate_selection(vendor, model, parameters)


def test_result_preserves_output_version_and_reports_incomplete():
    task = {"task": {"task_id": "work", "outputs": [{"key": "tasks/work/result", "version": 3}]}}
    value = {
        "outcome": "completed",
        "summary": "Done",
        "details": "Result",
        "files": [],
        "checks": [],
    }
    assert result_output(task, value)["outputs"][0]["expected_version"] == 3
    with pytest.raises(AgentIncomplete):
        result_output(task, {**value, "outcome": "blocked"})


def test_structured_vendor_checks_preserve_claims_without_inventing_success():
    task = {"task": {"task_id": "work", "outputs": []}}
    check = {"command": "pytest 中文", "result": "1 failed, 2 passed"}
    value = {
        "outcome": "completed",
        "summary": "Review finished",
        "details": "",
        "files": [],
        "checks": [check, "Manual review only"],
    }
    published = result_output(task, value)["outputs"][0]["value"]
    assert json.loads(published["checks"][0]) == check
    assert published["checks"][1] == "Manual review only"
    assert value["checks"][0] == check
    for invalid in (
        {"command": "pytest"},
        {"command": "pytest", "result": True},
        {"command": "pytest", "result": "", "passed": True},
    ):
        with pytest.raises(ValueError):
            result_output(task, {**value, "checks": [invalid]})


@pytest.mark.parametrize("mode", ["failed", "missing", "oversized", "batch"])
def test_failure_never_creates_success_receipt(tmp_path, monkeypatch, mode):
    def run(*args, **kwargs):
        if mode == "oversized":
            (tmp_path / "vendor-result.json").write_text("x" * 131073)
        return SimpleNamespace(returncode=1 if mode == "failed" else 0)

    monkeypatch.setattr("relayfence.vendor_adapter.subprocess.run", run)
    with pytest.raises((ValueError, RuntimeError)):
        execute_dsh(
            "harness.cmd" if mode == "batch" else "harness.exe",
            "cli.js",
            "deepseek-flash",
            {"provider": "isoft"},
            {},
            tmp_path,
        )


def test_dsh_bootstrap_calls_exported_esm_entry_and_preserves_arguments(tmp_path, monkeypatch):
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to verify the installed ESM bootstrap contract")
    script = tmp_path / "runtime/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.mjs"
    script.parent.mkdir(parents=True)
    script.write_text(
        "export async function runDesktopCli(runtime,support){"
        "console.log(JSON.stringify({runtime,support,args:process.argv.slice(2)}))}"
    )
    captured = []
    original_run = subprocess.run

    def run(command, **kwargs):
        captured.extend(command)
        (tmp_path / "vendor-result.json").write_text(
            json.dumps(
                {
                    "outcome": "completed",
                    "summary": "fixture",
                    "details": "",
                    "files": [],
                    "checks": [],
                }
            )
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("relayfence.vendor_adapter.subprocess.run", run)
    execute_dsh(
        node,
        str(script),
        "exact-model",
        {"provider": "fixture"},
        {"task": {"task_id": "fixture", "outputs": []}},
        tmp_path,
    )
    result = original_run(captured, capture_output=True, timeout=10, check=True)
    value = json.loads(result.stdout)
    assert value["args"][0] == "headless"
    assert value["args"][-1] == "-"
    assert Path(value["runtime"]) == tmp_path / "runtime"
    assert Path(value["support"]) == tmp_path / "runtime"
