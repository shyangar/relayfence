import json
from types import SimpleNamespace

import pytest

from relayfence.codex_adapter import (
    AgentIncomplete,
    execute,
    main,
    validate_selection,
    windows_sandbox_arguments,
)
from relayfence.worker import WorkerConfig, adapter_command


@pytest.mark.parametrize("failed", [False, True])
def test_adapter_json_survives_legacy_windows_stdout(tmp_path, monkeypatch, failed):
    import io

    raw = io.BytesIO()
    output = io.TextIOWrapper(raw, encoding="cp936")
    monkeypatch.setattr("sys.stdout", output)
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(b"{}")))
    monkeypatch.setattr(
        "sys.argv",
        [
            "adapter",
            "--executable",
            "codex",
            "--sandbox",
            "danger-full-access",
            "--model",
            "a",
            "--parameters",
            "{}",
        ],
    )
    monkeypatch.setenv("RELAYFENCE_RUN_DIRECTORY", str(tmp_path))
    text = "完成 — 中文与 emoji 🧪"

    def execute_result(*args):
        if failed:
            raise AgentIncomplete("failed", text)
        return {"outputs": [{"key": "result", "expected_version": 0, "value": text}]}

    monkeypatch.setattr("relayfence.codex_adapter.execute", execute_result)
    assert main() == 0
    output.flush()
    wire = raw.getvalue()
    assert wire.isascii()
    data = json.loads(wire.decode("utf-8"))
    assert (data["error"]["message"] if failed else data["outputs"][0]["value"]) == text


def test_codex_command_preserves_selection_paths_and_structured_result(tmp_path, monkeypatch):
    selection = {"model": "test-model", "parameters": {"reasoning_effort": "high"}}
    envelope = {
        "task": {"task_id": "work", "outputs": [{"key": "tasks/work/result", "version": 2}]},
        "inputs": [],
        "selection": selection,
    }

    def codex(command, **kwargs):
        assert command[0] == "C:/Agent software/codex.exe"
        assert command[command.index("--model") + 1] == "test-model"
        assert 'model_reasoning_effort="high"' in command
        # `--ignore-user-config` also disables skill discovery, so it must be absent.
        assert "--ignore-user-config" not in command and "--output-schema" in command
        assert json.loads(kwargs["input"].decode().split("\n", 1)[1]) == envelope
        schema = json.loads((tmp_path / "codex-schema.json").read_text())
        assert schema["additionalProperties"] is False
        (tmp_path / "codex-result.json").write_text(
            json.dumps(
                {
                    "outcome": "completed",
                    "summary": "Done",
                    "details": "Actual result",
                    "files": [],
                    "checks": ["test passed"],
                }
            )
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("relayfence.codex_adapter.subprocess.run", codex)
    result = execute("C:/Agent software/codex.exe", "read-only", selection, envelope, tmp_path)
    assert result["outputs"][0]["expected_version"] == 2
    assert "reported_model" not in result
    args = adapter_command(WorkerConfig(agent_id="codex", adapter="codex"), selection)
    assert args[args.index("--model") + 1] == "test-model"


@pytest.mark.parametrize(
    "selection",
    [
        {"model": None, "parameters": {}},
        {"model": "a", "parameters": {"temperature": "1"}},
        {"model": "a", "parameters": {"reasoning_effort": "wrong"}},
    ],
)
def test_unsupported_codex_selection_is_not_silently_ignored(selection):
    with pytest.raises(ValueError):
        validate_selection(selection)


@pytest.mark.parametrize("mode", ["exit", "missing", "large", "invalid", "batch"])
def test_codex_failure_never_publishes_a_success(tmp_path, monkeypatch, mode):
    def codex(*args, **kwargs):
        if mode == "large":
            (tmp_path / "codex-result.json").write_text("x" * 131073)
        if mode == "invalid":
            (tmp_path / "codex-result.json").write_text("{}")
        return SimpleNamespace(returncode=1 if mode == "exit" else 0)

    monkeypatch.setattr("relayfence.codex_adapter.subprocess.run", codex)
    with pytest.raises((ValueError, RuntimeError)):
        execute(
            "codex.cmd" if mode == "batch" else "codex",
            "read-only",
            {"model": "a", "parameters": {}},
            {},
            tmp_path,
        )


def test_codex_adapter_cli_does_not_expose_bad_input(monkeypatch, capsys):
    import io

    monkeypatch.setattr(
        "sys.argv",
        [
            "adapter",
            "--executable",
            "codex",
            "--sandbox",
            "read-only",
            "--model",
            "a",
            "--parameters",
            "{}",
        ],
    )
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(b"private not json")))
    assert main() == 1
    assert "private" not in capsys.readouterr().err.replace("private run logs", "logs")


@pytest.mark.parametrize("outcome", ["blocked", "failed"])
def test_native_adapter_reports_incomplete_work(tmp_path, monkeypatch, capsys, outcome):
    import io

    def result(*args, **kwargs):
        (tmp_path / "codex-result.json").write_text(
            json.dumps(
                {
                    "outcome": outcome,
                    "summary": "Permission denied",
                    "details": "No files changed",
                    "files": [],
                    "checks": [],
                }
            )
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("relayfence.codex_adapter.subprocess.run", result)
    with pytest.raises(AgentIncomplete, match="Permission denied"):
        execute("codex", "read-only", {"model": "a", "parameters": {}}, {}, tmp_path)
    monkeypatch.setattr(
        "relayfence.codex_adapter.execute",
        lambda *args: (_ for _ in ()).throw(AgentIncomplete(outcome, "Permission denied")),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "adapter",
            "--executable",
            "codex",
            "--sandbox",
            "read-only",
            "--model",
            "a",
            "--parameters",
            "{}",
        ],
    )
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(b"{}")))
    monkeypatch.setenv("RELAYFENCE_RUN_DIRECTORY", str(tmp_path))
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "agent_" + outcome


def test_windows_sandbox_preserves_only_existing_setting(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert windows_sandbox_arguments() == []
    (tmp_path / "config.toml").write_text('[windows]\nsandbox="elevated"\n')
    assert windows_sandbox_arguments() == ["-c", 'windows.sandbox="elevated"']
    (tmp_path / "config.toml").write_text('[unrelated]\nvalue="private"\n')
    assert windows_sandbox_arguments() == []
