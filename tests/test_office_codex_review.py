import copy
import io
import json
from types import SimpleNamespace

from starlette.testclient import TestClient

from relayfence import codex_adapter
from relayfence.server import create_app
from relayfence.worker import adapter_command, load_config

TOKEN = "office-review-synthetic-bearer-token-123456"


def office_group():
    return {
        "id": "engineering",
        "name": "Engineering",
        "software": "codex",
        "members": [
            {
                "id": "engineer",
                "name": "Engineer",
                "position": "backend",
                "relationship": "leader",
                "provider": "openai",
                "model": "test-model",
            }
        ],
    }


def test_full_access_config_survives_adapter_cli_to_subprocess(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "worker.json"
    config_path.write_text(
        json.dumps(
            {
                "agent_id": "engineer",
                "adapter": "codex",
                "executable": "C:/Agent software/codex.exe",
                "sandbox": "danger-full-access",
            }
        ),
        encoding="utf-8",
    )
    selection = {"model": "test-model", "parameters": {"reasoning_effort": "high"}}
    command = adapter_command(load_config(config_path), selection)
    envelope = {"task": {"task_id": "review", "outputs": []}, "selection": selection}
    monkeypatch.setattr("sys.argv", command[2:])
    monkeypatch.setattr(
        "sys.stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(envelope).encode()))
    )
    monkeypatch.setenv("RELAYFENCE_RUN_DIRECTORY", str(tmp_path))
    # Isolate this test from the developer's actual Codex configuration.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[windows]\nsandbox="unelevated"\n')
    monkeypatch.setattr("sys.platform", "win32")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == "C:/Agent software/codex.exe"
        assert argv.count("--sandbox") == 1
        assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
        assert argv[argv.index("--model") + 1] == selection["model"]
        # Skill discovery must stay enabled, so user config cannot be ignored here.
        assert "--ignore-user-config" not in argv
        assert 'approval_policy="never"' in argv
        assert 'model_reasoning_effort="high"' in argv
        assert 'windows.sandbox="unelevated"' in argv
        assert argv[-1] == "-"
        assert json.loads(kwargs["input"].decode().split("\n", 1)[1]) == envelope
        (tmp_path / "codex-result.json").write_text(
            json.dumps(
                {
                    "outcome": "completed",
                    "summary": "Done",
                    "details": "",
                    "files": [],
                    "checks": [],
                }
            )
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(codex_adapter.subprocess, "run", run)
    assert codex_adapter.main() == 0
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)["outputs"][0]["key"] == "tasks/review/result"


def test_public_office_assets_do_not_embed_bearer_or_project_data(ledger):
    secret = "private-project-content-8f0b28"
    ledger.ok(
        "state_put", project="private-review", key="private-key", expected_version=0, value=secret
    )
    group = office_group()
    group["name"] = secret
    ledger.ok("office_put", project="private-review", expected_revision=0, groups=[group])
    with TestClient(create_app(ledger.store, TOKEN), base_url="http://127.0.0.1") as client:
        for path, media_type in [
            ("/", "text/html"),
            ("/office.js", "text/javascript"),
            ("/office.css", "text/css"),
        ]:
            public = client.get(path)
            authenticated = client.get(path, headers={"Authorization": "Bearer " + TOKEN})
            assert public.status_code == authenticated.status_code == 200
            assert public.content == authenticated.content
            assert public.headers["content-type"].startswith(media_type)
            assert public.headers["x-content-type-options"] == "nosniff"
            for private in (TOKEN, secret, "private-review", "private-key"):
                assert private not in public.text
        assert client.post("/v1/office_get", json={"project": "private-review"}).status_code == 401


def test_public_office_assets_enforce_host_and_origin_before_authentication(ledger):
    with TestClient(create_app(ledger.store, TOKEN), base_url="http://127.0.0.1:8765") as client:
        for path in ("/", "/office.js", "/office.css"):
            for headers, status, code in [
                ({"Host": "attacker.example"}, 421, "invalid_host"),
                ({"Origin": "http://127.0.0.1:8766"}, 403, "invalid_origin"),
                ({"Origin": "https://127.0.0.1:8765"}, 403, "invalid_origin"),
                ({"Origin": "null"}, 403, "invalid_origin"),
            ]:
                response = client.get(path, headers=headers)
                assert response.status_code == status
                assert response.json()["error"]["code"] == code
                assert response.headers["cache-control"] == "no-store"
                assert TOKEN not in response.text
            assert client.get(path, headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200


def test_office_rejects_malformed_nested_configuration_without_mutation(ledger):
    group = office_group()
    ledger.ok("office_put", expected_revision=0, groups=[group])
    before = ledger.ok("office_get")
    for field, value in [
        ("avatar", True),
        ("avatar", 24),
        ("position", "administrator"),
        ("model", "--config=unsafe"),
        ("parameters", {"reasoning_effort": {"unexpected": "object"}}),
        ("fallback_models", [{"provider": "openai", "model": "test-model", "token": "secret"}]),
        ("fallback_models", [{"provider": "openai", "model": "test-model"}] * 5),
        ("unexpected", "secret"),
    ]:
        invalid = copy.deepcopy(group)
        invalid["members"][0][field] = value
        ledger.error("invalid_request", "office_put", expected_revision=1, groups=[invalid])
        assert ledger.ok("office_get") == before


def test_office_rejects_invalid_group_structure_and_generated_route_length(ledger):
    valid = office_group()
    missing_leader = copy.deepcopy(valid)
    missing_leader["members"][0]["relationship"] = "independent_session"
    two_leaders = copy.deepcopy(valid)
    two_leaders["members"].append({**valid["members"][0], "id": "second"})
    long_route = copy.deepcopy(valid)
    long_route["members"][0].update(
        id="a" * 128, fallback_models=[{"provider": "openai", "model": "test-model"}]
    )
    for groups in [
        [{**valid, "software": "unknown"}],
        [{**valid, "members": []}],
        [missing_leader],
        [two_leaders],
        [valid, {**valid, "members": [{**valid["members"][0], "id": "other"}]}],
        [long_route],
    ]:
        ledger.error("invalid_request", "office_put", expected_revision=0, groups=groups)
        assert ledger.ok("office_get")["revision"] == 0


def test_office_dispatch_rejects_malformed_briefings_without_creating_work(ledger):
    request = {
        "expected_revision": 0,
        "briefing_id": "review",
        "title": "Review",
        "description": "Review the code",
        "member_ids": ["engineer"],
    }
    for field, value in [
        ("expected_revision", True),
        ("title", ""),
        ("description", ""),
        ("member_ids", []),
        ("member_ids", ["engineer", "engineer"]),
        ("member_ids", "engineer"),
        # OfficeDispatch declares max_length=64; this must fail schema validation
        # before looking up members, even when no office is configured.
        ("briefing_id", "b" * 65),
    ]:
        ledger.error("invalid_request", "office_dispatch", **{**request, field: value})
        assert ledger.ok("tasks_list")["tasks"] == []
        assert ledger.ok("roles_list")["roles"] == []
