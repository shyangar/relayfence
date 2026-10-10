import io
import json
import os
import tomllib
from pathlib import Path

import httpx
import pytest

from relayfence.cli import connector_config, init_directory, main, read_token
from relayfence.client import RelayFenceClient, validate_url
from relayfence.errors import CoordinationError


def test_init_is_repeatable_private_and_never_overwrites(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("RELAYFENCE_TOKEN", raising=False)
    directory = tmp_path / "work"
    assert main(["init", "--directory", str(directory)]) == 0
    token_file = directory / "access.token"
    token = read_token(token_file)
    assert len(token) >= 32
    assert token not in capsys.readouterr().out
    assert init_directory(directory) == directory.resolve()
    assert read_token(token_file) == token
    if os.name != "nt":
        assert token_file.stat().st_mode & 0o777 == 0o600
    backup = tmp_path / "backup.sqlite3"
    assert main(["backup", str(backup), "--directory", str(directory)]) == 0
    assert backup.is_file()
    assert main(["backup", str(backup), "--directory", str(directory)]) == 1
    assert (
        main(["backup", str(tmp_path / "new.sqlite3"), "--directory", str(tmp_path / "missing")])
        == 1
    )


def test_token_precedence_and_errors(tmp_path, monkeypatch):
    path = tmp_path / "missing"
    monkeypatch.delenv("RELAYFENCE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="missing"):
        read_token(path)
    monkeypatch.setenv("RELAYFENCE_TOKEN", "x" * 40)
    assert read_token(path) == "x" * 40
    monkeypatch.setenv("RELAYFENCE_TOKEN", "")
    with pytest.raises(ValueError, match="32"):
        read_token(path)
    assert main(["serve", "--directory", str(tmp_path)]) == 1
    assert main(["serve", "--port", "0"]) == 1


@pytest.mark.parametrize(
    "url",
    [
        "ftp://localhost",
        "file:///secret",
        "http://remote.example",
        "http://user:pass@localhost",
        "http://localhost?token=secret",
        "http://localhost#fragment",
        "invalid",
    ],
)
def test_client_rejects_unsafe_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_connector_configs_use_absolute_paths_and_no_token_values(tmp_path, capsys):
    path = tmp_path / "directory with spaces" / "access.token"
    codex = tomllib.loads(connector_config("codex", "http://127.0.0.1:7433", path))
    assert Path(codex["mcp_servers"]["relayfence"]["command"]).is_absolute()
    assert codex["mcp_servers"]["relayfence"]["args"][-1] == str(path.resolve())
    zcode = json.loads(connector_config("zcode", "http://127.0.0.1:7433", path))
    assert zcode["mcp"]["servers"]["relayfence"]["args"][-1] == str(path.resolve())
    dsh = connector_config("dsh", "http://127.0.0.1:7433", path)
    assert "transport: stdio" in dsh and "@deepseek-ai/dsh-mcp-client" in dsh
    assert main(["config", "codex", "--token-file", str(path)]) == 0
    assert "[mcp_servers.relayfence]" in capsys.readouterr().out


def test_cli_json_file_and_stdin_preserve_result_and_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RELAYFENCE_TOKEN", "t" * 40)
    payload = tmp_path / "request.json"
    payload.write_text('{"key":"spec"}', encoding="utf-8")

    def success(self, operation, body):
        assert operation == "item_get" and body == {"key": "spec"}
        return {"ok": True, "data": {"value": "世界"}, "replayed": False}, 200

    monkeypatch.setattr(RelayFenceClient, "call_raw", success)
    assert main(["call", "item_get", str(payload)]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["value"] == "世界"
    monkeypatch.setattr("sys.stdin", io.StringIO('{"key":"spec"}'))
    assert main(["call", "item_get", "-"]) == 0
    monkeypatch.setattr(
        RelayFenceClient,
        "call_raw",
        lambda *args: (CoordinationError("not_found", "Missing", status=404).as_dict(), 404),
    )
    assert main(["call", "item_get", str(payload)]) == 1
    payload.write_text("{", encoding="utf-8")
    assert main(["call", "item_get", str(payload)]) == 1


def test_client_handles_unknown_network_outcome_and_error_envelopes():
    with RelayFenceClient("http://localhost:7433", "x" * 40) as client:
        client.http.close()

        def disconnected(request):
            raise httpx.ReadTimeout("private error", request=request)

        client.http = httpx.Client(transport=httpx.MockTransport(disconnected), base_url=client.url)
        result, status = client.call_raw("items_list", {})
        assert status == 503 and result["error"]["code"] == "transport_error"
        assert "SAME request_id" in result["error"]["message"]
        client.http.close()
        client.http = httpx.Client(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=[])),
            base_url=client.url,
        )
        assert client.call_raw("items_list", {})[1] == 503
        client.http.close()
        client.http = httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    409, json=CoordinationError("lease_lost", "Stop").as_dict()
                )
            ),
            base_url=client.url,
        )
        with pytest.raises(CoordinationError, match="Stop"):
            client.call("task_get", task_id="x")
