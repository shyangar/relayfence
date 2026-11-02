import copy
import json
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from relayfence.local_workspace import LocalWorkspaceManager


def discovered(_):
    return SimpleNamespace(
        catalogue=[
            {
                "id": "codex",
                "label": "Codex",
                "status": "available",
                "providers": [
                    {
                        "id": "openai",
                        "label": "OpenAI",
                        "models": [
                            {
                                "id": "model-one",
                                "parameters": {"reasoning_effort": ["medium", "high"]},
                            },
                            {"id": "model-two", "parameters": {"reasoning_effort": ["high"]}},
                        ],
                    }
                ],
            }
        ],
        runners={"codex": {"adapter": "codex", "executable": "codex"}},
    )


def test_project_list_persists_multiple_directories_and_does_not_start_work(
    manager, ledger, tmp_path
):
    first, second = tmp_path / "网站", tmp_path / "数据工具"
    first.mkdir()
    second.mkdir()
    manager.add_project(str(first))
    manager.add_project(str(second))
    manager.add_project(str(first / "."))
    assert len(manager.projects()["projects"]) == 2
    assert not manager.status()["runner_running"]
    restored = LocalWorkspaceManager(ledger.service, "http://127.0.0.1:7433", manager.directory)
    assert {p["path"] for p in restored.projects()["projects"]} == {str(first), str(second)}
    selected = manager.connect(str(first))
    assert not (manager.directory / "projects.json").exists()
    assert next(p for p in manager.projects()["projects"] if p["active"])["path"] == str(first)
    manager.add_project(str(second))
    assert manager.projects()["active_project"] == selected["project"]
    other = manager.connect(str(second))
    assert other["project"] != selected["project"]
    assert len(manager.projects()["projects"]) == 2
    assert sum(p["active"] for p in manager.projects()["projects"]) == 1
    first.rmdir()
    assert (
        next(p for p in manager.projects()["projects"] if p["path"] == str(first))["exists"]
        is False
    )


def test_project_list_rejects_invalid_paths(manager, tmp_path):
    with pytest.raises(ValueError):
        manager.add_project("relative")
    file = tmp_path / "file.txt"
    file.write_text("x")
    with pytest.raises(ValueError):
        manager.add_project(str(file))
    assert manager.projects()["projects"] == []


def test_runtime_reports_background_projects_and_shared_capacity(manager, tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    manager.add_project(str(first))
    first_id = manager._identity(first)
    assert manager.runtime_status(first_id)["health"]["status"] == "disconnected"
    manager.connect(str(first))
    first_dispatcher = manager._dispatcher
    second_id = manager.connect(str(second))["project"]
    assert manager.runtime_status(first_id)["health"]["status"] == "healthy"
    assert first_dispatcher.options["capacity"] is manager._dispatcher.options["capacity"]
    assert manager.runtime_status(second_id)["capacity"]["limit"] == 2
    with pytest.raises(ValueError, match="项目不存在"):
        manager.runtime_status("unknown")
    manager._pending_discovery = ([], {})
    assert manager.runtime_status(second_id)["health"]["status"] == "refresh_pending"
    manager._pending_discovery = None
    manager._dispatcher.stop.set()
    manager._thread.join(2)
    assert manager.runtime_status(second_id)["health"]["status"] == "stopped"


class FakeDispatcher:
    def __init__(self, path, url, env_provider=None, **options):
        self.stop = threading.Event()
        self.config = json.loads(path.read_text())
        self.workers = [{"future": None} for _ in self.config["workers"]]
        self.ticks = 0
        self.options = options

    def health(self):
        return {"status": "healthy"}

    def tick(self, pool):
        self.ticks += 1

    def run(self):
        self.stop.wait(10)


@pytest.fixture
def manager(ledger, tmp_path, tmp_path_factory, monkeypatch):
    monkeypatch.setattr("relayfence.local_workspace.Dispatcher", FakeDispatcher)
    # Keep private state outside the workspace these tests connect (tmp_path).
    instance = LocalWorkspaceManager(
        ledger.service,
        "http://127.0.0.1:7433",
        tmp_path_factory.mktemp("state") / "private",
        discover=discovered,
    )
    instance.start_discovery()
    instance._discovery_thread.join(2)
    yield instance
    instance.close()


def test_agent_inventory_credentials_and_definition_filter(ledger, tmp_path, monkeypatch):
    monkeypatch.setattr("relayfence.local_workspace.Dispatcher", FakeDispatcher)

    def discover(_):
        return SimpleNamespace(
            catalogue=[
                {
                    "id": "dsh",
                    "label": "DSH",
                    "installed": True,
                    "status": "available",
                    "message": "",
                    "providers": [
                        {
                            "id": "isoft",
                            "label": "iSoft",
                            "models": [{"id": "deepseek-flash", "parameters": {}}],
                            "auth": {
                                "kind": "api_key",
                                "authenticated": False,
                                "credential_env": "ISOFT_API_KEY",
                                "source": None,
                                "message": "缺少 ISOFT_API_KEY，请配置密钥",
                            },
                        }
                    ],
                }
            ],
            runners={
                "dsh": {
                    "adapter": "command",
                    "command": ["dsh", "{model}"],
                }
            },
        )

    manager = LocalWorkspaceManager(
        ledger.service, "http://127.0.0.1:7433", tmp_path / "private", discover=discover
    )
    manager.start_discovery()
    manager._discovery_thread.join(2)
    try:
        assert manager.agent_inventory()["agents"][0]["auth"]["state"] == "missing_key"
        assert manager._definitions("p", tmp_path, manager._catalogue, manager._runners) == []
        with pytest.raises(ValueError):
            manager.save_agent_credential(
                {
                    "software": "dsh",
                    "provider": "unknown",
                    "name": "ISOFT_API_KEY",
                    "value": "secret",
                }
            )

        saved = manager.save_agent_credential(
            {
                "software": "dsh",
                "provider": "isoft",
                "name": "ISOFT_API_KEY",
                "value": "sk-local-secret-9876",
            }
        )
        assert saved["masked"] == "••••••••9876"
        manager._discovery_thread.join(2)
        inventory = manager.agent_inventory()
        assert inventory["agents"][0]["auth"]["state"] == "ready"
        assert inventory["agents"][0]["providers"][0]["auth"]["stored"] is True
        definitions = manager._definitions("p", tmp_path, manager._catalogue, manager._runners)
        assert len(definitions) == 1
        assert definitions[0][1].runtime == "dsh"
        assert definitions[0][1].provider == "isoft"
        assert manager._agent_environment(SimpleNamespace(runtime="dsh", provider="isoft")) == {
            "ISOFT_API_KEY": "sk-local-secret-9876"
        }

        assert manager.delete_agent_credential(
            {"software": "dsh", "provider": "isoft", "name": "ISOFT_API_KEY"}
        ) == {"deleted": True}
        manager._discovery_thread.join(2)
        assert manager.agent_inventory()["agents"][0]["auth"]["state"] == "missing_key"
        assert manager._agent_environment(SimpleNamespace(runtime="dsh", provider="isoft")) == {}
    finally:
        manager.close()


def test_connect_automatically_registers_scoped_runner_and_exact_models(manager, ledger, tmp_path):
    workspace = tmp_path / "chosen-project"
    workspace.mkdir()
    status = manager.connect(str(workspace))
    binding = status["bindings"][0]
    agent = ledger.ok("agent_get", project=status["project"], agent_id=binding["agent_id"])
    assert [m["id"] for m in agent["models"]] == ["model-one", "model-two"]
    assert agent["models"][0]["parameters"]["reasoning_effort"] == ["medium", "high"]
    assert status["runner_running"] and not status["discovering"]
    assert status["catalogue"][0]["providers"][0]["agent_id"] == agent["agent_id"]
    private = manager.directory / status["project"]
    config = json.loads((private / (agent["agent_id"] + ".json")).read_text())
    assert config["cwd"] == str(workspace.resolve())
    assert config["sandbox"] == "workspace-write"
    dispatcher = json.loads((private / "dispatcher.json").read_text())
    assert dispatcher["max_parallel"] >= 2
    assert len(dispatcher["workers"]) == dispatcher["max_parallel"]
    token = (private / (agent["agent_id"] + ".token")).read_text().strip()
    assert ledger.store.credential_valid(ledger.store.digest(token))
    assert token not in json.dumps(status)
    assert list(workspace.iterdir()) == []


def test_reconnect_is_idempotent_and_restart_reuses_credential(manager, ledger, tmp_path):
    status = manager.connect(str(tmp_path))
    thread = manager._thread
    agent_id = status["bindings"][0]["agent_id"]
    token_path = manager.directory / status["project"] / (agent_id + ".token")
    original = token_path.read_bytes()
    revision = ledger.ok("agent_get", project=status["project"], agent_id=agent_id)["revision"]
    assert manager.connect(str(tmp_path))["project"] == status["project"]
    assert manager._thread is thread
    manager._stop_dispatcher()
    manager.connect(str(tmp_path))
    assert token_path.read_bytes() == original
    assert (
        ledger.ok("agent_get", project=status["project"], agent_id=agent_id)["revision"] == revision
    )


def test_switch_keeps_active_project_running_and_close_stops_all(manager, tmp_path):
    first = manager.connect(str(tmp_path))
    first_dispatcher, first_thread = manager._dispatcher, manager._thread
    active = Future()
    first_dispatcher.workers[0]["future"] = active
    second = tmp_path / "other"
    second.mkdir()
    selected = manager.connect(str(second))
    second_thread = manager._thread
    assert selected["project"] != first["project"]
    assert first_thread.is_alive() and not first_dispatcher.stop.is_set()
    assert manager.dispatcher_for(first["project"]) is first_dispatcher
    assert not active.done()
    assert manager.connect(str(tmp_path))["project"] == first["project"]
    assert manager._thread is first_thread
    manager.close()
    assert not first_thread.is_alive() and not second_thread.is_alive()
    with pytest.raises(ValueError, match="closed"):
        manager.connect(str(second))


@pytest.mark.parametrize("path", [".", "", "missing", "relative/project"])
def test_reject_relative_or_missing_paths_before_setup(manager, path):
    with pytest.raises(ValueError, match="absolute project directory"):
        manager.connect(path)
    assert manager.status()["project"] is None


def test_project_id_follows_resolved_path_and_changes_for_other_directory(manager, tmp_path):
    first = manager.connect(str(tmp_path))
    directory = tmp_path / "other"
    directory.mkdir()
    second = manager.connect(str(directory))
    assert first["project"] != second["project"]
    assert manager.connect(str(tmp_path))["project"] == first["project"]


def test_refresh_does_not_rotate_live_credentials(manager, ledger, tmp_path):
    status = manager.connect(str(tmp_path))
    agent_id = status["bindings"][0]["agent_id"]
    before = ledger.ok("agent_get", project=status["project"], agent_id=agent_id)
    thread = manager._thread
    manager.refresh()
    manager._discovery_thread.join(2)
    assert ledger.ok("agent_get", project=status["project"], agent_id=agent_id) == before
    assert manager._thread is thread


def test_connect_exposes_project_skills_to_the_shared_skill_list(manager, ledger, tmp_path):
    skill = tmp_path / ".agents" / "skills" / "project-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: project-skill\ndescription: Project skill\n---\n", encoding="utf-8"
    )

    status = manager.connect(str(tmp_path))

    catalogue = ledger.ok("skill_list", project=status["project"])["skills"]
    dsh = next(entry for entry in catalogue if entry["software"] == "dsh")
    assert [entry["name"] for entry in dsh["skills"]] == ["project-skill"]


def test_discovery_failure_is_sanitized(ledger, tmp_path, tmp_path_factory):
    def fail(_):
        raise RuntimeError("private-secret")

    instance = LocalWorkspaceManager(
        ledger.service,
        "http://127.0.0.1:7433",
        tmp_path_factory.mktemp("state") / "private",
        discover=fail,
    )
    instance.refresh()
    instance._discovery_thread.join(2)
    status = instance.status()
    assert "private-secret" not in json.dumps(status)
    assert status["discovery_error"]
    with pytest.raises(ValueError, match="No installed software"):
        instance.connect(str(tmp_path))
    instance.close()


def test_non_loopback_url_rejected(ledger, tmp_path):
    with pytest.raises(ValueError, match="loopback"):
        LocalWorkspaceManager(ledger.service, "http://198.51.100.2:7433", tmp_path)


def test_vendor_command_receives_explicit_permission_mode(manager, tmp_path):
    manager.permission_mode = "danger-full-access"
    with manager._lock:
        manager._catalogue[0]["id"] = "dsh"
        manager._runners = {
            "dsh": {
                "adapter": "command",
                "command": [
                    "python",
                    "-m",
                    "relayfence.vendor_adapter",
                    "--vendor",
                    "dsh",
                    "--model",
                    "{model}",
                    "--parameters",
                    "{parameters_json}",
                    "--permission-mode",
                    "workspace-write",
                ],
            }
        }
    status = manager.connect(str(tmp_path))
    agent_id = status["bindings"][0]["agent_id"]
    config = json.loads((manager.directory / status["project"] / (agent_id + ".json")).read_text())
    command = config["command"]
    assert command[command.index("--permission-mode") + 1] == "danger-full-access"
    assert command.count("--permission-mode") == 1


def test_invalid_catalogue_rejected_before_any_agents_are_registered(manager, ledger, tmp_path):
    with manager._lock:
        provider = manager._catalogue[0]["providers"][0]
        provider["models"].append(provider["models"][0])
    with pytest.raises(ValueError):
        manager.connect(str(tmp_path))
    assert ledger.ok("agents_list", project=manager._identity(tmp_path))["agents"] == []


def test_refresh_automatically_applies_changed_catalogue_when_idle(manager, ledger, tmp_path):
    status = manager.connect(str(tmp_path))
    previous_thread = manager._thread

    def newer(path):
        result = discovered(path)
        result.catalogue[0]["providers"][0]["models"].append({"id": "model-three"})
        return result

    manager._discover = newer
    manager.refresh()
    manager._discovery_thread.join(3)
    current = manager.status()
    assert not current["refresh_pending"]
    assert manager._thread is not previous_thread
    assert not previous_thread.is_alive()
    agent = ledger.ok(
        "agent_get", project=status["project"], agent_id=status["bindings"][0]["agent_id"]
    )
    assert [model["id"] for model in agent["models"]] == ["model-one", "model-two", "model-three"]
    assert current["catalogue"][0]["providers"][0]["models"][-1]["id"] == "model-three"


def test_refresh_waits_for_active_task_then_applies_without_reconnect(manager, ledger, tmp_path):
    original = manager.connect(str(tmp_path))
    old_dispatcher, old_thread = manager._dispatcher, manager._thread
    active_future = Future()
    old_dispatcher.workers[0]["future"] = active_future

    def newer(path):
        result = discovered(path)
        result.catalogue[0]["providers"][0]["models"] = [{"id": "model-new"}]
        return result

    manager._discover = newer
    manager.refresh()
    deadline = time.monotonic() + 2
    while not manager.status()["refresh_pending"] and time.monotonic() < deadline:
        time.sleep(0.01)
    pending = manager.status()
    assert pending["refresh_pending"] and pending["pending_catalogue_models"] == 1
    assert pending["refresh_message"]
    assert manager._thread is old_thread and old_thread.is_alive()
    # Public choices remain exactly the active registered choices while a replacement waits.
    assert pending["catalogue"] == original["catalogue"]
    # Completion is sufficient even before a subsequent dispatcher tick cleans up the future.
    active_future.set_result({"ok": True})
    manager._discovery_thread.join(3)
    current = manager.status()
    assert not current["refresh_pending"] and current["refresh_message"] is None
    assert current["catalogue"][0]["providers"][0]["models"] == [{"id": "model-new"}]
    agent = ledger.ok(
        "agent_get", project=current["project"], agent_id=current["bindings"][0]["agent_id"]
    )
    assert agent["models"] == [{"id": "model-new", "parameters": {}}]
    assert not old_thread.is_alive()


def test_refresh_with_no_available_providers_retains_working_runner_without_pausing(
    manager, tmp_path
):
    original = manager.connect(str(tmp_path))
    dispatcher = manager._dispatcher
    manager._discover = lambda path: SimpleNamespace(catalogue=[], runners={})
    manager.refresh()
    manager._discovery_thread.join(3)
    current = manager.status()
    assert not current["refresh_pending"]
    assert current["refresh_error"] and current["refresh_message"]
    assert current["runner_running"] and current["runner_error"] is None
    assert current["catalogue"] == original["catalogue"]
    assert current["bindings"] == original["bindings"]
    assert manager._dispatcher is dispatcher and not dispatcher.stop.is_set()
    # A subsequent dispatcher tick is permitted once the failed staged update is cleared.
    assert manager._pending_discovery is None
    ticks = dispatcher.ticks
    dispatcher.tick(None)
    assert dispatcher.ticks == ticks + 1


def test_refresh_provisioning_failure_disables_bindings_and_reports_stopped_runner(
    manager, tmp_path, monkeypatch
):
    manager.connect(str(tmp_path))
    old_thread = manager._thread

    def newer(path):
        result = discovered(path)
        result.catalogue[0]["providers"][0]["models"] = [{"id": "replacement"}]
        return result

    def fail(*args, **kwargs):
        raise RuntimeError("private-credential-details")

    manager._discover = newer
    monkeypatch.setattr(manager, "_call", fail)
    manager.refresh()
    manager._discovery_thread.join(3)
    current = manager.status()
    assert not current["refresh_pending"] and current["refresh_error"]
    assert not current["runner_running"] and current["runner_error"]
    assert current["bindings"] == []
    assert current["catalogue"][0]["providers"][0]["agent_id"] is None
    assert "private-credential-details" not in json.dumps(current)
    assert not old_thread.is_alive()


def _credential_discover(_):
    return SimpleNamespace(
        catalogue=[
            {
                "id": "dsh",
                "label": "DSH",
                "installed": True,
                "status": "available",
                "message": "",
                "providers": [
                    {
                        "id": "isoft",
                        "label": "iSoft",
                        "models": [{"id": "deepseek-flash", "parameters": {}}],
                        "auth": {
                            "kind": "api_key",
                            "authenticated": False,
                            "credential_env": "ISOFT_API_KEY",
                            "source": None,
                            "message": "缺少 ISOFT_API_KEY，请配置密钥",
                        },
                    }
                ],
            }
        ],
        runners={"dsh": {"adapter": "command", "command": ["dsh", "{model}"]}},
    )


def test_credential_save_updates_live_and_pending_catalogues(ledger, tmp_path, monkeypatch):
    monkeypatch.setattr("relayfence.local_workspace.Dispatcher", FakeDispatcher)
    manager = LocalWorkspaceManager(
        ledger.service,
        "http://127.0.0.1:7433",
        tmp_path / "private",
        discover=_credential_discover,
    )
    manager.start_discovery()
    manager._discovery_thread.join(2)
    try:
        # Stage a pending catalogue as if running tasks hold the connection.
        manager._workspace = str(tmp_path)
        manager._pending_discovery = (
            copy.deepcopy(manager._catalogue),
            copy.deepcopy(manager._runners),
        )
        monkeypatch.setattr(manager, "refresh", lambda: None)
        saved = manager.save_agent_credential(
            {
                "software": "dsh",
                "provider": "isoft",
                "name": "ISOFT_API_KEY",
                "value": "sk-local-secret-9876",
            }
        )
        assert saved["masked"] == "••••••••9876"
        live = manager._credential_target("dsh", "isoft", "ISOFT_API_KEY")[1]
        assert live["auth"]["stored"] is True and live["authenticated"] is True
        pending = manager._pending_discovery[0][0]["providers"][0]["auth"]
        assert pending["stored"] is True and pending["authenticated"] is True
    finally:
        manager.close()


def test_reconcile_retains_pending_discovery_while_a_scan_is_running(manager):
    attempts = []

    def flaky(path):
        attempts.append(path)
        if len(attempts) < 3:
            raise ValueError("Installed software discovery is running; try again shortly")
        return None

    manager.connect = flaky
    manager._workspace = "ws"
    manager._pending_discovery = ({}, {})
    manager._reconcile_refresh()
    assert len(attempts) == 3 and manager._pending_discovery

    def broken(path):
        raise ValueError("Choose an existing absolute project directory")

    manager.connect = broken
    manager._reconcile_refresh()
    assert manager._pending_discovery is None and manager._refresh_error


def test_project_sessions_are_shared_by_software_and_isolated_by_project(manager, tmp_path):
    from pathlib import Path

    from relayfence.project_sessions import ProjectSession

    workspace = tmp_path / "app-one"
    workspace.mkdir()
    first = manager.connect(str(workspace))
    configs = [
        json.loads(p.read_text())
        for p in (manager.directory / first["project"]).glob("local-*.json")
    ]
    assert configs and all(c["session_file"].endswith("codex.json") for c in configs)
    path = Path(configs[0]["session_file"])
    binding = ProjectSession(path, first["project"], "codex", workspace)
    binding.begin({"task_id": "first"})
    binding.bind("shared-project-thread")
    binding.finish(True)
    assert (
        manager.project_sessions(first["project"])["sessions"][0]["session_id"]
        == "shared-project-thread"
    )
    other = tmp_path / "app-two"
    other.mkdir()
    second = manager.connect(str(other))
    assert manager.project_sessions(second["project"])["sessions"] == []
    assert manager.project_sessions(first["project"])["sessions"][0]["turns"] == 1


def test_session_reset_is_explicit_versioned_and_refuses_live_workspace(manager, tmp_path):
    from relayfence.locking import local_lock
    from relayfence.project_sessions import ProjectSession

    workspace = tmp_path / "app"
    workspace.mkdir()
    project = manager.connect(str(workspace))["project"]
    path = manager.directory / project / "sessions" / "codex.json"
    binding = ProjectSession(path, project, "codex", workspace)
    binding.begin({"task_id": "first"})
    binding.bind("old-thread")
    binding.finish(True)
    payload = dict(
        project=project,
        runtime="codex",
        session_id="old-thread",
        updated_at=binding.value["updated_at"],
    )
    with pytest.raises(ValueError, match="已变化"):
        manager.reset_project_session({**payload, "session_id": "stale"})
    with local_lock(workspace / ".relayfence" / "worker.lock"):
        with pytest.raises(ValueError, match="仍有执行"):
            manager.reset_project_session(payload)
    assert manager.reset_project_session(payload)["sessions"] == []
    assert list(path.parent.glob("codex.archived-*.json"))
    assert not path.exists()
    with pytest.raises(ValueError, match="尚未建立"):
        manager.reset_project_session(payload)
    with pytest.raises(ValueError, match="不存在"):
        manager.reset_project_session({**payload, "runtime": "../other"})


def test_session_reset_does_not_recreate_missing_project_directory(manager, tmp_path):
    workspace = tmp_path / "missing-project"
    workspace.mkdir()
    manager.add_project(str(workspace))
    project = next(p["id"] for p in manager.projects()["projects"] if p["path"] == str(workspace))
    workspace.rmdir()
    with pytest.raises(ValueError, match="目录不存在"):
        manager.reset_project_session({"project": project, "runtime": "codex"})
    assert not workspace.exists()
