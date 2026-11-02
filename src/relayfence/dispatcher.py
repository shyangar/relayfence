"""Local polling dispatcher. Executable settings come only from a private config file."""

import asyncio
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .client import AsyncRelayFenceClient, RelayFenceClient
from .errors import CoordinationError
from .execution_capacity import ExecutionCapacity
from .locking import WorkspaceBusy, local_lock
from .worker import load_config, replace_private, run_task, save_private

logger = logging.getLogger(__name__)


class _ExecutionStop:
    """Observe runner shutdown and an individual task interruption together."""

    def __init__(self, runner_stop, task_stop):
        self.runner_stop = runner_stop
        self.task_stop = task_stop

    def is_set(self):
        return self.runner_stop.is_set() or self.task_stop.is_set()


class WorkerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: str
    token_file: str


class DispatcherConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workers: list[WorkerEntry] = Field(min_length=1, max_length=16)
    run_directory: str = "runs"
    poll_seconds: float = Field(default=2, ge=0.2, le=10, allow_inf_nan=False)
    max_parallel: int = Field(default=2, ge=1, le=16, strict=True)
    request_timeout_seconds: float = Field(default=3, ge=0.2, le=10, allow_inf_nan=False)


class Dispatcher:
    def __init__(
        self,
        path,
        url,
        stop=None,
        env_provider=None,
        *,
        capacity=None,
        before_poll=None,
        can_dispatch=None,
        scheduling_lock=None,
    ):
        self.path = Path(path).resolve()
        self.env_provider = env_provider
        try:
            self.config = DispatcherConfig.model_validate_json(self.path.read_bytes())
        except ValueError as exc:
            raise ValueError("Invalid dispatcher configuration") from exc
        self.url = url
        self.stop = stop or threading.Event()
        self.directory = (self.path.parent / self.config.run_directory).resolve()
        self.runner_id = "runner-" + uuid.uuid4().hex[:16]
        self.capacity = capacity or ExecutionCapacity(self.config.max_parallel)
        self.before_poll = before_poll
        self.can_dispatch = can_dispatch or (lambda: True)
        self._scheduling_lock = scheduling_lock or threading.Lock()
        self._last_poll_at = None
        self._last_poll_error = None
        self.workers = []
        self.worker_health = {}
        self._poll_loop = None
        self._poll_clients = {}
        self._retry_at = {}
        self._failures = {}
        self._execution_lock = threading.Lock()
        for entry in self.config.workers:
            worker_path = (self.path.parent / entry.config).resolve()
            config = load_config(worker_path)
            cwd = (worker_path.parent / config.cwd).resolve()
            if not cwd.is_dir():
                raise ValueError("Worker cwd must be an existing directory")
            token_file = (self.path.parent / entry.token_file).resolve()
            self._token(token_file)
            self.workers.append(
                {
                    "path": worker_path,
                    "config": config,
                    "cwd": cwd,
                    "token_file": token_file,
                    "future": None,
                    "task": None,
                    "task_stop": None,
                    # (task_id, revision) -> (failures, retry_at). A transient
                    # execution failure is retried with bounded backoff instead
                    # of rejecting the task for the life of the dispatcher.
                    "rejected": {},
                }
            )

    @staticmethod
    def _token(path):
        # Never use RELAYFENCE_TOKEN here: serve may have an admin token in its environment.
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("Invalid dispatcher instance token file")
        return token

    def _client(self, worker):
        return RelayFenceClient(
            self.url, self._token(worker["token_file"]), timeout=self.config.request_timeout_seconds
        )

    def _async_client(self, worker):
        return AsyncRelayFenceClient(
            self.url,
            "Bearer " + self._token(worker["token_file"]),
            timeout=self.config.request_timeout_seconds,
        )

    @staticmethod
    def _identity(worker):
        return worker["config"].project + "/" + worker["config"].agent_id

    @asynccontextmanager
    async def _poll_client(self, worker):
        # Each identity has at most one probe in flight. Execution uses a separate
        # client, so polling/rotation cannot close a task's active connection pool.
        identity = self._identity(worker)
        token = self._token(worker["token_file"])
        cached = self._poll_clients.get(identity)
        if cached is not None and cached[0] != token:
            await cached[1].__aexit__(None, None, None)
            del self._poll_clients[identity]
            cached = None
        if cached is None:
            context = self._async_client(worker)
            client = await context.__aenter__()
            cached = token, context, client
            self._poll_clients[identity] = cached
        yield cached[2]

    def close(self):
        """Stop probes before closing their clients; safe after run() or manual ticks."""
        self.stop.set()
        self.capacity.withdraw(self.runner_id)
        if self._poll_loop is not None:

            async def close_clients():
                try:
                    for _, context, _ in self._poll_clients.values():
                        await context.__aexit__(None, None, None)
                finally:
                    self._poll_clients.clear()

            try:
                self._poll_loop.run(close_clients())
            finally:
                self._poll_loop.close()
                self._poll_loop = None

    async def _list(self, client, method, key, project, **extra):
        result, after = [], ""
        while True:
            if self.stop.is_set():
                return []
            page = (await client.call(method, project=project, after=after, limit=200, **extra))[
                "data"
            ]
            result.extend(page[key])
            after = page["next_cursor"]
            if not after:
                return result

    def task_running(self, project, task_id):
        """Whether a local execution still owns resources for this task."""
        with self._execution_lock:
            return any(
                worker["config"].project == project
                and worker["task"] is not None
                and worker["task"]["task_id"] == task_id
                and worker["future"] is not None
                and not worker["future"].done()
                for worker in self.workers
            )

    def interrupt_task(self, project, task_id):
        """Signal one execution and return its teardown future, if present.

        The caller must fence/pause the ledger task before signalling, then wait
        for this future before resuming. Completion means the worker has killed
        its process tree and released its local locks. Other tasks continue.
        """
        with self._execution_lock:
            for worker in self.workers:
                if (
                    worker["config"].project == project
                    and worker["task"] is not None
                    and worker["task"]["task_id"] == task_id
                    and worker["future"] is not None
                ):
                    worker["task_stop"].set()
                    return worker["future"]
        return None

    def _execute_reserved(self, worker, task, directory, task_stop, ticket):
        try:
            return self._execute(worker, task, directory, task_stop)
        finally:
            self.capacity.release(ticket)

    def health(self):
        """A sanitized live observation; thread existence alone is not readiness."""
        with self._execution_lock:
            age = (
                None
                if self._last_poll_at is None
                else max(0, time.monotonic() - self._last_poll_at)
            )
            workers = {key: dict(value) for key, value in self.worker_health.items()}
            status = (
                "stopping"
                if self.stop.is_set()
                else "degraded"
                if self._last_poll_error
                else "starting"
                if age is None
                else "stale"
                if age > max(15, self.config.request_timeout_seconds * 2 + self.config.poll_seconds)
                else "degraded"
                if any(not value["ok"] for value in workers.values())
                else "healthy"
            )
            return {
                "status": status,
                "last_poll_age_seconds": age,
                "error_code": self._last_poll_error,
                "workers": workers,
            }

    def _execute(self, worker, task, directory, task_stop):
        try:
            extra_env = (
                self.env_provider(worker["config"]) if self.env_provider is not None else None
            )
            with self._client(worker) as client:
                run_task(
                    client,
                    worker["path"],
                    task["task_id"],
                    directory,
                    _ExecutionStop(self.stop, task_stop),
                    env=extra_env,
                    # Use the configuration validated at dispatcher startup. A
                    # process that can write the state directory must not be able
                    # to swap the executable between tasks.
                    config=worker["config"],
                )
            outcome = {"ok": True, "task_id": task["task_id"]}
        except WorkspaceBusy:
            return {"ok": False, "task_id": task["task_id"], "code": "workspace_busy"}
        except Exception as exc:
            outcome = {
                "ok": False,
                "task_id": task["task_id"],
                "code": exc.code if isinstance(exc, CoordinationError) else type(exc).__name__,
                "message": "Execution did not finish normally. Inspect this run; no automatic retry was requested.",
            }
        directory.mkdir(parents=True, exist_ok=True)
        save_private(directory / "dispatch-result.json", outcome)
        return outcome

    def tick(self, pool):
        for worker in self.workers:
            future = worker["future"]
            if future is not None and future.done():
                try:
                    outcome = future.result()
                except Exception as exc:
                    outcome = {"ok": False, "code": type(exc).__name__}
                task = worker["task"]
                if task is not None:
                    key = (task["task_id"], task["revision"])
                    if outcome["ok"]:
                        worker["rejected"].pop(key, None)
                    elif outcome["code"] in {
                        "ValueError",
                        "RuntimeError",
                        "OSError",
                        "FileNotFoundError",
                    }:
                        failures, _ = worker["rejected"].get(key, (0, 0))
                        failures = min(failures + 1, 8)
                        delay = min(
                            300.0,
                            max(2.0, self.config.poll_seconds * 2 ** (failures - 1)),
                        )
                        worker["rejected"][key] = (failures, time.monotonic() + delay)
                        if len(worker["rejected"]) > 256:
                            for old in list(worker["rejected"])[:-256]:
                                worker["rejected"].pop(old, None)
                with self._execution_lock:
                    worker["future"], worker["task"], worker["task_stop"] = None, None, None
        busy = [w for w in self.workers if w["future"] is not None]
        claimed = {
            (w["config"].project, w["task"]["task_id"])
            for w in self.workers
            if w["task"] is not None and w["future"] is not None
        }
        if self.stop.is_set():
            self.capacity.withdraw(self.runner_id)
            return
        if not self.can_dispatch():
            self.capacity.withdraw(self.runner_id)
            return
        if self.before_poll is not None:
            self.before_poll()
        if self._poll_loop is None:
            self._poll_loop = asyncio.Runner()
        # Poll identities concurrently, but select from the whole round to preserve
        # global priority. Multiple slots for the same provider must not claim the
        # same task; ``claimed`` also covers tasks selected earlier in this round.
        probes = self._poll_loop.run(self._poll_round(busy))
        candidates = []
        for worker, probe in probes:
            identity = self._identity(worker)
            try:
                if isinstance(probe, Exception):
                    raise probe
                task = probe
                with self._execution_lock:
                    self.worker_health[identity] = {"ok": True}
                self._failures.pop(identity, None)
                self._retry_at.pop(identity, None)
                if task:
                    candidates.append((task, worker))
            except (CoordinationError, OSError, ValueError) as exc:
                failures = min(self._failures.get(identity, 0) + 1, 8)
                self._failures[identity] = failures
                delay = min(30, self.config.poll_seconds * 2 ** (failures - 1))
                self._retry_at[identity] = time.monotonic() + delay
                with self._execution_lock:
                    self.worker_health[identity] = {
                        "ok": False,
                        "code": exc.code
                        if isinstance(exc, CoordinationError)
                        else type(exc).__name__,
                        "retry_after_seconds": delay,
                    }
        with self._execution_lock:
            self._last_poll_at = time.monotonic()
            self._last_poll_error = None
        if not candidates:
            self.capacity.withdraw(self.runner_id)
        for task, worker in sorted(candidates, key=lambda pair: self._priority(pair[0])):
            if self.stop.is_set() or len(busy) >= self.config.max_parallel:
                break
            task_key = (worker["config"].project, task["task_id"])
            if task_key in claimed:
                continue
            directory = self.directory / ("run-" + uuid.uuid4().hex)
            with self._scheduling_lock:
                if self.stop.is_set() or not self.can_dispatch():
                    self.capacity.withdraw(self.runner_id)
                    break
                ticket = self.capacity.reserve(
                    self.runner_id, worker["config"].project, task["task_id"], worker["cwd"]
                )
                if ticket is None:
                    # All local slots for this project use the same checkout.
                    # Keep the highest-priority candidate at the head of its queue.
                    break
                try:
                    with self._execution_lock:
                        worker["task"] = task
                        worker["task_stop"] = threading.Event()
                        worker["future"] = pool.submit(
                            self._execute_reserved,
                            worker,
                            task,
                            directory,
                            worker["task_stop"],
                            ticket,
                        )
                        worker["future"].add_done_callback(
                            lambda future, reserved=ticket: (
                                self.capacity.release(reserved) if future.cancelled() else None
                            )
                        )
                except BaseException:
                    self.capacity.release(ticket)
                    with self._execution_lock:
                        worker["task"], worker["task_stop"] = None, None
                    raise
            claimed.add(task_key)
            busy.append(worker)

    async def _poll_round(self, busy):
        async def probe(worker):
            try:
                # Bound the whole probe, including all pages and slow trickling
                # responses. No polling threads survive a cancelled request.
                async with asyncio.timeout(self.config.request_timeout_seconds):
                    return worker, await self._candidate(worker, busy)
            except TimeoutError:
                return worker, CoordinationError("poll_timeout", "Runner polling timed out")
            except (CoordinationError, OSError, ValueError) as exc:
                return worker, exc

        # Provider slots share a client; concurrent probes on that client would
        # race initialization/rotation and multiply identical ledger requests.
        representatives = {}
        for worker in self.workers:
            identity = self._identity(worker)
            if identity not in representatives or representatives[identity]["future"] is not None:
                representatives[identity] = worker
        return await asyncio.gather(
            *(
                probe(worker)
                for worker in representatives.values()
                if time.monotonic() >= self._retry_at.get(self._identity(worker), 0)
            )
        )

    async def _candidate(self, worker, busy):
        config = worker["config"]
        async with self._poll_client(worker) as client:
            if self.stop.is_set():
                return None
            active_task = next(
                (
                    slot["task"]["task_id"]
                    for slot in self.workers
                    if self._identity(slot) == self._identity(worker)
                    and slot["task"] is not None
                    and slot["future"] is not None
                ),
                None,
            )
            await client.call(
                "runner_heartbeat",
                project=config.project,
                actor=config.agent_id,
                request_id=uuid.uuid4().hex,
                runner_id=self.runner_id,
                active_task=active_task,
            )
            if self.stop.is_set() or worker["future"] or len(busy) >= self.config.max_parallel:
                return None
            active = {
                slot["task"]["task_id"]
                for slot in self.workers
                if slot["config"].project == config.project
                and slot["task"] is not None
                and slot["future"] is not None
            }
            roles = await self._list(client, "roles_list", "roles", config.project)
            assigned = {role["role_id"] for role in roles if role["agent_id"] == config.agent_id}
            tasks = await self._list(client, "tasks_list", "tasks", config.project, status="ready")
            tasks = [
                task
                for task in tasks
                if task["role_id"] in assigned
                and task["task_id"] not in active
                and not self._rejected(worker, task)
            ]
            return min(tasks, key=self._priority) if tasks else None

    @staticmethod
    def _rejected(worker, task):
        entry = worker["rejected"].get((task["task_id"], task["revision"]))
        return bool(entry and time.monotonic() < entry[1])

    @staticmethod
    def _priority(task):
        return (
            -task["priority"],
            task["due_at"] if task["due_at"] is not None else float("inf"),
            task["created_at"],
            task["task_id"],
        )

    def run(self, once=False):
        self.directory.mkdir(parents=True, exist_ok=True)
        with local_lock(self.path.with_suffix(self.path.suffix + ".lock")):
            with ThreadPoolExecutor(max_workers=self.config.max_parallel) as pool:
                try:
                    while not self.stop.is_set():
                        try:
                            self.tick(pool)
                            health = {
                                "ok": all(h["ok"] for h in self.worker_health.values()),
                                "workers": self.worker_health,
                            }
                        except (CoordinationError, OSError) as exc:
                            # Keep private, bounded health state. Never log request bodies or tokens.
                            health = {
                                "ok": False,
                                "code": exc.code
                                if isinstance(exc, CoordinationError)
                                else type(exc).__name__,
                            }
                            with self._execution_lock:
                                self._last_poll_error = health["code"]
                        try:
                            replace_private(self.directory / "health.json", health)
                        except OSError:
                            logger.warning("Unable to replace dispatcher health snapshot")
                        if once:
                            for worker in self.workers:
                                if worker["future"]:
                                    worker["future"].result()
                            return
                        self.stop.wait(self.config.poll_seconds)
                finally:
                    self.close()
