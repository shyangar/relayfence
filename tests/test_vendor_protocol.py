"""Portable vendor protocol integration tests using a real pipe-based subprocess."""

import io
import json
import queue
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from relayfence import vendor_adapter as adapter


@pytest.fixture
def vendor_process(tmp_path, monkeypatch):
    monkeypatch.setattr(
        adapter, "time", SimpleNamespace(monotonic=time.monotonic, sleep=lambda _: None)
    )
    script = tmp_path / "strict_vendor.py"
    script.write_text(
        """import json, os, sys, time
from pathlib import Path
root = Path(__file__).parent
mode = os.environ.get("FAKE_VENDOR_MODE", "success")
trace = root / "transcript.jsonl"
def record(value):
    with trace.open("a", encoding="utf-8") as f:
        f.write(json.dumps(value) + "\\n")
record({"argv": sys.argv[1:], "token_present": "RELAYFENCE_TOKEN" in os.environ,
        "builtin": os.environ.get("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"),
        "personal": os.environ.get("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE")})
def emit(value):
    print(json.dumps(value), flush=True)
def receive():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    value = json.loads(line)
    record(value)
    assert "jsonrpc" not in value, "ZCode protocol forbids a JSON-RPC version field"
    return value
request = receive()
if mode == "malformed":
    print("this is not JSON", flush=True)
    time.sleep(20)
if mode == "oversized_line":
    print("x" * 1048577, flush=True)
    time.sleep(20)
if mode == "closed":
    sys.exit(0)
if mode == "timeout":
    time.sleep(20)
if mode == "error":
    emit({"id": request["id"], "error": {"message": "provider rejected"}})
    time.sleep(20)
reads = 0
while True:
    method = request["method"]
    if method == "provider/updateAccountConfig":
        result = {"updated": True}
    elif method == "session/create":
        emit({"id": 999, "method": "client/requestPermission", "params": {"tool": "shell"}})
        reply = receive()
        assert reply["id"] == 999 and reply["error"]["code"] == -32601
        if mode == "auth_callback":
            emit({"id": 1000, "method": "interaction/requestProviderRuntimeHeaders",
                  "params": {"providerId": request["params"]["model"]["providerId"],
                             "modelSelection": request["params"]["model"],
                             "accountAccess": {"mode": "start-plan", "accountType": "zai"}}})
            auth_reply = receive()
            assert auth_reply["id"] == 1000
            assert auth_reply["result"]["requestAuth"]["apiKey"] == "fixture-private-token"
            emit({"method": "provider/diagnostic", "params": {"echo": "fixture-private-token"}})
        emit({"method": "session/notification", "params": {"status": "starting"}})
        result = {"session": {"sessionId": "portable-session", "status": "idle"},
                  "settings": {"model": {"current": request["params"]["model"]},
                               "mode": {"current": request["params"]["mode"]},
                               "thoughtLevel": {"current": request["params"]["thoughtLevel"]}}}
        if mode == "wrong_model":
            result["settings"]["model"]["current"]["modelId"] = "substituted-model"
        if mode == "wrong_permission":
            result["settings"]["mode"]["current"] = "yolo"
        if mode == "wrong_reasoning":
            result["settings"]["thoughtLevel"]["current"] = "low"
    elif method == "session/send":
        assert request["params"]["sessionId"] == "portable-session"
        assert "task-under-test" in request["params"]["content"]
        result = {}
    elif method == "session/read":
        reads += 1
        result = {"session": {"status": "running"}, "messages": []}
        if reads > 1:
            if mode != "missing_result":
                value = {"outcome": "blocked" if mode == "blocked" else "completed",
                         "summary": "Portable execution", "details": "Protocol validated",
                         "files": ["owned.py"], "checks": ["fake worker contract"]}
                (root / "vendor-result.json").write_text(json.dumps(value), encoding="utf-8")
            result = {"session": {"status": "completed"}, "messages": [{"content": "done"}]}
    else:
        raise AssertionError("Unexpected method " + method)
    emit({"id": request["id"], "result": result})
    request = receive()
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("RELAYFENCE_TOKEN", "must-not-reach-vendor")
    monkeypatch.setenv("FAKE_VENDOR_MODE", "success")
    return script


def execute(script, **kwargs):
    return adapter.execute_zcode(
        sys.executable,
        str(script),
        "explicit-model",
        {"provider": "account:vendor", "reasoning_effort": "medium"},
        {"task": {"task_id": "task-under-test", "outputs": []}},
        script.parent,
        "builtin.json",
        "personal.json",
        **kwargs,
    )


@pytest.mark.parametrize(
    "permission_mode,expected_mode",
    [
        (None, "build"),
        ("read-only", "plan"),
        ("workspace-write", "build"),
        ("danger-full-access", "yolo"),
    ],
)
def test_real_zcode_pipe_roundtrip_preserves_full_requested_selection(
    vendor_process, permission_mode, expected_mode
):
    result = execute(vendor_process, permission_mode=permission_mode)
    assert result["outputs"][0]["key"] == "tasks/task-under-test/result"
    assert result["outputs"][0]["value"]["summary"] == "Portable execution"
    transcript = [
        json.loads(line)
        for line in (vendor_process.parent / "transcript.jsonl").read_text().splitlines()
    ]
    metadata = transcript[0]
    assert metadata == {
        "argv": ["app-server"],
        "token_present": False,
        "builtin": "builtin.json",
        "personal": "personal.json",
    }
    requests = [value for value in transcript[1:] if "method" in value]
    assert [value["method"] for value in requests] == [
        "session/create",
        "session/send",
        "session/read",
        "session/read",
    ]
    create = requests[0]["params"]
    assert create["mode"] == expected_mode
    assert create["workspace"]["workspacePath"] == str(Path.cwd())
    assert create["model"] == {
        "providerId": "account:vendor",
        "modelId": "explicit-model",
        "options": {"reasoningLevel": "medium"},
    }
    assert create["thoughtLevel"] == "medium"
    assert all("jsonrpc" not in value for value in transcript)
    assert any(
        value.get("id") == 999 and value.get("error", {}).get("code") == -32601
        for value in transcript
    )


def test_account_snapshot_is_sent_before_creating_a_session(vendor_process):
    snapshot = vendor_process.parent / "account.json"
    snapshot.write_text(json.dumps({"accountId": "fixture-account", "providers": []}))
    execute(vendor_process, account_snapshot=snapshot)
    messages = [
        json.loads(line)
        for line in (vendor_process.parent / "transcript.jsonl").read_text().splitlines()
    ]
    assert messages[1]["method"] == "provider/updateAccountConfig"
    assert messages[1]["params"] == {"accountId": "fixture-account", "providers": []}
    assert messages[2]["method"] == "session/create"


@pytest.mark.parametrize(
    "mode,match",
    [
        ("malformed", "Invalid vendor protocol"),
        ("closed", "stream closed"),
        ("error", "rejected protocol"),
        ("missing_result", "bounded structured result"),
    ],
)
def test_protocol_failure_never_publishes_success(vendor_process, monkeypatch, mode, match):
    monkeypatch.setenv("FAKE_VENDOR_MODE", mode)
    with pytest.raises(RuntimeError, match=match):
        execute(vendor_process)


def test_silent_process_times_out_and_is_reaped(vendor_process, monkeypatch):
    monkeypatch.setenv("FAKE_VENDOR_MODE", "timeout")
    original_queue = queue.Queue

    class BoundedWaitQueue(original_queue):
        def get(self, block=True, timeout=None):
            return super().get(block=block, timeout=min(timeout, 0.3) if timeout else timeout)

    monkeypatch.setattr(adapter.queue, "Queue", BoundedWaitQueue)
    ticks = iter([0, 60, 121])
    monkeypatch.setattr(adapter, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with pytest.raises(RuntimeError, match="request timed out"):
        execute(vendor_process)
    # The adapter's finally block has joined its reader after terminating the child;
    # no process holds this private log open when execution returns.
    (vendor_process.parent / "vendor-events.log").unlink()


def test_blocked_worker_result_is_not_a_completed_receipt(vendor_process, monkeypatch):
    monkeypatch.setenv("FAKE_VENDOR_MODE", "blocked")
    with pytest.raises(adapter.AgentIncomplete):
        execute(vendor_process)


@pytest.mark.parametrize("vendor", ["dsh", "zcode"])
def test_main_passes_explicit_arguments_to_vendor_and_prints_structured_receipt(
    tmp_path, monkeypatch, capsys, vendor
):
    parameters = {"provider": "chosen"}
    if vendor == "zcode":
        parameters["reasoning_effort"] = "high"
    argv = [
        "vendor_adapter",
        "--vendor",
        vendor,
        "--executable",
        "runtime",
        "--script",
        "cli.js",
        "--model",
        "chosen-model",
        "--parameters",
        json.dumps(parameters),
        "--permission-mode",
        "workspace-write",
        "--builtin-config",
        "builtin.json",
        "--personal-config",
        "personal.json",
        "--account-snapshot",
        "account.json",
        "--patch",
        "patch.yml",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"task": {"task_id": "cli"}}')))
    monkeypatch.setenv("RELAYFENCE_RUN_DIRECTORY", str(tmp_path))
    calls = []

    def worker(*args):
        calls.append(args)
        return {"outputs": [{"key": "result", "expected_version": 0, "value": "done"}]}

    monkeypatch.setattr(adapter, "execute_" + vendor, worker)
    assert adapter.main() == 0
    assert json.loads(capsys.readouterr().out)["outputs"][0]["value"] == "done"
    assert calls[0][:6] == (
        "runtime",
        "cli.js",
        "chosen-model",
        parameters,
        {"task": {"task_id": "cli"}},
        tmp_path,
    )
    if vendor == "zcode":
        assert calls[0][6:] == (
            "builtin.json",
            "personal.json",
            "workspace-write",
            "account.json",
            None,
            1800.0,
        )
    else:
        assert calls[0][6:] == (["patch.yml"], "workspace-write")


@pytest.mark.parametrize("failure", ["blocked", "runtime", "oversized", "missing_config"])
def test_main_failure_contract(tmp_path, monkeypatch, capsys, failure):
    argv = [
        "vendor_adapter",
        "--vendor",
        "zcode",
        "--executable",
        "runtime",
        "--script",
        "cli.js",
        "--model",
        "model",
        "--parameters",
        "{}",
    ]
    if failure != "missing_config":
        argv += ["--builtin-config", "b", "--personal-config", "p"]
    monkeypatch.setattr(sys, "argv", argv)
    raw = b"x" * (adapter.MAX_PAYLOAD_BYTES + 1) if failure == "oversized" else b"{}"
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    monkeypatch.setenv("RELAYFENCE_RUN_DIRECTORY", str(tmp_path))

    def worker(*args):
        if failure == "blocked":
            raise adapter.AgentIncomplete("blocked", "Needs clarification")
        raise RuntimeError("private vendor failure")

    monkeypatch.setattr(adapter, "execute_zcode", worker)
    assert adapter.main() == (0 if failure == "blocked" else 1)
    captured = capsys.readouterr()
    if failure == "blocked":
        assert json.loads(captured.out)["outputs"] == []
        assert json.loads(captured.out)["error"]["code"] == "agent_blocked"
    else:
        assert captured.out == ""
        assert "private runtime logs" in captured.err
        assert "private vendor failure" not in captured.err


@pytest.mark.parametrize("log_mode", ["single", "ambiguous", "invalid", "oversized"])
def test_dsh_structured_stdout_fallback_is_bounded_and_unambiguous(tmp_path, monkeypatch, log_mode):
    value = {
        "outcome": "completed",
        "summary": "Recovered stdout result",
        "details": "Done",
        "files": [],
        "checks": [],
    }

    def run(command, **kwargs):
        assert kwargs["env"]["DSH_PERMISSION_MODE"] == "workspace-write"
        assert "RELAYFENCE_TOKEN" not in kwargs["env"]
        if log_mode == "oversized":
            content = b"x" * (adapter.MAX_PAYLOAD_BYTES + 1)
        elif log_mode == "invalid":
            content = b'Runtime notice: {not JSON} and {"unrelated": true}\n'
        else:
            content = ("Runtime notice\n" + json.dumps(value) + "\n").encode()
            if log_mode == "ambiguous":
                content += json.dumps({**value, "summary": "Another result"}).encode()
        kwargs["stdout"].write(content)
        kwargs["stdout"].flush()
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("RELAYFENCE_TOKEN", "private")
    monkeypatch.setattr(adapter.subprocess, "run", run)
    args = (
        "runtime.exe",
        "cli.js",
        "model",
        {"provider": "explicit-provider"},
        {"task": {"task_id": "stdout-task", "outputs": []}},
        tmp_path,
    )
    if log_mode == "single":
        result = adapter.execute_dsh(*args, permission_mode="workspace-write")
        assert result["outputs"][0]["value"] == value
    else:
        with pytest.raises(RuntimeError, match="bounded|unambiguous"):
            adapter.execute_dsh(*args, permission_mode="workspace-write")


def test_dsh_invalid_permission_mode_does_not_start_runtime(tmp_path, monkeypatch):
    def forbidden_run(*args, **kwargs):
        pytest.fail("Invalid permissions must fail before starting the runtime")

    monkeypatch.setattr(adapter.subprocess, "run", forbidden_run)
    with pytest.raises(ValueError, match="permission mode"):
        adapter.execute_dsh(
            "runtime.exe",
            "cli.js",
            "model",
            {"provider": "explicit"},
            {},
            tmp_path,
            permission_mode="invented",
        )


def test_long_task_result_key_is_stable_bounded_and_preserves_version():
    task_id = "task/" + "long" * 40
    value = {
        "outcome": "completed",
        "summary": "Done",
        "details": "Done",
        "files": [],
        "checks": [],
    }
    first = adapter.result_output({"task": {"task_id": task_id, "outputs": []}}, value)
    key = first["outputs"][0]["key"]
    assert len(key) <= 128
    second = adapter.result_output(
        {"task": {"task_id": task_id, "outputs": [{"key": key, "version": 7}]}}, value
    )
    assert second["outputs"][0]["key"] == key
    assert second["outputs"][0]["expected_version"] == 7


@pytest.mark.parametrize("mode", ["wrong_model", "wrong_permission", "wrong_reasoning"])
def test_runtime_must_acknowledge_selection_before_prompt_is_sent(
    vendor_process, monkeypatch, mode
):
    monkeypatch.setenv("FAKE_VENDOR_MODE", mode)
    with pytest.raises(RuntimeError, match="acknowledge"):
        execute(vendor_process, permission_mode="workspace-write")
    transcript = (vendor_process.parent / "transcript.jsonl").read_text()
    assert '"method": "session/send"' not in transcript


def auth_request():
    return {
        "providerId": "account:zai-start-plan",
        "modelSelection": {"providerId": "account:zai-start-plan", "modelId": "GLM-5.3"},
        "accountAccess": {"mode": "start-plan", "accountType": "zai"},
    }


@pytest.fixture
def credentials(tmp_path):
    path = tmp_path / "fixture-credentials.json"
    path.write_text(
        json.dumps({"oauth:active_provider": "zai", "zcodejwttoken": "fixture-private-token"})
    )
    return path


def test_explicit_existing_account_auth_returns_selected_account_only(credentials, capsys):
    result = adapter.zcode_account_auth(
        auth_request(), "GLM-5.3", {"provider": "account:zai-start-plan"}, credentials
    )
    assert result == {"headersApplied": True, "requestAuth": {"apiKey": "fixture-private-token"}}
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("mismatch", ["provider", "model", "selection_provider", "family", "mode"])
def test_account_auth_rejects_callback_route_substitution(credentials, mismatch):
    request = auth_request()
    if mismatch == "provider":
        request["providerId"] = "other"
    elif mismatch == "model":
        request["modelSelection"]["modelId"] = "other-model"
    elif mismatch == "selection_provider":
        request["modelSelection"]["providerId"] = "other"
    elif mismatch == "family":
        request["accountAccess"]["accountType"] = "bigmodel"
    else:
        request["accountAccess"]["mode"] = "api-key"
    with pytest.raises(ValueError, match="selected Start Plan route"):
        adapter.zcode_account_auth(
            request, "GLM-5.3", {"provider": "account:zai-start-plan"}, credentials
        )


@pytest.mark.parametrize(
    "contents,match",
    [
        ({"oauth:active_provider": "bigmodel", "zcodejwttoken": "fixture"}, "family"),
        ({"oauth:active_provider": "zai"}, "missing"),
        ({"oauth:active_provider": "zai", "zcodejwttoken": "  "}, "missing"),
        ({"oauth:active_provider": "zai", "zcodejwttoken": 42}, "missing"),
    ],
)
def test_account_auth_requires_current_family_and_nonempty_token(credentials, contents, match):
    credentials.write_text(json.dumps(contents))
    with pytest.raises(ValueError, match=match):
        adapter.zcode_account_auth(
            auth_request(), "GLM-5.3", {"provider": "account:zai-start-plan"}, credentials
        )


def test_account_auth_bounds_credential_file_before_reading(credentials):
    credentials.write_bytes(b"x" * (adapter.MAX_PAYLOAD_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        adapter.zcode_account_auth(
            auth_request(), "GLM-5.3", {"provider": "account:zai-start-plan"}, credentials
        )


@pytest.mark.parametrize("decode_result", ["success", "failed", "oversized"])
def test_encrypted_account_uses_private_captured_pipe(
    credentials, monkeypatch, capsys, decode_result
):
    credentials.write_text(
        json.dumps({"oauth:active_provider": "enc:v1:fixture", "zcodejwttoken": "enc:v1:fixture"})
    )

    def decode(command, **kwargs):
        assert command[0:2] == ["node", "-e"]
        assert command[-1] == str(credentials)
        assert "fixture-private-token" not in " ".join(command)
        assert kwargs == {"capture_output": True, "timeout": 10, "check": False}
        output = json.dumps(
            {"oauth:active_provider": "zai", "zcodejwttoken": "fixture-private-token"}
        ).encode()
        if decode_result == "oversized":
            output = b"x" * (adapter.MAX_PAYLOAD_BYTES + 1)
        return SimpleNamespace(returncode=1 if decode_result == "failed" else 0, stdout=output)

    monkeypatch.setattr(adapter.subprocess, "run", decode)
    args = (auth_request(), "GLM-5.3", {"provider": "account:zai-start-plan"}, credentials)
    if decode_result == "success":
        assert adapter.zcode_account_auth(*args)["requestAuth"]["apiKey"] == "fixture-private-token"
    else:
        with pytest.raises(ValueError, match="could not be read"):
            adapter.zcode_account_auth(*args)
    captured = capsys.readouterr()
    assert "fixture-private-token" not in captured.out + captured.err


def test_runtime_auth_callback_is_scoped_and_secret_is_redacted(
    vendor_process, credentials, monkeypatch
):
    monkeypatch.setenv("FAKE_VENDOR_MODE", "auth_callback")
    result = adapter.execute_zcode(
        sys.executable,
        str(vendor_process),
        "GLM-5.3",
        {"provider": "account:zai-start-plan", "reasoning_effort": "medium"},
        {"task": {"task_id": "task-under-test", "outputs": []}},
        vendor_process.parent,
        "builtin.json",
        "personal.json",
        credentials_file=credentials,
    )
    assert result["outputs"][0]["value"]["outcome"] == "completed"
    log = (vendor_process.parent / "vendor-events.log").read_text()
    assert "fixture-private-token" not in log
    assert "[REDACTED]" in log


def test_oversized_protocol_line_is_rejected(vendor_process, monkeypatch):
    monkeypatch.setenv("FAKE_VENDOR_MODE", "oversized_line")
    with pytest.raises(RuntimeError, match="line exceeded its size limit"):
        execute(vendor_process)


def test_account_snapshot_is_bounded_before_protocol_submission(vendor_process):
    snapshot = vendor_process.parent / "oversized-account.json"
    snapshot.write_bytes(b"x" * (adapter.MAX_PAYLOAD_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        execute(vendor_process, account_snapshot=snapshot)
