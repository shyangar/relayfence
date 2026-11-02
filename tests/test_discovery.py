"""Catalogue discovery tests use real local pipes, never vendor credentials/network."""

import json
import os
import queue
import sys
import time

import pytest

from relayfence import discovery


def test_unavailable_is_explicit_and_never_publishes_exception_secrets(tmp_path, monkeypatch):
    def absent(*_):
        raise FileNotFoundError("private-token /private/home")

    def timeout(*_):
        raise queue.Empty("private-token")

    def invalid(*_):
        raise ValueError("private-token")

    monkeypatch.setattr(discovery, "_discover_codex", absent)
    monkeypatch.setattr(discovery, "_discover_opencode", invalid)
    monkeypatch.setattr(discovery, "_discover_zcode", timeout)
    monkeypatch.setattr(discovery, "_discover_dsh", invalid)
    result = discovery.discover_installed(tmp_path)
    assert result.runners == {}
    assert [s["status"] for s in result.catalogue] == ["unavailable"] * 4
    assert len({s["message"] for s in result.catalogue}) == 3
    assert "private" not in json.dumps(result.catalogue)
    assert all(s["providers"] == [] for s in result.catalogue)


def test_private_runner_settings_are_separate_from_public_catalogue(tmp_path, monkeypatch):
    def available(*_):
        models = discovery._providers(
            [("fixture", "Fixture", discovery._model("exact-case", "Model", "fixture", ["high"]))]
        )
        return models, {"adapter": "codex", "executable": "/private/runtime"}

    for name in ("codex", "opencode", "zcode", "dsh"):
        monkeypatch.setattr(discovery, "_discover_" + name, available)
    result = discovery.discover_installed(tmp_path)
    assert all(s["status"] == "available" for s in result.catalogue)
    assert "/private" not in json.dumps(result.catalogue)
    assert result.runners["codex"]["executable"] == "/private/runtime"
    model = result.catalogue[0]["providers"][0]["models"][0]
    assert model["id"] == "exact-case"
    assert model["parameters"] == {"provider": ["fixture"], "reasoning_effort": ["high"]}


def test_snapshots_are_immutable_and_content_addressed(tmp_path):
    first = discovery._snapshot(tmp_path, "account", {"revision": 1})
    assert discovery._snapshot(tmp_path, "account", {"revision": 1}) == first
    second = discovery._snapshot(tmp_path, "account", {"revision": 2})
    assert first != second
    assert json.loads(first.read_bytes()) == {"revision": 1}
    assert json.loads(second.read_bytes()) == {"revision": 2}


def test_codex_uses_newest_installed_binary_not_hash_sort(tmp_path, monkeypatch):
    base = tmp_path / "OpenAI/Codex/bin"
    old, new = base / "ffff/codex.exe", base / "aaaa/codex.exe"
    for p in (old, new):
        p.parent.mkdir(parents=True)
        p.touch()
    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    os.utime(new, ns=(2_000_000_000, 2_000_000_000))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("RELAYFENCE_CODEX_EXECUTABLE", raising=False)
    assert discovery._codex_executable() == str(new)


def test_desktop_account_revision_is_recovered_without_other_log_fields(tmp_path):
    revision = "account:" + json.dumps(
        [
            "zcode-builtin:30:hash",
            [{"providerId": "account:fixture", "config": {"access": {"entitled": True}}}],
            {"account:fixture": {"availability": "available"}},
        ]
    )
    line = {"receivedRevision": revision, "token": "never-copy-this"}
    (tmp_path / "desktop.log").write_text("[desktop] " + json.dumps(line), encoding="utf-8")
    snapshot = discovery._account_snapshot(tmp_path, 30)
    assert snapshot["providers"]["account:fixture"]["access"]["entitled"] is True
    assert "never-copy-this" not in json.dumps(snapshot)
    assert discovery._account_snapshot(tmp_path, 31) is None


def test_protocol_handles_callbacks_and_reaps_process(tmp_path):
    script = tmp_path / "probe.py"
    script.write_text("""import json,sys
request=json.loads(sys.stdin.readline())
print(json.dumps({"id":999,"method":"client/callback"}),flush=True)
reply=json.loads(sys.stdin.readline())
assert reply["error"]["code"]==-32601
print(json.dumps({"id":request["id"],"result":{"data":["exact-model"]}}),flush=True)
sys.stdin.read()
""")
    with discovery._process([sys.executable, str(script)]) as process:
        protocol = discovery._Protocol(process, 3)
        assert protocol.rpc("model/list", {}) == {"data": ["exact-model"]}
    assert process.poll() is not None
    protocol.reader.join(timeout=1)
    assert not protocol.reader.is_alive()


def test_explicit_empty_environment_is_not_replaced_with_the_parent_env(monkeypatch):
    seen = {}

    def popen(command, **kwargs):
        seen["env"] = kwargs["env"]
        raise OSError("stop before starting")

    monkeypatch.setattr(discovery.subprocess, "Popen", popen)
    with pytest.raises(OSError):
        with discovery._process(["fixture"], {}):
            pass
    assert seen["env"] == {}


def test_protocol_timeout_reaps_blocked_runtime(tmp_path):
    script = tmp_path / "blocked.py"
    script.write_text("import time;time.sleep(60)")
    start = time.monotonic()
    with pytest.raises((TimeoutError, queue.Empty)):
        with discovery._process([sys.executable, str(script)]) as process:
            protocol = discovery._Protocol(process, 0.1)
            protocol.rpc("model/list", {})
    assert process.poll() is not None
    assert time.monotonic() - start < 5


def test_oversized_protocol_output_fails_bounded(tmp_path):
    script = tmp_path / "oversized.py"
    script.write_text("import sys,time;print('x'*2097153,flush=True);time.sleep(60)")
    with discovery._process([sys.executable, str(script)]) as process:
        with pytest.raises(ValueError, match="closed"):
            discovery._Protocol(process, 3).rpc("model/list", {})


def test_catalogue_registry_bound_is_explicit():
    rows = [("p", "P", discovery._model(f"m{i}", None, "p")) for i in range(65)]
    with pytest.raises(ValueError, match="limit"):
        discovery._providers(rows)


@pytest.fixture
def runtime_probe(monkeypatch):
    from contextlib import contextmanager

    calls = []
    responses = {}

    @contextmanager
    def process(command, env=None):
        calls.append((command, env))
        yield object()

    class Protocol:
        def __init__(self, process, timeout):
            self.messages = queue.Queue()
            if "dsh" in responses:
                self.messages.put(responses["dsh"])

        def send(self, value):
            calls.append(value)

        def rpc(self, method, params):
            calls.append((method, params))
            value = responses[method]
            return value.pop(0) if isinstance(value, list) else value

    monkeypatch.setattr(discovery, "_process", process)
    monkeypatch.setattr(discovery, "_Protocol", Protocol)
    return calls, responses


def test_codex_live_catalog_pagination_keeps_exact_supported_efforts(
    tmp_path, monkeypatch, runtime_probe
):
    calls, responses = runtime_probe
    monkeypatch.setattr(discovery, "_codex_executable", lambda: "/local/codex")
    responses.update(
        {
            "initialize": {},
            "model/list": [
                {
                    "data": [
                        {
                            "id": "model-one",
                            "model": "exact-one",
                            "displayName": "One",
                            "supportedReasoningEfforts": [{"reasoningEffort": "ultra"}],
                        }
                    ],
                    "nextCursor": "next",
                },
                {"data": [{"id": "exact-two"}], "nextCursor": None},
            ],
        }
    )
    providers, runner = discovery._discover_codex(tmp_path, 1)
    assert runner["executable"] == "/local/codex"
    assert providers[0]["models"][0]["parameters"] == {"reasoning_effort": ["ultra"]}
    assert providers[0]["models"][1]["parameters"] == {}
    assert ("model/list", {"includeHidden": False, "limit": 100, "cursor": "next"}) in calls


def test_zcode_uses_active_runtime_and_account_snapshot(tmp_path, monkeypatch, runtime_probe):
    calls, responses = runtime_probe
    install = tmp_path / "install"
    script = install / "resources/glm/zcode.cjs"
    script.parent.mkdir(parents=True)
    script.touch()
    home = tmp_path / "home"
    builtin = home / "v2/runtime/provider/platform/version/endpoint/zcode-builtin.json"
    builtin.parent.mkdir(parents=True)
    builtin.write_text('{"revision":30}')
    monkeypatch.setenv("ZCODE_HOME", str(home))
    monkeypatch.setenv("RELAYFENCE_TOKEN", "never-to-runtime")
    monkeypatch.setattr(discovery, "_install_roots", lambda _: [install])
    monkeypatch.setattr(discovery.shutil, "which", lambda _: "/local/node")
    monkeypatch.setattr(discovery, "_account_snapshot", lambda *_: {"revision": "fixture"})
    responses.update(
        {
            "provider/updateAccountConfig": {},
            "session/create": {
                "settings": {
                    "model": {
                        "available": [
                            {
                                "ref": {"providerId": "account:fixture", "modelId": "Exact-Model"},
                                "reasoning": {"levels": [{"value": "max"}]},
                            }
                        ]
                    }
                }
            },
        }
    )
    providers, runner = discovery._discover_zcode(tmp_path / "private", 1)
    assert providers[0]["models"][0]["reasoning_efforts"] == ["max"]
    assert calls[0][1]["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] == str(builtin)
    assert "RELAYFENCE_TOKEN" not in calls[0][1]
    assert "--account-snapshot" in runner["command"]
    assert "never-to-runtime" not in json.dumps(runner)


def test_dsh_reports_installed_routes_and_their_auth_state(tmp_path, monkeypatch, runtime_probe):
    calls, responses = runtime_probe
    install = tmp_path / "install"
    (install / "resources").mkdir(parents=True)
    (install / "resources/app.asar").touch()
    (install / "DeepSeek Harness.exe").touch()
    monkeypatch.setattr(discovery, "_install_roots", lambda _: [install])
    monkeypatch.setenv("RELAYFENCE_TOKEN", "never-to-runtime")
    monkeypatch.delenv("RELAYFENCE_TEST_MISSING_KEY", raising=False)
    responses["dsh"] = {
        "rows": [
            {
                "id": "account",
                "label": "Account",
                "authKind": "account",
                "credentialEnv": None,
                "models": [{"id": "exact-model", "label": "Exact"}],
            },
            {
                "id": "missing-key",
                "label": "Unavailable",
                "authKind": "api_key",
                "credentialEnv": "RELAYFENCE_TEST_MISSING_KEY",
                "models": [{"id": "do-not-offer", "label": "Unavailable"}],
            },
        ],
        "patches": [{"id": "llm-pi-ai", "config": {"providers": {}}}],
    }
    providers, runner = discovery._discover_dsh(tmp_path / "private", 1)
    assert [p["id"] for p in providers] == ["account", "missing-key"]
    assert providers[0]["auth"]["kind"] == "account"
    assert providers[0]["auth"]["authenticated"] is False
    assert providers[1]["auth"]["credential_env"] == "RELAYFENCE_TEST_MISSING_KEY"
    assert providers[1]["auth"]["authenticated"] is False
    assert "--patch" in runner["command"]
    assert calls[0][1]["ELECTRON_RUN_AS_NODE"] == "1"
    assert "RELAYFENCE_TOKEN" not in calls[0][1]


def test_opencode_reads_only_plain_model_references_and_owns_the_probe(tmp_path, monkeypatch):
    import io
    from contextlib import contextmanager

    calls = []

    class Process:
        stdout = io.BytesIO(
            b"\x1b[32misoft/deepseek-flash\x1b[0m\n"
            b"fixture/other-model\n"
            b"diagnostic line\n"
            b"isoft/deepseek-flash\n"
        )

        @staticmethod
        def wait(timeout=None):
            return 0

    @contextmanager
    def process(command, env=None):
        calls.append((command, env))
        yield Process()

    monkeypatch.setattr(discovery, "_process", process)
    monkeypatch.setattr("relayfence.opencode_adapter.find_executable", lambda: "/local/opencode")
    monkeypatch.setenv("RELAYFENCE_TOKEN", "never-to-runtime")

    providers, runner = discovery._discover_opencode(tmp_path / "private", 1)

    assert calls[0][0] == ["/local/opencode", "models"]
    assert "RELAYFENCE_TOKEN" not in calls[0][1]
    assert [p["id"] for p in providers] == ["isoft", "fixture"]
    assert providers[0]["models"][0]["id"] == "isoft/deepseek-flash"
    assert providers[0]["models"][0]["parameters"] == {"provider": ["isoft"]}
    command = runner["command"]
    assert "relayfence.opencode_adapter" in command
    assert command[command.index("--permission-mode") + 1] == "danger-full-access"
    assert "{model}" in command and "{parameters_json}" in command
    assert "never-to-runtime" not in json.dumps(runner)


def test_opencode_auth_sources_are_allowlisted(monkeypatch):
    import io

    class Process:
        stdout = io.BytesIO(
            b"isoft  ISOFT_API_KEY  <img src=x onerror=alert(1)>\nopenai  OPENAI_API_KEY  OAuth\n"
        )

        @staticmethod
        def wait(timeout=None):
            return 0

    class Context:
        def __enter__(self):
            return Process()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(discovery, "_process", lambda *args, **kwargs: Context())

    result = discovery._opencode_auth("/local/opencode", {}, 1)

    assert result["isoft"]["source"] == "configured"
    assert result["openai"]["source"] == "oauth"


def test_opencode_enriches_the_reference_list_with_free_and_paid_prices(tmp_path, monkeypatch):
    import io
    from contextlib import contextmanager

    calls = []
    metadata = {
        "data": [
            {
                "providerID": "opencode",
                "modelID": "beam-free",
                "name": "Beam Free",
                "cost": {
                    "input": 0,
                    "output": 0,
                    "cache": {"read": 0, "write": 0},
                },
            },
            {
                "providerID": "opencode",
                "modelID": "paid",
                "name": "Paid Model",
                "cost": {"input": 0.1, "output": 0.5},
            },
        ]
    }

    class Process:
        def __init__(self, stdout):
            self.stdout = io.BytesIO(stdout)

        @staticmethod
        def wait(timeout=None):
            return 0

    @contextmanager
    def process(command, env=None):
        calls.append(command)
        if command[1:] == ["models"]:
            yield Process(b"opencode/beam-free\nopencode/paid\n")
        else:
            yield Process(json.dumps(metadata).encode())

    monkeypatch.setattr(discovery, "_process", process)
    monkeypatch.setattr("relayfence.opencode_adapter.find_executable", lambda: "/local/opencode")

    providers, _runner = discovery._discover_opencode(tmp_path / "private", 2)

    assert calls[0] == ["/local/opencode", "models"]
    assert calls[1] == ["/local/opencode", "auth", "list"]
    assert calls[2] == ["/local/opencode", "api", "--standalone", "GET", "/api/model"]
    models = {model["id"]: model for model in providers[0]["models"]}
    assert models["opencode/beam-free"]["label"] == "Beam Free"
    assert models["opencode/beam-free"]["free"] is True
    assert models["opencode/beam-free"]["pricing"] == {"input": 0, "output": 0}
    assert models["opencode/paid"]["free"] is False
    assert models["opencode/paid"]["pricing"] == {"input": 0.1, "output": 0.5}


def test_opencode_bounds_each_provider_to_the_agent_model_limit(tmp_path, monkeypatch):
    import io
    from contextlib import contextmanager

    class Process:
        stdout = io.BytesIO(b"".join(f"gateway/model-{i}\n".encode() for i in range(70)))

        @staticmethod
        def wait(timeout=None):
            return 0

    @contextmanager
    def process(command, env=None):
        yield Process()

    monkeypatch.setattr(discovery, "_process", process)
    monkeypatch.setattr("relayfence.opencode_adapter.find_executable", lambda: "/local/opencode")
    providers, _runner = discovery._discover_opencode(tmp_path / "private", 1)

    assert len(providers) == 1
    assert len(providers[0]["models"]) == discovery.PROVIDER_MODEL_LIMIT
    assert providers[0]["models"][-1]["id"] == "gateway/model-63"


def test_opencode_requires_a_real_executable(tmp_path, monkeypatch):
    monkeypatch.setattr("relayfence.opencode_adapter.find_executable", lambda: None)
    with pytest.raises(FileNotFoundError):
        discovery._discover_opencode(tmp_path, 1)
    monkeypatch.setattr("relayfence.opencode_adapter.find_executable", lambda: "opencode.cmd")
    with pytest.raises(ValueError, match="batch"):
        discovery._discover_opencode(tmp_path, 1)


def test_discovery_rejects_invalid_timeout_and_large_files(tmp_path):
    with pytest.raises(ValueError, match="timeout"):
        discovery.discover_installed(tmp_path, timeout_seconds=0)
    path = tmp_path / "large.json"
    path.write_bytes(b" " * (discovery.LIMIT + 1))
    with pytest.raises(ValueError, match="limit"):
        discovery._read_json(path)
    with pytest.raises(ValueError, match="model"):
        discovery._model("", "", "fixture")
    with pytest.raises(ValueError, match="reasoning"):
        discovery._model("m", "M", "fixture", [str(n) for n in range(33)])


def test_windows_registry_roots_respect_installed_paths_and_tolerate_missing_keys(
    tmp_path, monkeypatch
):
    from contextlib import contextmanager
    from types import SimpleNamespace

    records = {
        "irrelevant": {"DisplayName": "Other app"},
        "zcode": {
            "DisplayName": "ZCode 9",
            "DisplayIcon": str(tmp_path / "zcode/uninstall.ico") + ",0",
        },
        "dsh": {"DisplayName": "DeepSeek Harness", "InstallLocation": str(tmp_path / "dsh")},
    }

    @contextmanager
    def open_key(parent, key):
        if parent == "machine" or "WOW6432" in key or key == "broken":
            raise OSError("registry access denied")
        yield key if key in records else "parent"

    def query(key, name):
        try:
            return records[key][name], 1
        except KeyError as exc:
            raise OSError("missing value") from exc

    fake = SimpleNamespace(
        HKEY_CURRENT_USER="user",
        HKEY_LOCAL_MACHINE="machine",
        OpenKey=open_key,
        QueryInfoKey=lambda _: (4,),
        EnumKey=lambda _, i: ["irrelevant", "broken", "zcode", "dsh"][i],
        QueryValueEx=query,
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(
        discovery,
        "os",
        SimpleNamespace(
            name="nt",
            environ={
                "LOCALAPPDATA": str(tmp_path / "local"),
                "ProgramFiles": str(tmp_path / "programs"),
                "RELAYFENCE_ZCODE_HOME": str(tmp_path / "override"),
            },
        ),
    )
    monkeypatch.setattr(discovery.shutil, "which", lambda _: str(tmp_path / "path/cli.exe"))
    zcode = discovery._install_roots("zcode")
    assert tmp_path / "override" in zcode
    assert tmp_path / "zcode" in zcode
    assert tmp_path / "path" in zcode
    assert len(zcode) == len(set(zcode))
    assert tmp_path / "dsh" in discovery._install_roots("dsh")


def test_codex_override_path_fallback_and_batch_shim_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAYFENCE_CODEX_EXECUTABLE", "/explicit/codex")
    assert discovery._codex_executable() == "/explicit/codex"
    monkeypatch.delenv("RELAYFENCE_CODEX_EXECUTABLE")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(discovery.shutil, "which", lambda _: "/path/codex")
    assert discovery._codex_executable() == "/path/codex"
    monkeypatch.setattr(discovery.shutil, "which", lambda _: "/path/codex.cmd")
    assert discovery._codex_executable() is None


def test_protocol_rejects_error_response_and_expired_deadline():
    import io
    from types import SimpleNamespace

    process = SimpleNamespace(
        stdin=io.BytesIO(),
        stdout=io.BytesIO(b'{"method":"notification"}\n{"id":1,"error":{"message":"private"}}\n'),
    )
    protocol = discovery._Protocol(process, 1)
    with pytest.raises(ValueError, match="rejected"):
        protocol.rpc("model/list", {})
    protocol.deadline = 0
    with pytest.raises(TimeoutError):
        protocol.rpc("model/list", {})


@pytest.mark.parametrize("data", [b"not-json\n", b"[]\n", b'{"id":1}\n' * 300])
def test_protocol_malformed_or_flooded_stream_stops_reader(data):
    import io
    from types import SimpleNamespace

    process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(data))
    protocol = discovery._Protocol(process, 1)
    protocol.reader.join(timeout=1)
    assert not protocol.reader.is_alive()
    assert protocol.messages.qsize() <= 256


def test_missing_runtime_prerequisites_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "_codex_executable", lambda: None)
    with pytest.raises(FileNotFoundError):
        discovery._discover_codex(tmp_path, 1)
    monkeypatch.setattr(discovery, "_install_roots", lambda _: [tmp_path])
    with pytest.raises(FileNotFoundError):
        discovery._discover_zcode(tmp_path, 1)
    with pytest.raises(FileNotFoundError):
        discovery._discover_dsh(tmp_path, 1)
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources/app.asar").touch()
    with pytest.raises(FileNotFoundError, match="executable"):
        discovery._discover_dsh(tmp_path, 1)


def test_corrupt_and_unrelated_desktop_logs_do_not_become_account_config(tmp_path):
    (tmp_path / "desktop.log").write_text(
        'unrelated private secret\n{"receivedRevision":"account:not-json"}\n', encoding="utf-8"
    )
    assert discovery._account_snapshot(tmp_path, 30) is None


def test_posix_probe_group_cleanup_reaps_even_when_group_already_gone(monkeypatch):
    import io
    from types import SimpleNamespace

    actions = []
    process = SimpleNamespace(
        pid=123,
        stdin=io.BytesIO(),
        stdout=io.BytesIO(),
        poll=lambda: None,
        kill=lambda: actions.append("kill"),
        wait=lambda **_: actions.append("wait"),
    )

    def gone(*_):
        actions.append("group")
        raise ProcessLookupError

    monkeypatch.setattr(discovery, "os", SimpleNamespace(name="posix", environ={}, killpg=gone))
    monkeypatch.setattr(discovery, "signal", SimpleNamespace(SIGKILL=9))
    monkeypatch.setattr(discovery.subprocess, "Popen", lambda *_, **__: process)
    with discovery._process(["fixture"]):
        pass
    assert actions == ["group", "kill", "wait"]
    assert process.stdin.closed and process.stdout.closed
