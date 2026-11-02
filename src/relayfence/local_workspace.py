"""Server-owned local project connection and automatic scoped runner setup."""

import copy
import hashlib
import ipaddress
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .agent_credentials import AgentCredentials
from .agent_inventory import build_inventory
from .dispatcher import Dispatcher
from .models import AgentPut
from .storage import ProjectStore
from .worker import WorkerConfig, replace_private

# Vendor CLIs supply display labels (software/provider names, model publisher
# wording). Only characters in this conservative set become shared ledger
# labels; anything else is dropped so prompt fragments and markup can't be
# smuggled into records other office members read.
_LABEL_CHARS = re.compile(r"[^ \w./:+@()\[\]（）\-]+", re.UNICODE)


def _safe_label(value):
    text = _LABEL_CHARS.sub(" ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120]


def _contains(parent, child):
    """Case-normalised path containment, including the path itself."""
    try:
        Path(os.path.normcase(str(child))).relative_to(Path(os.path.normcase(str(parent))))
        return True
    except ValueError:
        return False


class LocalWorkspaceManager:
    """Own discovery, credentials and one parallel dispatcher per selected directory.

    Only trusted local discovery supplies executable settings. Browser requests supply
    an existing absolute directory, never commands, catalogue entries or credentials.
    """

    def __init__(
        self,
        service,
        url,
        directory,
        permission_mode="workspace-write",
        discover=None,
        state_root=None,
        max_parallel=2,
    ):
        parsed = urlsplit(url)
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except (TypeError, ValueError):
            loopback = parsed.hostname == "localhost"
        if parsed.scheme != "http" or not loopback or parsed.username or parsed.password:
            raise ValueError("Local runners require a loopback HTTP service")
        if permission_mode not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("Invalid local runner permission mode")
        self.service, self.url = service, url
        self.directory = Path(directory).resolve()
        # The CLI passes the whole state directory (token + ledger + local
        # configs); tests and embedders default to the local config directory.
        self.state_root = Path(state_root or directory).resolve()
        self.permission_mode = permission_mode
        from .execution_capacity import ExecutionCapacity

        self.capacity = ExecutionCapacity(max_parallel)
        self._discover = discover
        self._lock = threading.RLock()
        self._connection_lock = threading.RLock()
        self._execution_lock = threading.Lock()
        self._discovery_thread = None
        self._contexts = {}
        self._dispatcher = None
        self._thread = None
        self._catalogue, self._runners, self._bindings = [], {}, []
        self._workspace = self._project = self._connected_signature = None
        self._discovering = self._closed = False
        self._discovery_error = self._runner_error = None
        self._pending_discovery = None
        self._refresh_message = None
        self._refresh_error = None
        self._shutdown = threading.Event()
        self._scanned_at = None
        self._refresh_again = False
        self.projects_store = ProjectStore(service.store, self.directory)
        self.projects_store.migrate_legacy()
        self.agent_credentials = AgentCredentials(service.store, self.directory)
        from .group_chat import GroupChat
        from .member_chat import MemberChat

        self.member_chat = MemberChat(service, self)
        self.member_chat.recover_pending()
        self.group_chat = GroupChat(service)

    def _saved_projects(self):
        saved = self.projects_store.as_dict()
        if saved:
            return saved
        # Recover directories connected before the SQLite project index existed,
        # then persist the recovered rows into SQLite.
        recovered = {}
        for config_path in self.directory.glob("local-*/local-*.json"):
            try:
                if config_path.stat().st_size > 1048576:
                    continue
                config = json.loads(config_path.read_text(encoding="utf-8"))
                folder = Path(config["cwd"])
                ident = self._identity(folder)
                if folder.is_absolute() and config.get("project") == ident:
                    recovered[ident] = {
                        "id": ident,
                        "name": folder.name or str(folder),
                        "path": str(folder),
                    }
            except (OSError, ValueError, KeyError, TypeError):
                continue
        for ident, item in recovered.items():
            self.projects_store.upsert(ident, item["path"], item["name"])
        return recovered

    def add_project(self, path):
        if (
            not isinstance(path, str)
            or not path.strip()
            or not Path(path).expanduser().is_absolute()
        ):
            raise ValueError("请选择已有项目的绝对目录")
        folder = Path(path).expanduser().resolve(strict=True)
        if not folder.is_dir():
            raise ValueError("所选路径不是文件夹")
        project = self._identity(folder)
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.projects_store.upsert(project, str(folder), folder.name or str(folder))
        return self.projects()

    def projects(self):
        with self._lock:
            items = list(self._saved_projects().values())
            active = self._project
        with self.service.store.connection() as db:
            for item in items:
                item["active"] = item["id"] == active
                item["exists"] = Path(item["path"]).is_dir()
                office = db.execute(
                    "SELECT definition FROM office_configs WHERE project=?", (item["id"],)
                ).fetchone()
                item["groups"] = len(json.loads(office["definition"])) if office else 0
                item["running_tasks"] = db.execute(
                    "SELECT count(*) FROM tasks WHERE project=? AND status='running'", (item["id"],)
                ).fetchone()[0]
                item["tasks"] = db.execute(
                    "SELECT count(*) FROM tasks WHERE project=?", (item["id"],)
                ).fetchone()[0]
        return {
            "projects": sorted(items, key=lambda item: item["name"].casefold()),
            "active_project": active,
        }

    def start_discovery(self):
        return self.refresh()

    def refresh(self):
        """Refresh installed software off the HTTP event loop without touching live jobs."""
        with self._lock:
            if self._closed:
                raise ValueError("Local project connection has closed")
            if self._discovery_thread and self._discovery_thread.is_alive():
                self._refresh_again = True
            elif not self._discovery_thread or not self._discovery_thread.is_alive():
                self._discovering = True
                self._discovery_thread = threading.Thread(
                    target=self._refresh, name="relayfence-discovery", daemon=True
                )
                self._discovery_thread.start()
        return self.status()

    def _refresh(self):
        while True:
            try:
                if self._discover is None:
                    from .discovery import discover_installed

                    discover = discover_installed
                else:
                    discover = self._discover
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                result = discover(self.directory / "discovery")
                with self._lock:
                    if not self._closed:
                        catalogue, runners = (
                            copy.deepcopy(result.catalogue),
                            copy.deepcopy(result.runners),
                        )
                        self.agent_credentials.annotate(catalogue)
                        self._scanned_at = time.time()
                        signature = json.dumps([catalogue, runners], sort_keys=True)
                        if self._workspace and signature != self._connected_signature:
                            # Keep the currently registered catalogue usable until its
                            # replacement has actually been provisioned, including mid-task.
                            self._pending_discovery = (catalogue, runners)
                            self._refresh_message = "New software settings will apply automatically after the current tasks finish."
                        else:
                            self._catalogue, self._runners = catalogue, runners
                            self._pending_discovery = None
                            self._refresh_message = None
                        self._discovery_error = None
                        self._refresh_error = None
            except Exception:
                with self._lock:
                    self._discovery_error = "Installed software discovery failed; refresh to retry."
            with self._lock:
                # Reconcile must be able to call connect(), which refuses to run
                # while discovery is marked active.
                self._discovering = False
                self._refresh_again = False
                if self._closed or self._shutdown.is_set():
                    return
            self._reconcile_refresh()
            with self._lock:
                if self._refresh_again and not self._closed and not self._shutdown.is_set():
                    self._discovering = True
                    continue
                return

    def _reconcile_refresh(self):
        while not self._shutdown.is_set():
            with self._lock:
                workspace = self._workspace
                if self._pending_discovery is None or not workspace:
                    return
            try:
                with self._connection_lock:
                    with self._lock:
                        if self._workspace != workspace:
                            return
                    self.connect(workspace)
                return
            except ValueError as exc:
                # Both "current tasks running" and a user-triggered discovery
                # scan are transient: keep the pending discovery for a retry.
                text = str(exc)
                if "current tasks" not in text and "discovery is running" not in text:
                    self._refresh_failed()
                    return
            except Exception:
                self._refresh_failed()
                return
            self._shutdown.wait(0.25)

    def _refresh_failed(self):
        with self._lock:
            self._pending_discovery = None
            self._refresh_error = "Automatic software refresh could not apply; refresh to retry."
            retained = bool(
                self._thread
                and self._thread.is_alive()
                and self._dispatcher
                and not self._dispatcher.stop.is_set()
            )
            if retained:
                self._refresh_message = (
                    "Refresh failed. The previous runner configuration is retained; "
                    "refreshed software availability has not been applied."
                )
            else:
                self._bindings = []
                self._runner_error = "Local execution stopped during software refresh; reconnect or refresh to retry."
                self._refresh_message = self._runner_error

    def status(self):
        with self._lock:
            catalogue = copy.deepcopy(self._catalogue)
            bindings = copy.deepcopy(self._bindings)
            for software in catalogue:
                for provider in software.get("providers", []):
                    provider["agent_id"] = next(
                        (
                            b["agent_id"]
                            for b in bindings
                            if b["software"] == software["id"] and b["provider"] == provider["id"]
                        ),
                        None,
                    )
            return {
                "project": self._project,
                "workspace": self._workspace,
                "catalogue": catalogue,
                "bindings": bindings,
                "discovering": self._discovering,
                "discovery_error": self._discovery_error,
                "runner_error": self._runner_error,
                "runner_running": bool(self._thread and self._thread.is_alive()),
                "permission_mode": self.permission_mode,
                "refresh_pending": self._pending_discovery is not None,
                "refresh_message": self._refresh_message,
                "refresh_error": self._refresh_error,
                "pending_catalogue_models": sum(
                    len(provider.get("models", []))
                    for software in self._pending_discovery[0]
                    for provider in software.get("providers", [])
                )
                if self._pending_discovery
                else 0,
            }

    def agent_inventory(self):
        with self._lock:
            catalogue = copy.deepcopy(self._catalogue)
            discovering = self._discovering
            scanned_at = self._scanned_at
        return build_inventory(catalogue, discovering=discovering, scanned_at=scanned_at)

    def project_sessions(self, project=None):
        from .project_sessions import RUNTIMES, read_binding

        project = project or self._project
        known = self.projects_store.as_dict()
        if not isinstance(project, str):
            raise ValueError("项目身份格式无效")
        if not project:
            return {"project": "", "sessions": []}
        if project not in known:
            raise ValueError("请先添加并连接该本地项目")
        rows = []
        for runtime in sorted(RUNTIMES):
            path = self.directory / project / "sessions" / (runtime + ".json")
            try:
                saved = read_binding(path)
                if saved:
                    rows.append(
                        {
                            k: saved.get(k)
                            for k in (
                                "runtime",
                                "session_id",
                                "status",
                                "updated_at",
                                "turns",
                                "last_task",
                                "model",
                            )
                        }
                    )
            except (ValueError, OSError):
                rows.append({"runtime": runtime, "status": "invalid", "session_id": None})
        return {"project": project, "sessions": rows}

    def reset_project_session(self, payload):
        from .locking import WorkspaceBusy, local_lock
        from .project_sessions import RUNTIMES, read_binding

        project, runtime = payload.get("project"), payload.get("runtime")
        known = self.projects_store.as_dict()
        if (
            not isinstance(project, str)
            or not isinstance(runtime, str)
            or project not in known
            or runtime not in RUNTIMES
        ):
            raise ValueError("项目或智能体软件不存在")
        path = self.directory / project / "sessions" / (runtime + ".json")
        workspace = Path(known[project]["path"])
        if not workspace.is_dir():
            raise ValueError("项目目录不存在，请先恢复目录再管理会话")
        try:
            with local_lock(workspace / ".relayfence" / "worker.lock"):
                saved = read_binding(path)
                if saved is None:
                    raise ValueError("该软件尚未建立项目会话")
                if saved.get("session_id") != payload.get("session_id") or saved.get(
                    "updated_at"
                ) != payload.get("updated_at"):
                    raise ValueError("会话记录已变化，请刷新后重试")
                path.replace(path.with_name(runtime + ".archived-" + uuid.uuid4().hex + ".json"))
        except WorkspaceBusy as exc:
            raise ValueError("项目仍有执行正在使用会话，请暂停并等待进程退出后再重建") from exc
        return self.project_sessions(project)

    def _credential_target(self, software, provider_id, name):
        if not isinstance(software, str) or not isinstance(provider_id, str):
            raise ValueError("请选择要配置的智能体供应商")
        if not isinstance(name, str):
            raise ValueError("请选择要配置的密钥")
        with self._lock:
            entry = next((item for item in self._catalogue if item.get("id") == software), None)
            provider = (
                next(
                    (item for item in entry.get("providers", []) if item.get("id") == provider_id),
                    None,
                )
                if entry
                else None
            )
            installed = bool(entry and entry.get("installed", True))
        if not entry or not installed:
            raise ValueError("该智能体尚未安装或未完成扫描")
        if not provider:
            raise ValueError("未找到该模型供应商，请重新扫描")
        auth = provider.get("auth") or {}
        if auth.get("credential_env") != name:
            raise ValueError("该供应商不支持配置此密钥")
        return entry, provider

    def save_agent_credential(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        software = payload.get("software")
        provider = payload.get("provider")
        name = payload.get("name")
        self._credential_target(software, provider, name)
        hint = self.agent_credentials.save(software, provider, name, payload.get("value"))
        self._annotate_credentials()
        self.refresh()
        return {"saved": True, "masked": hint}

    def delete_agent_credential(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        software = payload.get("software")
        provider = payload.get("provider")
        name = payload.get("name")
        self._credential_target(software, provider, name)
        removed = self.agent_credentials.delete(software, provider, name)
        self._annotate_credentials()
        if removed:
            self.refresh()
        return {"deleted": removed}

    def _annotate_credentials(self):
        """Merge key presence into the live and pending catalogues under one lock.

        ``_pending_discovery`` keeps the stored/authenticated flags from its scan
        time, so save/delete must update it too or connect could apply a stale
        view (a deleted key still shown ready, a saved key still unselectable).
        """
        with self._lock:
            self.agent_credentials.annotate(self._catalogue)
            if self._pending_discovery is not None:
                self.agent_credentials.annotate(self._pending_discovery[0])

    def _agent_environment(self, config):
        runtime = getattr(config, "runtime", "")
        provider = getattr(config, "provider", "")
        if not runtime or not provider:
            return {}
        # Injection is restricted to the env names the catalogue currently
        # advertises for this provider, matching what the UI knows.
        names = set()
        with self._lock:
            entry = next((item for item in self._catalogue if item.get("id") == runtime), None)
            if entry:
                target = next(
                    (item for item in entry.get("providers", []) if item.get("id") == provider),
                    None,
                )
                env_name = (target.get("auth") or {}).get("credential_env") if target else None
                if env_name:
                    names.add(env_name)
        if not names:
            return {}
        return self.agent_credentials.environment(runtime, provider, names)

    def _call(self, operation, **payload):
        response, code = self.service.call(operation, payload)
        if code >= 400 or not response["ok"]:
            raise ValueError("Automatic runner setup failed: " + response["error"]["code"])
        return response["data"]

    @staticmethod
    def _identity(path):
        # normcase makes equivalent Windows path spellings share their ledger project.
        return "local-" + hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest()[:24]

    def connect(self, path):
        with self._connection_lock:
            with self._lock:
                previous = self._capture_context()
            try:
                return self._connect(path)
            except Exception:
                # A failed switch must not detach the still-running previous project.
                with self._lock:
                    old = previous["_dispatcher"]
                    if old and not old.stop.is_set():
                        for key, value in previous.items():
                            setattr(self, key, value)
                    else:
                        # The old runner was already stopped, so the project must
                        # not keep looking healthy after a failed reconnect.
                        self._runner_error = (
                            "Local execution could not start; reconnect the project to retry."
                        )
                self._sync_skill_workspace()
                raise

    def _sync_skill_workspace(self):
        """Expose the active project's skill roots to `skill_list` and role validation."""
        if self._project:
            self.service.store.set_skill_workspace(self._workspace, self._project)

    def _connect(self, path):
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise ValueError("Choose an existing absolute project directory")
        workspace = Path(path).expanduser()
        if not workspace.is_absolute() or not workspace.is_dir():
            raise ValueError("Choose an existing absolute project directory")
        workspace = workspace.resolve(strict=True)
        if _contains(workspace, self.state_root) or _contains(self.state_root, workspace):
            raise ValueError(
                "RelayFence state must live outside the connected project. "
                "Restart serve with --directory pointing outside this folder."
            )
        project = self._identity(workspace)
        self.add_project(str(workspace))
        with self._connection_lock:
            with self._lock:
                if self._closed:
                    raise ValueError("Local project connection has closed")
                if self._discovering:
                    raise ValueError("Installed software discovery is running; try again shortly")
                catalogue, runners = copy.deepcopy(
                    self._pending_discovery or (self._catalogue, self._runners)
                )
                signature = json.dumps([catalogue, runners], sort_keys=True)
                if (
                    self._project == project
                    and signature == self._connected_signature
                    and self._thread
                    and self._thread.is_alive()
                ):
                    self._sync_skill_workspace()
                    return self.status()
            definitions = self._definitions(project, workspace, catalogue, runners)
            if not definitions:
                raise ValueError("No installed software with available models was discovered")
            with self._lock:
                if self._project != project:
                    if self._project:
                        self._contexts[self._project] = self._capture_context()
                    if project in self._contexts:
                        self._restore_context(self._contexts[project])
                        if (
                            self._thread
                            and self._thread.is_alive()
                            and not self._dispatcher.stop.is_set()
                        ):
                            self._sync_skill_workspace()
                            return self.status()
                    self._dispatcher = self._thread = None
            self._stop_dispatcher(require_idle=True)
            target = self.directory / project
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Multiple slots per provider let members who share the same
            # provider run concurrently.  Total workers stay bounded by the
            # dispatcher's 16-worker configuration limit.
            slot_count = max(1, min(8, 16 // max(1, len(definitions))))
            max_parallel = min(8, len(definitions) * slot_count)
            workers, bindings = [], []
            for definition, config, binding in definitions:
                agent_id = definition.agent_id
                response, _ = self.service.call(
                    "agent_get", {"project": project, "agent_id": agent_id}
                )
                existing = response["data"] if response["ok"] else None
                payload = definition.model_dump(mode="json")
                payload["expected_revision"] = existing["revision"] if existing else 0
                token_path = target / (agent_id + ".token")
                comparable = {
                    k: payload[k]
                    for k in (
                        "agent_id",
                        "label",
                        "runtime",
                        "mode",
                        "enabled",
                        "capabilities",
                        "models",
                    )
                }
                if existing is None or any(existing[k] != v for k, v in comparable.items()):
                    agent = self._call("agent_put", **payload)
                else:
                    agent = existing
                token_valid = False
                if token_path.exists():
                    token = token_path.read_text(encoding="utf-8").strip()
                    token_hash = self.service.store.digest(token)
                    with self.service.store.connection() as db:
                        identity = db.execute(
                            "SELECT project, agent_id FROM agents WHERE token_hash=?", (token_hash,)
                        ).fetchone()
                    token_valid = bool(
                        identity
                        and identity["project"] == project
                        and identity["agent_id"] == agent_id
                        and self.service.store.credential_valid(token_hash)
                    )
                if not token_valid:
                    issued = self._call(
                        "agent_token_rotate",
                        project=project,
                        actor="local-setup",
                        request_id=uuid.uuid4().hex,
                        agent_id=agent_id,
                        expected_revision=agent["revision"],
                    )
                    self._write_token(token_path, issued["token"])
                config_path = target / (agent_id + ".json")
                config.session_file = str(
                    (target / "sessions" / (binding["software"] + ".json")).resolve()
                )
                replace_private(config_path, config.model_dump(mode="json", exclude_none=True))
                for _ in range(slot_count):
                    workers.append({"config": config_path.name, "token_file": token_path.name})
                bindings.append(binding)
            dispatcher_path = target / "dispatcher.json"
            replace_private(
                dispatcher_path,
                {
                    "workers": workers,
                    "max_parallel": max_parallel,
                    # Start additional slots promptly; selection still enforces
                    # priority and never claims one task twice.
                    "poll_seconds": 0.5,
                },
            )

            def can_dispatch():
                with self._lock:
                    return not (self._project == project and self._pending_discovery is not None)

            dispatcher = Dispatcher(
                dispatcher_path,
                self.url,
                env_provider=self._agent_environment,
                capacity=self.capacity,
                before_poll=lambda: self.service.store.advance_meetings(project),
                can_dispatch=can_dispatch,
                scheduling_lock=self._execution_lock,
            )

            def run():
                try:
                    dispatcher.run()
                except Exception:
                    with self._lock:
                        message = "Local execution stopped; reconnect the project to retry."
                        if self._project == project:
                            self._runner_error = message
                        elif project in self._contexts:
                            self._contexts[project]["_runner_error"] = message

            thread = threading.Thread(target=run, name="relayfence-local-runner", daemon=True)
            with self._lock:
                self._dispatcher, self._thread = dispatcher, thread
                self._workspace, self._project = str(workspace), project
                self._bindings, self._connected_signature = bindings, signature
                self._catalogue, self._runners = catalogue, runners
                self._pending_discovery = None
                self._refresh_message = None
                self._refresh_error = None
                self._runner_error = None
                # Register the root before background meeting advancement can
                # create skill-bearing roles for this project.
                self._sync_skill_workspace()
                thread.start()
            self._sync_skill_workspace()
            return self.status()

    _context_fields = (
        "_dispatcher",
        "_thread",
        "_workspace",
        "_project",
        "_bindings",
        "_connected_signature",
        "_catalogue",
        "_runners",
        "_runner_error",
    )

    def _capture_context(self):
        return {key: getattr(self, key) for key in self._context_fields}

    def _restore_context(self, context):
        # The catalogue and its pending refresh are global (installed software),
        # not project-specific. Preserve them so a switch does not discard a
        # staged refresh or the message explaining why it is pending.
        for key, value in context.items():
            setattr(self, key, value)

    def dispatcher_for(self, project):
        with self._lock:
            if project == self._project:
                return self._dispatcher
            return self._contexts.get(project, {}).get("_dispatcher")

    def runtime_status(self, project):
        if not isinstance(project, str) or project not in self.projects_store.as_dict():
            raise ValueError("项目不存在，请先连接本地目录")
        with self._lock:
            context = (
                self._capture_context()
                if project == self._project
                else self._contexts.get(project, {})
            )
            dispatcher, thread = context.get("_dispatcher"), context.get("_thread")
            running = bool(thread and thread.is_alive())
            error = context.get("_runner_error")
            pending = project == self._project and self._pending_discovery is not None
        health = dispatcher.health() if dispatcher else {"status": "disconnected"}
        if dispatcher and not running:
            health["status"] = "stopped"
        elif pending:
            health["status"] = "refresh_pending"
        return {
            "project": project,
            "observed_at": time.time(),
            "runner_running": running,
            "runner_error": error,
            "health": health,
            "capacity": self.capacity.snapshot(project),
        }

    def _definitions(self, project, workspace, catalogue, runners):
        definitions = []
        for software in catalogue:
            software_id = software["id"]
            settings = runners.get(software_id)
            if software.get("status") != "available" or not settings:
                continue
            for provider in software.get("providers", []):
                if not provider.get("models") or provider.get("authenticated") is False:
                    continue
                agent_id = (
                    "local-"
                    + software_id
                    + "-"
                    + hashlib.sha256(provider["id"].encode()).hexdigest()[:12]
                )
                definition = AgentPut(
                    project=project,
                    actor="local-setup",
                    request_id=uuid.uuid4().hex,
                    agent_id=agent_id,
                    expected_revision=0,
                    label=(
                        (_safe_label(software.get("label") or software_id) or software_id)
                        + " / "
                        + (_safe_label(provider.get("label") or provider["id"]) or provider["id"])
                    )[:160],
                    runtime=software_id,
                    mode="command",
                    models=[
                        {"id": m["id"], "parameters": m.get("parameters", {})}
                        for m in provider["models"]
                    ],
                )
                settings = copy.deepcopy(settings)
                if settings["adapter"] == "codex":
                    settings["sandbox"] = self.permission_mode
                else:
                    command = settings["command"]
                    if "--permission-mode" in command:
                        command[command.index("--permission-mode") + 1] = self.permission_mode
                    else:
                        command += ["--permission-mode", self.permission_mode]
                config = WorkerConfig(
                    **{
                        **settings,
                        "project": project,
                        "agent_id": agent_id,
                        "cwd": str(workspace),
                        "runtime": software_id,
                        "provider": provider["id"],
                    }
                )
                definitions.append(
                    (
                        definition,
                        config,
                        {
                            "software": software_id,
                            "provider": provider["id"],
                            "agent_id": agent_id,
                        },
                    )
                )
        if len(definitions) > 16:
            raise ValueError("Discovered providers exceed the local runner limit of 16")
        return definitions

    @staticmethod
    def _write_token(path, token):
        pending = path.with_name("." + path.name + "." + uuid.uuid4().hex)
        try:
            fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(token + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)

    def _stop_dispatcher(self, require_idle=False):
        with self._execution_lock:
            dispatcher, thread = self._dispatcher, self._thread
            if dispatcher is None:
                return
            if require_idle and any(
                w["future"] is not None and not getattr(w["future"], "done", lambda: False)()
                for w in dispatcher.workers
            ):
                raise ValueError(
                    "Wait for the current tasks to finish before reconnecting the project"
                )
            dispatcher.stop.set()
        if thread:
            thread.join(timeout=15)
            if thread.is_alive():
                raise ValueError("Local runner is still stopping; try again shortly")
        with self._lock:
            self._dispatcher = self._thread = None

    def close(self):
        with self._lock:
            self._closed = True
            self._shutdown.set()
        # The discovery thread may be waiting for the connection lock while it
        # reconciles a staged refresh. Release the lock and join it first.
        discovery = self._discovery_thread
        if discovery and discovery is not threading.current_thread():
            discovery.join(timeout=15)
        with self._connection_lock:
            self._stop_dispatcher()
            for context in self._contexts.values():
                dispatcher, thread = context["_dispatcher"], context["_thread"]
                if dispatcher:
                    dispatcher.stop.set()
                if thread:
                    thread.join(timeout=15)
            self._contexts.clear()
        self.service.store.set_skill_workspace(None, project=None)
