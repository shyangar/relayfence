import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_registry import bind, register
from test_task_management import control
from test_worker import (
    LocalClient,
    command_task,  # noqa: F401
)

from relayfence.client import RelayFenceClient
from relayfence.dispatcher import Dispatcher
from relayfence.errors import CoordinationError
from relayfence.locking import local_lock


@pytest.fixture
def dispatch_setup(command_task, tmp_path, monkeypatch):  # noqa: F811
    client, worker, ledger, token = command_task
    data = json.loads(worker.read_text())
    Path(data["command"][1]).write_text(
        "import json,sys,time\n"
        "r=json.load(sys.stdin)\n"
        "if r['task']['description']=='fail': sys.exit(3)\n"
        "if r['task']['description']=='slow': time.sleep(20)\n"
        "print(json.dumps({'outputs':[{'key':'results/'+r['task']['task_id'],'expected_version':0,'value':{'inputs':[i['key'] for i in r['inputs']]}}]}))\n"
    )
    data["timeout_seconds"] = 30
    worker.write_text(json.dumps(data))
    token_file = tmp_path / "agent.token"
    token_file.write_text(token)
    config = tmp_path / "dispatcher.json"
    config.write_text(
        json.dumps(
            {
                "workers": [{"config": worker.name, "token_file": token_file.name}],
                "poll_seconds": 0.2,
            }
        )
    )

    @contextmanager
    def local_client(self, worker):
        yield client

    @asynccontextmanager
    async def local_async_client(self, worker):
        with self._client(worker) as sync_client:
            async with sync_client.async_peer(timeout=self.config.request_timeout_seconds) as peer:
                yield peer

    monkeypatch.setattr(Dispatcher, "_client", local_client)
    monkeypatch.setattr(Dispatcher, "_async_client", local_async_client)
    dispatcher = Dispatcher(config, "http://127.0.0.1:7433")
    yield dispatcher, ledger, config
    dispatcher.close()


def drain(dispatcher, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            while not predicate():
                assert time.monotonic() < deadline, "Dispatcher did not reach expected state"
                dispatcher.tick(pool)
                time.sleep(0.02)
        finally:
            dispatcher.close()


def test_dispatches_by_priority_then_dependencies_without_desktop(dispatch_setup):
    dispatcher, ledger, _ = dispatch_setup
    control(ledger, "t", "pause")
    ledger.ok("task_create", task_id="z-low", title="Low", role_id="developer", priority=0)
    ledger.ok("task_create", task_id="high", title="High", role_id="developer", priority=2)
    ledger.ok(
        "task_create",
        task_id="child",
        title="Child",
        role_id="developer",
        priority=3,
        depends_on=["high"],
    )
    drain(
        dispatcher,
        lambda: all(
            ledger.ok("task_get", task_id=t)["status"] == "done" for t in ["z-low", "high", "child"]
        ),
    )
    claims = [
        e["subject"]
        for e in ledger.ok("events_list", limit=200)["events"]
        if e["kind"] == "task.claimed"
    ]
    assert claims == ["high", "child", "z-low"]
    assert ledger.ok("item_get", key="results/child")["value"]["inputs"] == ["results/high"]
    assert ledger.ok("runners_list")["runners"][0]["online"]


def test_failure_is_not_repeated_after_dispatcher_restart(dispatch_setup):
    dispatcher, ledger, config = dispatch_setup
    ledger.ok(
        "task_update",
        task_id="t",
        title="Fail",
        description="fail",
        role_id="developer",
        expected_revision=1,
    )
    dispatcher.run(once=True)
    assert ledger.ok("task_get", task_id="t")["status"] == "failed"
    Dispatcher(config, dispatcher.url).run(once=True)
    assert ledger.ok("task_get", task_id="t")["fence"] == 1
    control(ledger, "t", "retry")
    Dispatcher(config, dispatcher.url).run(once=True)
    assert ledger.ok("task_get", task_id="t")["fence"] == 2


def test_operator_cancel_stops_running_process_and_prevents_publication(dispatch_setup):
    dispatcher, ledger, _ = dispatch_setup
    ledger.ok(
        "task_update",
        task_id="t",
        title="Slow",
        description="slow",
        role_id="developer",
        expected_revision=1,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        dispatcher.tick(pool)
        deadline = time.monotonic() + 8
        while ledger.ok("task_get", task_id="t")["status"] != "running":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        control(ledger, "t", "cancel")
        outcome = dispatcher.workers[0]["future"].result(timeout=6)
        assert not outcome["ok"]
    assert ledger.ok("task_get", task_id="t")["status"] == "cancelled"
    assert ledger.ok("items_list")["items"] == []


def test_lock_contention_does_not_claim_work_and_expired_work_not_auto_replayed(dispatch_setup):
    dispatcher, ledger, config = dispatch_setup
    with local_lock(dispatcher.workers[0]["cwd"] / ".relayfence" / "worker.lock"):
        dispatcher.run(once=True)
    assert ledger.ok("task_get", task_id="t")["fence"] == 0
    with dispatcher._client(dispatcher.workers[0]) as client:
        client.call(
            "task_claim",
            actor="coder",
            request_id="claim",
            task_id="t",
            selection={"model": "model-a", "parameters": {"effort": "high"}},
        )
    ledger.clock.advance(301)
    Dispatcher(config, dispatcher.url).run(once=True)
    task = ledger.ok("task_get", task_id="t")
    assert task["status"] == "running" and task["fence"] == 1


def test_shutdown_prevents_launch(dispatch_setup):
    dispatcher, ledger, _ = dispatch_setup
    dispatcher.stop.set()
    dispatcher.run()
    assert ledger.ok("task_get", task_id="t")["fence"] == 0


def test_workspace_lock_is_retried_after_its_owner_exits(dispatch_setup):
    dispatcher, ledger, _ = dispatch_setup
    with ThreadPoolExecutor(max_workers=1) as pool:
        with local_lock(dispatcher.workers[0]["cwd"] / ".relayfence" / "worker.lock"):
            dispatcher.tick(pool)
            assert dispatcher.workers[0]["future"].result(timeout=3)["code"] == "workspace_busy"
        dispatcher.tick(pool)
        assert dispatcher.workers[0]["future"].result(timeout=3)["ok"]
    assert ledger.ok("task_get", task_id="t")["status"] == "done"
    assert ledger.ok("task_get", task_id="t")["fence"] == 1


def test_priority_applies_across_agents_with_shared_workspace(dispatch_setup, monkeypatch):
    original, ledger, path = dispatch_setup
    principal, token = register(ledger, agent_id="reviewer", mode="command")
    bind(ledger, role_id="review", agent_id="reviewer")
    ledger.ok("task_create", task_id="urgent-review", title="Urgent", priority=3, role_id="review")
    cfg = json.loads(path.read_text())
    worker_data = json.loads(original.workers[0]["path"].read_text())
    (path.parent / "reviewer.json").write_text(json.dumps({**worker_data, "agent_id": "reviewer"}))
    (path.parent / "reviewer.token").write_text(token)
    cfg["workers"].append({"config": "reviewer.json", "token_file": "reviewer.token"})
    cfg["max_parallel"] = 2
    path.write_text(json.dumps(cfg))
    with original._client(original.workers[0]) as first:
        first_client = first

    @contextmanager
    def clients(self, worker):
        yield (
            LocalClient(ledger, principal)
            if worker["config"].agent_id == "reviewer"
            else first_client
        )

    monkeypatch.setattr(Dispatcher, "_client", clients)
    dispatcher = Dispatcher(path, original.url)
    drain(
        dispatcher,
        lambda: all(
            ledger.ok("task_get", task_id=t)["status"] == "done" for t in ("t", "urgent-review")
        ),
    )
    claims = [
        e["subject"]
        for e in ledger.ok("events_list", limit=200)["events"]
        if e["kind"] == "task.claimed"
    ]
    assert claims == ["urgent-review", "t"]


def test_dispatcher_continues_after_connection_failure(dispatch_setup, monkeypatch):
    dispatcher, ledger, _ = dispatch_setup
    count = []
    tick = dispatcher.tick

    def fail_then_stop(pool):
        count.append(1)
        if len(count) == 1:
            raise CoordinationError("transport_error", "offline")
        tick(pool)
        dispatcher.stop.set()

    monkeypatch.setattr(dispatcher, "tick", fail_then_stop)
    dispatcher.run()
    assert len(count) == 2
    assert json.loads((dispatcher.directory / "health.json").read_text())["ok"]


def test_one_unavailable_agent_does_not_block_other_agents(dispatch_setup, monkeypatch):
    dispatcher, ledger, _ = dispatch_setup
    healthy = dispatcher.workers[0]
    broken = {**healthy, "config": healthy["config"].model_copy(update={"agent_id": "offline"})}
    dispatcher.workers.insert(0, broken)
    original = Dispatcher._client

    @contextmanager
    def clients(self, worker):
        if worker["config"].agent_id == "offline":
            raise CoordinationError("unauthorized", "Credential revoked")
        with original(self, worker) as client:
            yield client

    monkeypatch.setattr(Dispatcher, "_client", clients)
    dispatcher.run(once=True)
    assert ledger.ok("task_get", task_id="t")["status"] == "done"
    assert dispatcher.worker_health["default/offline"]["code"] == "unauthorized"


@pytest.mark.parametrize("change", ["duplicate", "missing-cwd", "bad-token", "invalid"])
def test_dispatcher_rejects_bad_configuration(dispatch_setup, change):
    _, _, path = dispatch_setup
    config = json.loads(path.read_text())
    if change == "duplicate":
        config["workers"] *= 2
    elif change == "missing-cwd":
        worker = path.parent / config["workers"][0]["config"]
        data = json.loads(worker.read_text())
        worker.write_text(json.dumps({**data, "cwd": "missing"}))
    elif change == "bad-token":
        (path.parent / config["workers"][0]["token_file"]).write_text("bad")
    else:
        config["max_parallel"] = 0
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        Dispatcher(path, "http://127.0.0.1:7433", threading.Event())


def test_poll_connections_reused_and_replaced_after_token_rotation(dispatch_setup, monkeypatch):
    dispatcher, _, _ = dispatch_setup
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.client_address[1], self.headers["Authorization"]))
            data = {"roles": [], "tasks": [], "next_cursor": None}
            raw = json.dumps({"ok": True, "data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    dispatcher.url = f"http://127.0.0.1:{server.server_port}"

    def factory(self, worker):
        return RelayFenceClient(self.url, self._token(worker["token_file"]), timeout=1)

    monkeypatch.setattr(Dispatcher, "_client", factory)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            dispatcher.tick(pool)
            dispatcher.tick(pool)
            assert len(requests) == 6
            assert len({port for port, _ in requests}) == 1
            old_port = requests[-1][0]
            dispatcher.workers[0]["token_file"].write_text("r" * 40)
            dispatcher.tick(pool)
            assert len(requests) == 9
            assert all(auth == "Bearer " + "r" * 40 for _, auth in requests[6:])
            assert requests[-1][0] != old_port
    finally:
        dispatcher.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_slow_polls_run_in_parallel_without_multiplying_waits(dispatch_setup, monkeypatch):
    dispatcher, _, _ = dispatch_setup
    first = dispatcher.workers[0]
    dispatcher.workers.extend(
        {**first, "config": first["config"].model_copy(update={"agent_id": f"slow-{i}"})}
        for i in range(3)
    )
    arrived = []

    async def probe(worker, busy):
        # Every probe must be in flight before any can finish.
        arrived.append(worker)
        async with asyncio.timeout(1):
            while len(arrived) < 4:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.15)
        if worker["config"].agent_id != "coder":
            raise CoordinationError("transport_error", "offline")

    monkeypatch.setattr(dispatcher, "_candidate", probe)
    with ThreadPoolExecutor(max_workers=1) as pool:
        dispatcher.tick(pool)
    assert dispatcher.worker_health["default/coder"]["ok"]
    assert not dispatcher.worker_health["default/slow-0"]["ok"]


def test_offline_backoff_recovers_without_restarting(dispatch_setup, monkeypatch):
    dispatcher, _, _ = dispatch_setup
    attempts = []

    async def probe(worker, busy):
        attempts.append(1)
        if len(attempts) < 3:
            raise CoordinationError("transport_error", "offline")

    monkeypatch.setattr(dispatcher, "_candidate", probe)
    with ThreadPoolExecutor(max_workers=1) as pool:
        dispatcher.tick(pool)
        dispatcher.tick(pool)
        assert len(attempts) == 1
        time.sleep(0.21)
        dispatcher.tick(pool)
        dispatcher.tick(pool)
        assert len(attempts) == 2
        assert dispatcher.worker_health["default/coder"]["retry_after_seconds"] == 0.4
        time.sleep(0.41)
        dispatcher.tick(pool)
        assert dispatcher.worker_health["default/coder"] == {"ok": True}


@pytest.mark.parametrize("reply", ["silent", "trickle"])
def test_entire_poll_deadline_cancels_slow_http_and_closes_cleanly(
    dispatch_setup, monkeypatch, reply
):
    dispatcher, _, _ = dispatch_setup
    received, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            received.set()
            if reply == "silent":
                release.wait(5)
                return
            self.send_response(200)
            self.send_header("Content-Length", "10000")
            self.end_headers()
            try:
                while not release.wait(0.02):
                    self.wfile.write(b" ")
                    self.wfile.flush()
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    dispatcher.url = f"http://127.0.0.1:{server.server_port}"
    dispatcher.config = dispatcher.config.model_copy(update={"request_timeout_seconds": 0.3})

    def factory(self, worker):
        return RelayFenceClient(self.url, self._token(worker["token_file"]), timeout=20)

    monkeypatch.setattr(Dispatcher, "_client", factory)
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            dispatcher.tick(pool)
        dispatcher.close()
        assert received.is_set()
        assert time.monotonic() - started < 2
        assert dispatcher.worker_health["default/coder"]["code"] == "poll_timeout"
        assert not dispatcher._poll_clients and dispatcher._poll_loop is None
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_health_write_failure_does_not_terminate_dispatch(dispatch_setup, monkeypatch):
    dispatcher, _, _ = dispatch_setup
    calls = []

    def unavailable(path, value):
        calls.append(1)
        if len(calls) == 2:
            dispatcher.stop.set()
        raise PermissionError("private path must not be logged")

    monkeypatch.setattr("relayfence.dispatcher.replace_private", unavailable)
    dispatcher.run()
    assert len(calls) == 2


def test_health_readers_never_observe_partial_json(tmp_path):
    from relayfence.worker import replace_private

    path = tmp_path / "health.json"
    replace_private(path, {"revision": 0, "payload": "x" * 1000})
    stop = threading.Event()
    errors, reads = [], []

    def reader():
        while not stop.is_set():
            try:
                value = json.loads(path.read_text())
                assert isinstance(value["revision"], int) and value["payload"] == "x" * 1000
                reads.append(1)
            except PermissionError:
                # Windows can transiently deny an open during replacement. A
                # reader retries; it must never parse an incomplete document.
                time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)
                break

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for revision in range(1, 50):
            try:
                replace_private(path, {"revision": revision, "payload": "x" * 1000})
            except PermissionError:
                # Windows may deny replacement while another process has the old
                # snapshot open. Preserve it and try again next polling cycle.
                pass
    finally:
        stop.set()
        thread.join(timeout=5)
    assert reads and not errors
    assert not list(tmp_path.glob("*.tmp"))
