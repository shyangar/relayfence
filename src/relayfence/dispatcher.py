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
from .locking import WorkspaceBusy, local_lock
from .worker import load_config, replace_private, run_task, save_private

logger = logging.getLogger(__name__)


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
    def __init__(self, path, url, stop=None):
        self.path = Path(path).resolve()
        try:
            self.config = DispatcherConfig.model_validate_json(self.path.read_bytes())
        except ValueError as exc:
            raise ValueError("Invalid dispatcher configuration") from exc
        self.url = url
        self.stop = stop or threading.Event()
        self.directory = (self.path.parent / self.config.run_directory).resolve()
        self.runner_id = "runner-" + uuid.uuid4().hex[:16]
        self.workers = []
        self.worker_health = {}
        self._poll_loop = None
        self._poll_clients = {}
        self._retry_at = {}
        self._failures = {}
        seen = set()
        for entry in self.config.workers:
            worker_path = (self.path.parent / entry.config).resolve()
            config = load_config(worker_path)
            cwd = (worker_path.parent / config.cwd).resolve()
            if not cwd.is_dir():
                raise ValueError("Worker cwd must be an existing directory")
            identity = (config.project, config.agent_id)
            if identity in seen:
                raise ValueError("Duplicate agent in dispatcher configuration")
            seen.add(identity)
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
                    "rejected": set(),
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

    def _execute(self, worker, task, directory):
        try:
            with self._client(worker) as client:
                run_task(client, worker["path"], task["task_id"], directory, self.stop)
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
                if not outcome["ok"] and outcome["code"] in {
                    "ValueError",
                    "RuntimeError",
                    "OSError",
                    "FileNotFoundError",
                }:
                    worker["rejected"].add((worker["task"]["task_id"], worker["task"]["revision"]))
                worker["future"], worker["task"] = None, None
        busy = [w for w in self.workers if w["future"] is not None]
        if self.stop.is_set():
            return
        if self._poll_loop is None:
            self._poll_loop = asyncio.Runner()
        # Poll identities concurrently, but select from the whole round to preserve
        # global priority (particularly for roles sharing a workspace).
        probes = self._poll_loop.run(self._poll_round(busy))
        candidates = []
        for worker, probe in probes:
            identity = self._identity(worker)
            try:
                if isinstance(probe, Exception):
                    raise probe
                task = probe
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
                self.worker_health[identity] = {
                    "ok": False,
                    "code": exc.code if isinstance(exc, CoordinationError) else type(exc).__name__,
                    "retry_after_seconds": delay,
                }
        for task, worker in sorted(candidates, key=lambda pair: self._priority(pair[0])):
            if self.stop.is_set() or len(busy) >= self.config.max_parallel:
                break
            if any(w["cwd"] == worker["cwd"] for w in busy):
                continue
            directory = self.directory / ("run-" + uuid.uuid4().hex)
            worker["task"] = task
            worker["future"] = pool.submit(self._execute, worker, task, directory)
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

        return await asyncio.gather(
            *(
                probe(worker)
                for worker in self.workers
                if time.monotonic() >= self._retry_at.get(self._identity(worker), 0)
            )
        )

    async def _candidate(self, worker, busy):
        config = worker["config"]
        async with self._poll_client(worker) as client:
            if self.stop.is_set():
                return None
            await client.call(
                "runner_heartbeat",
                project=config.project,
                actor=config.agent_id,
                request_id=uuid.uuid4().hex,
                runner_id=self.runner_id,
                active_task=worker["task"]["task_id"] if worker["task"] else None,
            )
            if self.stop.is_set() or worker["future"] or len(busy) >= self.config.max_parallel:
                return None
            if any(w["cwd"] == worker["cwd"] for w in busy):
                return None
            roles = await self._list(client, "roles_list", "roles", config.project)
            assigned = {role["role_id"] for role in roles if role["agent_id"] == config.agent_id}
            tasks = await self._list(client, "tasks_list", "tasks", config.project, status="ready")
            tasks = [
                task
                for task in tasks
                if task["role_id"] in assigned
                and (task["task_id"], task["revision"]) not in worker["rejected"]
            ]
            return min(tasks, key=self._priority) if tasks else None

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
