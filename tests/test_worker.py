import asyncio
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_registry import SELECTION, bind, register

from relayfence.cli import main
from relayfence.errors import CoordinationError
from relayfence.locking import WorkspaceBusy
from relayfence.worker import command_arguments, execute_command, run_task


class LocalClient:
    def __init__(self, ledger, principal):
        self.ledger = ledger
        self.principal = principal

    def call(self, operation, **payload):
        result, status = self.ledger.service.call(operation, payload, self.principal)
        if not result["ok"]:
            error = result["error"]
            raise CoordinationError(error["code"], error["message"], error["details"], status)
        return result

    @asynccontextmanager
    async def async_peer(self, timeout):
        async def call(operation, **payload):
            return self.call(operation, **payload)

        yield SimpleNamespace(call=call)


@pytest.fixture
def command_task(ledger, tmp_path):
    principal, token = register(ledger, mode="command")
    bind(ledger)
    ledger.ok("task_create", task_id="t", title="Test", role_id="developer")
    script = tmp_path / "adapter.py"
    script.write_text(
        "import json, os, sys\n"
        "request = json.load(sys.stdin)\n"
        "assert 'RELAYFENCE_TOKEN' not in os.environ\n"
        "assert 'lease_token' not in request\n"
        "assert sys.argv[1] == request['selection']['model']\n"
        "assert sys.argv[2] == request['selection']['parameters']['effort']\n"
        "print(json.dumps({'reported_model':sys.argv[1], 'outputs':[{'key':'result','expected_version':0,'value':{'model':sys.argv[1],'effort':sys.argv[2]}}]}))\n",
        encoding="utf-8",
    )
    config = tmp_path / "worker.json"
    config.write_text(
        json.dumps(
            {
                "agent_id": "coder",
                "command": [sys.executable, str(script), "{model}", "{parameter:effort}"],
                "timeout_seconds": 2,
            }
        )
    )
    return LocalClient(ledger, principal), config, ledger, token


def test_real_command_receives_selected_model_and_parameters_and_publishes(
    command_task, tmp_path, monkeypatch
):
    client, config, ledger, _ = command_task
    monkeypatch.setenv("RELAYFENCE_TOKEN", "must-not-reach-subprocess")
    directory = tmp_path / "run"
    result = run_task(client, config, "t", directory)
    assert result["data"]["task"]["status"] == "done"
    assert ledger.ok("item_get", key="result")["value"] == {"model": "model-a", "effort": "high"}
    assert result["data"]["task"]["execution"]["model_evidence"] == "worker_reported"
    payload = json.loads((directory / "complete-request.json").read_text())
    assert client.call("task_complete", **payload)["replayed"]
    with pytest.raises(FileExistsError):
        run_task(client, config, "t", directory)
    if os.name != "nt":
        assert (directory / "claim-response.json").stat().st_mode & 0o777 == 0o600


def test_shared_checkout_stays_exclusive_until_child_exits(command_task, tmp_path):
    client, config, ledger, _ = command_task
    data = json.loads(config.read_text())
    data["cwd"] = str(tmp_path)
    config.write_text(json.dumps(data))
    marker, release = tmp_path / "started", tmp_path / "release"
    Path(data["command"][1]).write_text(
        "import json,sys,time\nfrom pathlib import Path\n"
        "r=json.loads(sys.stdin.buffer.read())\n"
        "Path('started').write_text('started')\n"
        "while not Path('release').exists(): time.sleep(.02)\n"
        "print(json.dumps({'outputs':[{'key':'results/'+r['task']['task_id'],'expected_version':0,'value':'done'}]}))\n",
        encoding="utf-8",
    )
    ledger.ok("task_create", task_id="second", title="Second writer", role_id="developer")
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(run_task, client, config, "t", tmp_path / "first")
        try:
            deadline = time.monotonic() + 5
            while not marker.exists():
                assert time.monotonic() < deadline
                time.sleep(0.02)
            with pytest.raises(WorkspaceBusy):
                run_task(client, config, "second", tmp_path / "second")
            assert ledger.ok("task_get", task_id="second")["fence"] == 0
        finally:
            release.write_text("released")
        assert first.result(timeout=5)["data"]["task"]["status"] == "done"
    assert (
        run_task(client, config, "second", tmp_path / "second")["data"]["task"]["status"] == "done"
    )


@pytest.mark.parametrize(
    "body,message",
    [
        ("import sys;sys.exit(3)", "code 3"),
        ("print('not json')", "WorkerResult"),
        ("import time;time.sleep(20)", "timed out"),
        ("print('x'*140000)", "128 KiB|size limit"),
    ],
)
def test_failed_command_records_failure_without_publishing(command_task, tmp_path, body, message):
    client, config, ledger, _ = command_task
    data = json.loads(config.read_text())
    Path(data["command"][1]).write_text(body)
    with pytest.raises(RuntimeError, match=message):
        run_task(client, config, "t", tmp_path / "failed")
    assert ledger.ok("task_get", task_id="t")["status"] == "failed"
    assert ledger.ok("items_list")["items"] == []


def test_adapter_reported_blocker_becomes_failed_not_done(command_task, tmp_path):
    client, config, ledger, _ = command_task
    data = json.loads(config.read_text())
    Path(data["command"][1]).write_text(
        "import json\nprint(json.dumps({'error':{'code':'agent_blocked','message':'Workspace is read only'}}))\n"
    )
    # The agent-authored summary stays in the run directory; the shared ledger
    # only records a fixed, prewritten sentence for this outcome code.
    with pytest.raises(CoordinationError, match="无法继续"):
        run_task(client, config, "t", tmp_path / "blocked")
    task = ledger.ok("task_get", task_id="t")
    assert task["status"] == "failed" and task["failure"]["code"] == "agent_blocked"
    summary = task["failure"]["message"]
    assert (
        "无法继续" in summary
        and "Workspace is read only" not in summary
        and "read only" not in summary
        and not task["outputs"]
    )


def test_unknown_completion_preserves_exact_request_without_release(command_task, tmp_path):
    client, config, ledger, _ = command_task
    actual_call = client.call

    def drop_reply(operation, **payload):
        result = actual_call(operation, **payload)
        if operation == "task_complete":
            raise CoordinationError("transport_error", "Lost response", status=503)
        return result

    client.call = drop_reply
    directory = tmp_path / "lost"
    with pytest.raises(CoordinationError, match="Lost response"):
        run_task(client, config, "t", directory)
    assert ledger.ok("task_get", task_id="t")["status"] == "done"
    saved = json.loads((directory / "complete-request.json").read_text())
    assert actual_call("task_complete", **saved)["replayed"]
    assert ledger.ok("item_get", key="result")["version"] == 1


def test_command_receives_extra_environment_without_plaintext_leaking_to_config(tmp_path):
    directory = tmp_path / "run"
    directory.mkdir()
    script = (
        "import json,os,pathlib;"
        "pathlib.Path(os.environ['RELAYFENCE_RUN_DIRECTORY'],'seen.txt').write_text("
        "os.environ.get('PROVIDER_TEST_KEY','')+'|'+os.environ.get('RELAYFENCE_TOKEN',''));"
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
            20,
            heartbeat,
            0.1,
            extra_env={
                "PROVIDER_TEST_KEY": "secret-in-process-only",
                "RELAYFENCE_TOKEN": "must-not-leak",
            },
        )
    )
    assert result.outputs[0].key == "result"
    assert (directory / "seen.txt").read_text() == "secret-in-process-only|"


def test_command_environment_inherits_only_allowlisted_relayfence_names(tmp_path, monkeypatch):
    directory = tmp_path / "run"
    directory.mkdir()
    script = (
        "import json,os,pathlib;"
        "names=sorted(k for k in os.environ if k.startswith('RELAYFENCE_'));"
        "pathlib.Path(os.environ['RELAYFENCE_RUN_DIRECTORY'],'env.txt').write_text(json.dumps(names));"
        "print(json.dumps({'outputs':[{'key':'result','expected_version':0,'value':'ok'}]}))"
    )
    monkeypatch.setenv("RELAYFENCE_TOKEN", "t" * 40)
    monkeypatch.setenv("RELAYFENCE_SECRET_VALUE", "private")
    monkeypatch.setenv("RELAYFENCE_OPENCODE_EXECUTABLE", "explicit-opencode")

    async def heartbeat():
        return None

    asyncio.run(
        execute_command(
            [sys.executable, "-c", script],
            tmp_path,
            {},
            directory,
            20,
            heartbeat,
            0.1,
        )
    )
    inherited = json.loads((directory / "env.txt").read_text())
    assert inherited == [
        "RELAYFENCE_OPENCODE_EXECUTABLE",
        "RELAYFENCE_RUN_DIRECTORY",
        "RELAYFENCE_VENDOR_TIMEOUT",
    ]


def test_command_heartbeat_and_loss_stops_process(tmp_path):
    directory = tmp_path / "run"
    directory.mkdir()
    calls = []

    async def heartbeat():
        calls.append(1)
        raise CoordinationError("lease_lost", "Stop")

    start = time.monotonic()
    with pytest.raises(CoordinationError, match="Stop"):
        asyncio.run(
            execute_command(
                [sys.executable, "-c", "import time;time.sleep(10)"],
                tmp_path,
                {},
                directory,
                20,
                heartbeat,
                0.1,
            )
        )
    assert calls == [1] and time.monotonic() - start < 5


@pytest.mark.parametrize(
    "command",
    [
        [],
        ["{model}", "--run"],
        ["exe"],
        ["exe", "{unknown}", "{model}"],
        ["exe", "", "{model}"],
        ["exe", "{parameter:missing}", "{model}"],
        ["thing.cmd", "{model}"],
        ["exe", "a\x00b", "{model}"],
    ],
)
def test_bad_command_templates_are_rejected(command):
    with pytest.raises(ValueError):
        command_arguments(command, SELECTION)


def test_parameter_json_is_one_argument():
    args = command_arguments(["exe", "{model}", "{parameters_json}"], SELECTION)
    assert args == ["exe", "model-a", '{"effort":"high"}']


def test_cli_worker_errors_do_not_echo_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RELAYFENCE_TOKEN", "x" * 40)
    config = tmp_path / "bad.json"
    config.write_text('{"password":"secret-do-not-print"}')
    assert (
        main(
            [
                "worker",
                "--config",
                str(config),
                "--task",
                "t",
                "--run-directory",
                str(tmp_path / "run"),
                "--token-file",
                str(tmp_path / "unused"),
            ]
        )
        == 1
    )
    output = capsys.readouterr().err
    assert "Invalid local worker configuration" in output and "secret-do-not-print" not in output


def test_preflight_rejects_unsupported_modes_ids_and_paths(command_task, tmp_path):
    client, config, ledger, _ = command_task
    original = json.loads(config.read_text())
    config.write_text(json.dumps({**original, "cwd": "missing"}))
    with pytest.raises(ValueError, match="cwd"):
        run_task(client, config, "t", tmp_path / "r1")
    config.write_text(json.dumps({**original, "agent_id": "wrong"}))
    with pytest.raises(ValueError, match="different"):
        run_task(client, config, "t", tmp_path / "r2")
    config.write_text(json.dumps(original))
    ledger.ok(
        "agent_put",
        agent_id="coder",
        expected_revision=2,
        label="External",
        runtime="custom",
        models=[{"id": "model-a"}],
    )
    with pytest.raises(ValueError, match="External"):
        run_task(client, config, "t", tmp_path / "r3")
    ledger.ok("task_assign", task_id="t", expected_revision=1, role_id=None)
    with pytest.raises(ValueError, match="role-assigned"):
        run_task(client, config, "t", tmp_path / "r4")


def test_real_worker_reports_activity_using_its_authenticated_lease(command_task, tmp_path):
    client, config, ledger, _ = command_task
    data = json.loads(config.read_text())
    data.update(timeout_seconds=12, cwd=str(tmp_path))
    config.write_text(json.dumps(data))
    with ledger.store.connection() as db:
        db.execute(
            "INSERT INTO meeting_tasks VALUES (?,?,?,?,?,?)",
            ("default", "t", "", "worker", "group", "execution"),
        )
    Path(data["command"][1]).write_text(
        "import json,sys,os,time\nfrom pathlib import Path\n"
        "r=json.loads(sys.stdin.buffer.read())\n"
        "event={'type':'item.completed','item':{'type':'agent_message','text':'Reading project files'}}\n"
        "(Path(os.environ['RELAYFENCE_RUN_DIRECTORY'])/'codex-events.jsonl').write_text(json.dumps(event)+'\\n')\n"
        "while not Path('release-activity').exists(): time.sleep(.02)\n"
        "print(json.dumps({'outputs':[{'key':'result','expected_version':0,'value':'done'}]}))\n",
        encoding="utf-8",
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_task, client, config, "t", tmp_path / "live-run")
        try:
            deadline = time.monotonic() + 8
            while True:
                entries = ledger.ok("task_activity_list")["entries"]
                if entries:
                    break
                if future.done():
                    future.result()
                assert time.monotonic() < deadline
                time.sleep(0.05)
            assert entries[0]["text"] == "Reading project files"
            assert entries[0]["member_id"] == "worker"
            assert ledger.ok("task_get", task_id="t")["status"] == "running"
        finally:
            (tmp_path / "release-activity").write_text("release")
        assert future.result(timeout=5)["data"]["task"]["status"] == "done"
