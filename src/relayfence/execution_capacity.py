"""Host-local admission control, independent of task leases and process ownership.

Reserve before submitting work; release only after the worker has torn down its
process tree. The filesystem lock remains the cross-process correctness boundary.
"""

import os
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path


class ExecutionCapacity:
    def __init__(self, limit=2, *, clock=time.monotonic):
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("Execution concurrency must be an integer from 1 to 16")
        self.limit = limit
        self.clock = clock
        self._lock = threading.Lock()
        self._active = {}
        self._waiting = OrderedDict()

    @staticmethod
    def workspace_key(workspace):
        return os.path.normcase(str(Path(workspace).resolve()))

    def reserve(self, owner, project, task_id, workspace):
        key = self.workspace_key(workspace)
        with self._lock:
            now = self.clock()
            # A stopped/crashed scheduler must not retain a place indefinitely.
            for waiter, entry in list(self._waiting.items()):
                if now - entry["seen"] > 30:
                    self._waiting.pop(waiter)
            entry = self._waiting.setdefault(owner, {"since": now})
            entry.update(project=project, task_id=task_id, workspace=key, seen=now)
            occupied = {item["workspace"] for item in self._active.values()}
            eligible = next(
                (who for who, item in self._waiting.items() if item["workspace"] not in occupied),
                None,
            )
            if key in occupied or len(self._active) >= self.limit or eligible != owner:
                return None
            ticket = uuid.uuid4().hex
            self._active[ticket] = {**entry, "owner": owner, "started": now}
            self._waiting.pop(owner)
            return ticket

    def release(self, ticket):
        with self._lock:
            self._active.pop(ticket, None)

    def withdraw(self, owner):
        """Remove queued intent, never free a still-running execution."""
        with self._lock:
            self._waiting.pop(owner, None)

    def snapshot(self, project=None):
        with self._lock:
            now = self.clock()
            occupied = {item["workspace"] for item in self._active.values()}
            active = [
                {
                    "project": item["project"],
                    "task_id": item["task_id"],
                    "elapsed_seconds": round(max(0, now - item["started"]), 1),
                }
                for item in self._active.values()
                if project is None or item["project"] == project
            ]
            waiting = [
                {
                    "project": item["project"],
                    "task_id": item["task_id"],
                    "wait_seconds": round(max(0, now - item["since"]), 1),
                    "reason": "workspace_busy"
                    if item["workspace"] in occupied
                    else "capacity_full"
                    if len(self._active) >= self.limit
                    else "fair_queue",
                }
                for item in self._waiting.values()
                if now - item["seen"] <= 30 and (project is None or item["project"] == project)
            ]
            return {
                "limit": self.limit,
                "active_total": len(self._active),
                "workspace_limit": 1,
                "active": active,
                "waiting": waiting,
            }
