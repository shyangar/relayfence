import sys

import pytest

from relayfence import cli, server


@pytest.mark.parametrize("no_browser", [True, False])
def test_first_serve_initializes_and_opens_without_exposing_admin_token(
    tmp_path, monkeypatch, capsys, no_browser
):
    import threading
    import webbrowser

    import uvicorn

    directory = tmp_path / "new-local-state"
    args = ["relayfence", "serve", "--directory", str(directory)]
    if no_browser:
        args.append("--no-browser")
    monkeypatch.setattr(sys, "argv", args)
    opened = []
    monkeypatch.setattr(webbrowser, "open", opened.append)

    class ImmediateTimer:
        def __init__(self, delay, callback):
            assert delay == 1
            self.callback = callback

        def start(self):
            self.callback()

    monkeypatch.setattr(threading, "Timer", ImmediateTimer)
    captured = {}

    def create(store, token, **kwargs):
        captured.update(kwargs, token=token)
        kwargs["on_ready"]()
        return "local-app"

    def run(app, **kwargs):
        assert app == "local-app"
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["access_log"] is False

    monkeypatch.setattr(server, "create_app", create)
    monkeypatch.setattr(uvicorn, "run", run)
    assert cli.main() == 0
    token = (directory / "access.token").read_text().strip()
    assert captured["token"] == token
    assert (directory / "ledger.sqlite3").is_file()
    output = capsys.readouterr().out
    assert token not in output
    assert "http://127.0.0.1:7433/" in output
    assert "#pair=" not in output
    assert "sessions" not in captured
    assert captured["local_ui"] is True
    assert len(opened) == (0 if no_browser else 1)
    if not no_browser:
        assert opened == [line for line in output.splitlines() if line.startswith("http://")]
    assert captured["workspace_manager"].permission_mode == "workspace-write"
