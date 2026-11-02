"""Persistent project/runtime sessions and real pipe-based identity capture."""

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from relayfence.codex_adapter import execute
from relayfence.project_sessions import (
    ProjectSession,
    current_session,
    read_binding,
    stream_process,
)


def session_env(monkeypatch, tmp_path, runtime="codex"):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "private" / (runtime + ".json")
    monkeypatch.setenv("RELAYFENCE_SESSION_FILE", str(path))
    monkeypatch.setenv("RELAYFENCE_SESSION_PROJECT", "project-a")
    monkeypatch.setenv("RELAYFENCE_SESSION_RUNTIME", runtime)
    return path


def test_binding_survives_restart_and_refuses_identity_replacement(tmp_path):
    path = tmp_path / "binding.json"
    first = ProjectSession(path, "a", "codex", tmp_path)
    assert first.session_id is None
    first.begin({"task_id": "task-one"})
    first.bind("thread-123")
    first.finish(True)
    second = ProjectSession(path, "a", "codex", tmp_path)
    assert second.session_id == "thread-123" and second.value["turns"] == 1
    second.begin({"task_id": "task-two"})
    with pytest.raises(ValueError, match="其他会话"):
        second.bind("other-thread")
    second.finish(False)
    assert read_binding(path)["session_id"] == "thread-123"
    with pytest.raises(ValueError, match="不匹配"):
        ProjectSession(path, "b", "codex", tmp_path)
    with pytest.raises(ValueError, match="不匹配"):
        ProjectSession(path, "a", "dsh", tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="不匹配"):
        ProjectSession(path, "a", "codex", other)


def test_creation_without_identity_requires_explicit_recovery(tmp_path):
    path = tmp_path / "binding.json"
    first = ProjectSession(path, "a", "codex", tmp_path)
    first.begin({"task_id": "task"})
    with pytest.raises(ValueError, match="没有报告"):
        first.finish(True)
    with pytest.raises(ValueError, match="未取得身份"):
        ProjectSession(path, "a", "codex", tmp_path)
    with pytest.raises(ValueError, match="有效"):
        first.bind("--last")


@pytest.mark.parametrize(
    "data",
    [b"bad-json", b"[]", b'{"version":9}', b'{"version":1,"session_id":[]}', b"x" * 32769],
    ids=["bad-json", "array", "version", "invalid-identity", "oversize"],
)
def test_corrupt_bindings_never_silently_start_new_history(tmp_path, data):
    path = tmp_path / "binding.json"
    path.write_bytes(data)
    with pytest.raises(ValueError):
        read_binding(path)


def test_session_env_validates_runtime_and_does_not_put_private_paths_in_prompt(
    tmp_path, monkeypatch
):
    assert current_session("codex", {}) is None
    path = session_env(monkeypatch, tmp_path)
    envelope = {"task": {"task_id": "t"}, "selection": {"model": "model", "parameters": {}}}
    with pytest.raises(ValueError, match="运行时不匹配"):
        current_session("dsh", envelope)
    monkeypatch.delenv("RELAYFENCE_SESSION_PROJECT")
    with pytest.raises(ValueError, match="缺少项目"):
        current_session("codex", envelope)
    monkeypatch.setenv("RELAYFENCE_SESSION_PROJECT", "project-a")
    bound = current_session("codex", envelope)
    assert str(path) not in json.dumps(envelope)
    assert bound.value["model"] == "model"


@pytest.mark.parametrize(
    "dialect,event",
    [
        ("codex", {"type": "thread.started", "thread_id": "thread-one"}),
        ("dsh", {"type": "session", "sessionId": "session-one"}),
        ("opencode", {"type": "text", "sessionID": "open-one"}),
    ],
)
def test_stream_captures_native_identity_even_when_process_later_fails(tmp_path, dialect, event):
    binding = ProjectSession(tmp_path / "binding.json", "p", dialect, tmp_path)
    binding.begin({"task_id": "task"})
    script = tmp_path / "fake.py"
    script.write_text(
        'import sys,json\nsys.stdin.buffer.read()\nprint("not json")\nprint("[]")\nprint('
        + repr(json.dumps(event))
        + ")\nsys.exit(1)\n"
    )
    events = io.BytesIO()
    result = stream_process([sys.executable, str(script)], b"prompt", events, binding, dialect)
    binding.finish(False)
    assert result.returncode == 1 and binding.session_id
    assert json.dumps(event).encode() in events.getvalue()
    assert ProjectSession(binding.path, "p", dialect, tmp_path).session_id == binding.session_id


def test_codex_second_task_resumes_exact_project_thread_with_current_model(tmp_path, monkeypatch):
    path = session_env(monkeypatch, tmp_path)
    calls = []

    def run(command, prompt, events, binding, dialect):
        calls.append(command)
        assert "--ephemeral" not in command
        assert str(path).encode() not in prompt
        binding.bind("project-thread")
        run_dir = Path(command[command.index("--output-last-message") + 1]).parent
        (run_dir / "codex-result.json").write_text(
            json.dumps(
                {
                    "outcome": "completed",
                    "summary": "done",
                    "details": "verified",
                    "files": [],
                    "checks": [],
                }
            )
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("relayfence.codex_adapter.stream_process", run)
    for i, model in enumerate(["model-one", "model-two"]):
        directory = tmp_path / str(i)
        directory.mkdir()
        execute(
            "codex.exe",
            "read-only",
            {"model": model, "parameters": {"reasoning_effort": "medium"}},
            {"task": {"task_id": "task-" + str(i), "outputs": []}},
            directory,
        )
    assert "resume" not in calls[0]
    assert calls[1][calls[1].index("resume") + 1] == "project-thread"
    assert calls[1][calls[1].index("--model") + 1] == "model-two"
    assert read_binding(path)["turns"] == 2


def test_zcode_resumes_same_native_session_and_applies_new_model(tmp_path, monkeypatch):
    from relayfence.vendor_adapter import execute_zcode

    session_env(monkeypatch, tmp_path, "zcode")
    script = tmp_path / "protocol.py"
    script.write_text(
        """import json,sys,re
from pathlib import Path
root=Path(__file__).parent
state=root/'native-session.json'
while line:=sys.stdin.readline():
 r=json.loads(line); method=r['method']; p=r['params']
 with (root/'trace.jsonl').open('a') as f:f.write(json.dumps({'method':method,'params':p})+'\\n')
 if method=='session/create':
  v={'session':{'sessionId':'native-project-session','status':'idle'},'settings':{'model':{'current':p['model']},'mode':{'current':p['mode']},'thoughtLevel':{'current':p['thoughtLevel']}}}
  state.write_text(json.dumps(v))
 elif method=='session/resume':
  v=json.loads(state.read_text());assert p['sessionId']==v['session']['sessionId']
 elif method in ['session/setModel','session/setThoughtLevel','session/setMode']:
  v=json.loads(state.read_text())
  field={'session/setModel':'model','session/setThoughtLevel':'thoughtLevel','session/setMode':'mode'}[method]
  v['settings'][field]['current']=p[field]
  state.write_text(json.dumps(v))
 elif method=='session/send':
  assert p['sessionId']=='native-project-session'
  result=re.search(r'JSON object to (.*?) with outcome',p['content']).group(1)
  Path(result).write_text(json.dumps({'outcome':'completed','summary':'done','details':'ok','files':[],'checks':[]}))
  v={}
 elif method=='session/read':v={'session':{'status':'completed'},'messages':[{'content':'done'}]}
 else:raise AssertionError(method)
 print(json.dumps({'id':r['id'],'result':v}),flush=True)
""",
        encoding="utf-8",
    )
    for i, model in enumerate(["first-model", "second-model"]):
        directory = tmp_path / ("run-" + str(i))
        directory.mkdir()
        execute_zcode(
            sys.executable,
            str(script),
            model,
            {"provider": "provider", "reasoning_effort": "high"},
            {"task": {"task_id": "work-" + str(i), "outputs": []}},
            directory,
            "builtin.json",
            "personal.json",
            permission_mode="danger-full-access",
        )
    trace = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert sum(r["method"] == "session/create" for r in trace) == 1
    assert sum(r["method"] == "session/resume" for r in trace) == 1
    assert (
        next(r for r in trace if r["method"] == "session/setModel")["params"]["model"]["modelId"]
        == "second-model"
    )
    assert read_binding(tmp_path / "private" / "zcode.json")["turns"] == 2


def test_dsh_resumes_saved_session_in_json_mode_without_last_session_guessing(
    tmp_path, monkeypatch
):
    from relayfence.vendor_adapter import execute_dsh

    path = session_env(monkeypatch, tmp_path, "dsh")
    calls = []

    def run(command, prompt, events, binding, dialect, env):
        calls.append(command)
        assert "--json" in command and dialect == "dsh"
        binding.bind("session-fixed")
        result = Path(command[command.index("--patch") + 1]).parent / "vendor-result.json"
        result.write_text(
            json.dumps(
                {
                    "outcome": "completed",
                    "summary": "done",
                    "details": "ok",
                    "files": [],
                    "checks": [],
                }
            )
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("relayfence.vendor_adapter.stream_process", run)
    for i in range(2):
        directory = tmp_path / str(i)
        directory.mkdir()
        execute_dsh(
            "electron.exe",
            "cli.js",
            "model",
            {"provider": "deepseek-account"},
            {"task": {"task_id": "work-" + str(i), "outputs": []}},
            directory,
        )
    assert "--session-id" not in calls[0]
    assert calls[1][calls[1].index("--session-id") + 1] == "session-fixed"
    assert read_binding(path)["turns"] == 2


def test_worker_forwards_only_explicit_project_session_metadata(tmp_path):
    import asyncio

    from relayfence.worker import execute_command

    directory = tmp_path / "run"
    directory.mkdir()
    script = (
        "import os,json;"
        "assert os.environ['RELAYFENCE_SESSION_PROJECT']=='project-a';"
        "assert os.environ['RELAYFENCE_SESSION_RUNTIME']=='codex';"
        "assert os.environ['RELAYFENCE_SESSION_FILE']=='private-binding';"
        "assert 'RELAYFENCE_TOKEN' not in os.environ;"
        "print(json.dumps({'outputs':[{'key':'result','expected_version':0,'value':'ok'}]}))"
    )

    async def heartbeat():
        return None

    result = asyncio.run(
        execute_command(
            [sys.executable, "-c", script],
            tmp_path,
            {},
            directory,
            10,
            heartbeat,
            0.1,
            extra_env={
                "RELAYFENCE_SESSION_PROJECT": "project-a",
                "RELAYFENCE_SESSION_RUNTIME": "codex",
                "RELAYFENCE_SESSION_FILE": "private-binding",
                "RELAYFENCE_TOKEN": "must-not-forward",
            },
        )
    )
    assert result.outputs[0].value == "ok"


def test_project_session_local_routes_use_existing_browser_authorization(ledger):
    from starlette.testclient import TestClient

    from relayfence.server import create_app

    manager = SimpleNamespace(
        start_discovery=lambda: None,
        close=lambda: None,
        project_sessions=lambda p: {"project": p, "sessions": []},
        reset_project_session=lambda p: {"project": p["project"], "sessions": []},
    )
    app = create_app(ledger.store, "t" * 40, local_ui=True, workspace_manager=manager)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as client:
        headers = {
            "X-RelayFence-Local": "1",
            "Sec-Fetch-Site": "same-origin",
            "Origin": "http://127.0.0.1",
        }
        assert client.post(
            "/local/project-sessions", json={"project": "a"}, headers=headers
        ).json() == {"project": "a", "sessions": []}
        assert (
            client.post(
                "/local/project-session-reset", json={"project": "a"}, headers=headers
            ).status_code
            == 200
        )
        assert client.post("/local/project-sessions", json={"project": "a"}).status_code == 401
