"""Real subprocess regressions: wrappers, descendants, lost owners and slow IPC."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_worker import command_task  # noqa: F401

from relayfence.client import RelayFenceClient
from relayfence.dispatcher import Dispatcher
from relayfence.errors import CoordinationError
from relayfence.processes import managed_process
from relayfence.runner import LocalServer
from relayfence.worker import execute_command

RESULT = {"outputs": [{"key": "result", "expected_version": 0, "value": "ok"}]}
PYTHON = str(Path(sys.base_prefix) / "python.exe") if os.name == "nt" else sys.executable


def alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            assert ctypes.get_last_error() == 87  # PID no longer exists.
            return False
        try:
            return api.WaitForSingleObject(handle, 0) == 258
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat")
        # A killed orphan can remain a zombie until init reaps it on Linux CI.
        return not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (ProcessLookupError, FileNotFoundError):
        return False


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "Timed out waiting for process lifecycle transition"
        time.sleep(0.02)


@pytest.fixture
def process_tree(tmp_path):
    pids = tmp_path / "pids.json"
    child = (
        "import json,os,subprocess,time\nfrom pathlib import Path\n"
        f"p=subprocess.Popen([{PYTHON!r},'-c','import time; time.sleep(60)'])\n"
        f"pids=Path({str(pids)!r})\n"
        "pending=pids.with_suffix('.pending')\n"
        "pending.write_text(json.dumps([os.getpid(),p.pid]))\n"
        "pending.replace(pids)\ntime.sleep(60)\n"
    )
    adapter = tmp_path / "tree-adapter.py"
    adapter.write_text(
        "import json,subprocess,sys,time\nfrom pathlib import Path\n"
        f"subprocess.Popen([{PYTHON!r},'-c',{child!r}])\n"
        f"while not Path({str(pids)!r}).exists(): time.sleep(.01)\n"
        "if sys.argv[1]=='failure': sys.exit(7)\n"
        "if sys.argv[1]=='wait': time.sleep(60)\n"
        f"print({json.dumps(RESULT)!r})\n"
    )
    directory = tmp_path / "run"
    directory.mkdir()
    try:
        yield adapter, directory, pids
    finally:
        # A failing regression must not leak test children. Never enumerate/kill
        # unrelated interpreters or agents on the developer's machine.
        if pids.exists():
            for pid in json.loads(pids.read_text()):
                if alive(pid):
                    os.kill(pid, signal.SIGTERM)


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout", "stop", "lease"])
def test_descendants_stop_even_when_adapter_exits_first(process_tree, outcome):
    adapter, directory, pids = process_tree
    stop = threading.Event()

    async def heartbeat():
        if pids.exists():
            if outcome == "stop":
                stop.set()
            if outcome == "lease":
                raise CoordinationError("lease_lost", "lease revoked")

    mode = outcome if outcome in {"success", "failure"} else "wait"
    invocation = execute_command(
        [sys.executable, str(adapter), mode],
        directory.parent,
        {},
        directory,
        1.5 if outcome == "timeout" else 10,
        heartbeat,
        0.05,
        stop,
    )
    if outcome == "success":
        assert asyncio.run(invocation).outputs[0].value == "ok"
    else:
        message = {
            "failure": "code 7",
            "timeout": "timed out",
            "stop": "stopped",
            "lease": "revoked",
        }
        with pytest.raises((RuntimeError, CoordinationError), match=message[outcome]):
            asyncio.run(invocation)
    assert pids.exists(), "The test must actually create descendants"
    wait_until(lambda: not any(alive(pid) for pid in json.loads(pids.read_text())))


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel job ownership")
def test_windows_owner_crash_kills_job_without_python_cleanup(process_tree):
    adapter, directory, pids = process_tree
    (directory / "input.json").write_text("{}")
    source = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        "import os,time,sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0,{source!r})\n"
        "from relayfence.processes import managed_process\n"
        f"d=Path({str(directory)!r})\n"
        "with (d/'stdout').open('wb') as out, (d/'stderr').open('wb') as err:\n"
        f" with managed_process([{PYTHON!r},{str(adapter)!r},'wait'],input_path=d/'input.json',"
        "cwd=d,env=os.environ.copy(),stdout=out,stderr=err): time.sleep(60)\n"
    )
    owner = subprocess.Popen([PYTHON, "-c", code], creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        wait_until(pids.exists)
        assert all(alive(pid) for pid in json.loads(pids.read_text()))
        owner.kill()  # No finally/atexit handler runs in the owner.
        owner.wait(timeout=5)
        wait_until(lambda: not any(alive(pid) for pid in json.loads(pids.read_text())))
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX service-manager SIGTERM")
def test_sigterm_stops_owned_group_and_restores_handler(process_tree):
    adapter, directory, pids = process_tree
    (directory / "input.json").write_text("{}")
    code = (
        "import os,signal,threading\nfrom pathlib import Path\n"
        "from relayfence.processes import managed_process,stop_on_signal\n"
        f"d=Path({str(directory)!r})\n"
        "stop=threading.Event()\nprevious=signal.getsignal(signal.SIGTERM)\n"
        "with stop_on_signal(stop), (d/'out').open('wb') as out, (d/'err').open('wb') as err:\n"
        f" with managed_process([{PYTHON!r},{str(adapter)!r},'wait'],input_path=d/'input.json',"
        "cwd=d,env=os.environ.copy(),stdout=out,stderr=err): stop.wait(60)\n"
        "assert signal.getsignal(signal.SIGTERM)==previous\n"
    )
    owner = subprocess.Popen([PYTHON, "-c", code])
    try:
        wait_until(pids.exists)
        owner.terminate()
        assert owner.wait(timeout=5) == 0
        wait_until(lambda: not any(alive(pid) for pid in json.loads(pids.read_text())))
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows startup gate")
@pytest.mark.parametrize("assignment_fails", [False, True])
def test_adapter_cannot_start_before_job_assignment(process_tree, monkeypatch, assignment_fails):
    from relayfence._windows_job import WindowsJob

    adapter, directory, pids = process_tree
    original = WindowsJob.assign

    def delayed_assignment(self, pid):
        time.sleep(0.3)
        assert not pids.exists(), "Adapter forked before the job owned the gate"
        if assignment_fails:
            raise OSError("Job assignment failed")
        original(self, pid)

    monkeypatch.setattr(WindowsJob, "assign", delayed_assignment)
    (directory / "input.json").write_text("{}")

    def launch():
        with (directory / "out").open("wb") as out, (directory / "err").open("wb") as err:
            with managed_process(
                [sys.executable, str(adapter), "success"],
                input_path=directory / "input.json",
                cwd=directory,
                env=os.environ.copy(),
                stdout=out,
                stderr=err,
            ) as process:
                assert process.wait(timeout=5) == 0

    if assignment_fails:
        with pytest.raises(OSError, match="assignment failed"):
            launch()
        assert not pids.exists()
    else:
        launch()
        wait_until(lambda: not any(alive(pid) for pid in json.loads(pids.read_text())))


@pytest.mark.parametrize("cause", ["timeout", "stop", "heartbeat"])
def test_unresponsive_http_does_not_block_process_monitor(process_tree, cause):
    adapter, directory, pids = process_tree
    requested, release, stop = threading.Event(), threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            requested.set()
            release.wait(10)  # Simulate an accepted request whose response never arrives.
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def scenario():
        with RelayFenceClient(f"http://127.0.0.1:{server.server_port}", "t" * 40) as client:
            async with client.async_peer(timeout=20) as peer:

                async def heartbeat():
                    await peer.call("task_heartbeat", task_id="test")

                async def cancel():
                    while not (requested.is_set() and pids.exists()):
                        await asyncio.sleep(0.01)
                    if cause == "stop":
                        stop.set()

                cancellation = asyncio.create_task(cancel())
                try:
                    await execute_command(
                        [sys.executable, str(adapter), "wait"],
                        directory.parent,
                        {},
                        directory,
                        1.5 if cause == "timeout" else 10,
                        heartbeat,
                        0.02,
                        stop,
                        heartbeat_timeout=1.5 if cause == "heartbeat" else 20,
                    )
                finally:
                    cancellation.cancel()
                    await asyncio.gather(cancellation, return_exceptions=True)

    started = time.monotonic()
    try:
        with pytest.raises((RuntimeError, CoordinationError), match="timed out|stopped"):
            asyncio.run(scenario())
        assert requested.is_set() and not release.is_set()
        assert time.monotonic() - started < 4, "Cancellation waited for the blocked HTTP response"
        assert pids.exists()
        wait_until(lambda: not any(alive(pid) for pid in json.loads(pids.read_text())))
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("stream,size", [("stderr", 1_048_577), ("events", 8_388_609)])
def test_final_output_limits_are_checked_after_fast_exit(tmp_path, stream, size):
    async def heartbeat():
        pass

    code = (
        "import os,sys\nfrom pathlib import Path\n"
        + (
            f"sys.stderr.write('x'*{size})\n"
            if stream == "stderr"
            else f"Path(os.environ['RELAYFENCE_RUN_DIRECTORY'],'codex-events.jsonl').write_bytes(b'x'*{size})\n"
        )
        + f"print({json.dumps(RESULT)!r})\n"
    )
    with pytest.raises(RuntimeError, match="size limit"):
        asyncio.run(
            execute_command(
                [sys.executable, "-c", code],
                tmp_path,
                {},
                tmp_path,
                10,
                heartbeat,
                3,
            )
        )


@pytest.mark.parametrize("outcome", ["success", "cancel"])
def test_real_http_dispatch_claim_execute_and_cancel(
    command_task,  # noqa: F811
    process_tree,
    tmp_path,
    outcome,
):
    _, worker_path, _, token = command_task
    adapter, _, pids = process_tree
    config = json.loads(worker_path.read_text())
    config.update(
        command=[
            sys.executable,
            str(adapter),
            "success" if outcome == "success" else "wait",
            "{model}",
        ],
        timeout_seconds=30,
    )
    worker_path.write_text(json.dumps(config))
    (tmp_path / "agent.token").write_text(token)
    dispatch_path = tmp_path / "dispatcher.json"
    dispatch_path.write_text(
        json.dumps(
            {
                "workers": [{"config": worker_path.name, "token_file": "agent.token"}],
                "poll_seconds": 0.2,
            }
        )
    )
    admin_token = "a" * 40
    with (
        LocalServer(tmp_path, admin_token) as server,
        RelayFenceClient(server.url, admin_token) as admin,
    ):
        dispatcher = Dispatcher(dispatch_path, server.url)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(dispatcher.run, once=True)
            try:
                wait_until(pids.exists, timeout=8)
                if outcome == "cancel":
                    task = admin.call("task_get", task_id="t")["data"]
                    admin.call(
                        "task_control",
                        actor="operator",
                        request_id="cancel-once",
                        task_id="t",
                        expected_revision=task["revision"],
                        action="cancel",
                    )
                future.result(timeout=8)
            finally:
                dispatcher.stop.set()
        task = admin.call("task_get", task_id="t")["data"]
        assert task["status"] == ("done" if outcome == "success" else "cancelled")
        if outcome == "cancel":
            assert admin.call("items_list")["data"]["items"] == []
        else:
            assert admin.call("item_get", key="result")["data"]["value"] == "ok"
        wait_until(lambda: not any(alive(pid) for pid in json.loads(pids.read_text())))
