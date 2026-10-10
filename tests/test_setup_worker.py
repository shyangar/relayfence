import json
import threading

import pytest
from test_worker import LocalClient

from relayfence.cli import main
from relayfence.dispatcher import Dispatcher
from relayfence.identity import ADMIN
from relayfence.setup_worker import setup_codex


def test_setup_creates_credential_scoped_worker_and_preserves_existing_files(ledger, tmp_path):
    directory = tmp_path / "private"
    result = setup_codex(
        LocalClient(ledger, ADMIN),
        directory,
        "default",
        "native",
        "available-model",
        tmp_path,
        "codex",
        "read-only",
        "high",
    )
    config = json.loads((directory / "worker.json").read_text())
    assert config["adapter"] == "codex" and config["sandbox"] == "read-only"
    assert "token" not in json.dumps(result)
    dispatcher = Dispatcher(result["dispatcher_config"], "http://127.0.0.1:7433")
    assert dispatcher.workers[0]["config"].agent_id == "native"
    role = ledger.ok("role_get", role_id="native")
    assert role["selection"] == {
        "model": "available-model",
        "parameters": {"reasoning_effort": "high"},
    }
    with pytest.raises(FileExistsError):
        setup_codex(
            LocalClient(ledger, ADMIN),
            directory,
            "default",
            "another",
            "model",
            tmp_path,
            "codex",
            "read-only",
            None,
        )


def test_setup_rejects_missing_workspace_before_registering(ledger, tmp_path):
    with pytest.raises(ValueError, match="Workspace"):
        setup_codex(
            LocalClient(ledger, ADMIN),
            tmp_path / "config",
            "default",
            "a",
            "model",
            tmp_path / "missing",
            "codex",
            "read-only",
            None,
        )
    assert not (tmp_path / "config").exists()


def test_cli_dispatch_and_setup_paths(monkeypatch, tmp_path, capsys):
    seen = []

    class Dispatch:
        def __init__(self, path, url):
            seen.append((path, url))

        def run(self, once=False):
            seen.append(once)

    monkeypatch.setattr("relayfence.dispatcher.Dispatcher", Dispatch)
    assert main(["dispatch", "--config", "dispatcher.json", "--once"]) == 0
    assert seen[-1] is True
    monkeypatch.setenv("RELAYFENCE_TOKEN", "a" * 40)
    monkeypatch.setattr("relayfence.setup_worker.setup_codex", lambda *args: {"agent_id": "native"})
    assert (
        main(
            [
                "setup-codex",
                "--directory",
                str(tmp_path / "config"),
                "--workspace",
                str(tmp_path),
                "--model",
                "available-model",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["agent_id"] == "native"


def test_serve_starts_and_stops_integrated_dispatcher(monkeypatch, tmp_path):
    from relayfence.cli import init_directory

    init_directory(tmp_path)
    started, stopped = threading.Event(), threading.Event()

    class Dispatch:
        def __init__(self, path, url):
            self.stop = threading.Event()
            assert url == "http://127.0.0.1:7433"

        def run(self):
            started.set()
            self.stop.wait(5)
            stopped.set()

    def serve(*args, **kwargs):
        assert started.wait(2)

    monkeypatch.setattr("relayfence.dispatcher.Dispatcher", Dispatch)
    monkeypatch.setattr("uvicorn.run", serve)
    assert main(["serve", "--directory", str(tmp_path), "--dispatcher", "dispatcher.json"]) == 0
    assert stopped.is_set()
