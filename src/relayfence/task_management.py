"""Task lifecycle and prerequisite checks. Store supplies transactional helpers."""

import json

from .errors import CoordinationError


class TaskManagement:
    def _changed_task_dependencies(self, db, project, row):
        pending = list(json.loads(row["task_snapshot"]))
        seen, changed = set(), []
        while pending:
            ref = pending.pop()
            ident = ref["task_id"]
            version = (ident, ref["fence"])
            if version in seen:
                continue
            seen.add(version)
            if len(seen) > 256:
                raise CoordinationError("dependency_limit", "Task dependency closure exceeds 256")
            parent = self._task_row(db, project, ident)
            if parent["status"] != "done" or parent["fence"] != ref["fence"]:
                changed.append(ident)
            pending.extend(json.loads(parent["task_snapshot"]))
        return sorted(changed)

    def _validate_task_dependencies(self, db, project, task_id, dependencies):
        pending = list(dependencies)
        seen = set()
        while pending:
            ident = pending.pop()
            if ident == task_id:
                raise CoordinationError("dependency_cycle", "Task dependencies must be acyclic")
            if ident in seen:
                continue
            seen.add(ident)
            if len(seen) > 256:
                raise CoordinationError("dependency_limit", "Task dependency closure exceeds 256")
            row = self._task_row(db, project, ident)
            pending.extend(json.loads(row["depends_on"]))

    def _task_blockers(self, db, project, row):
        blockers = []
        for ident in json.loads(row["depends_on"]):
            dependency = self._task_row(db, project, ident)
            if dependency["status"] != "done":
                blockers.append({"kind": "task", "key": ident, "reason": dependency["status"]})
            elif (
                self._changed_task_dependencies(db, project, dependency)
                or not self._freshness(db, project, json.loads(dependency["outputs"]))[0]["fresh"]
            ):
                blockers.append({"kind": "task", "key": ident, "reason": "stale_result"})
        for key in json.loads(row["inputs"]):
            head = self._head(db, project, key)
            if not head:
                blockers.append({"kind": "input", "key": key, "reason": "missing"})
            elif not self._freshness(db, project, [{"key": key, "version": head["version"]}])[0][
                "fresh"
            ]:
                blockers.append({"kind": "input", "key": key, "reason": "stale"})
        return blockers

    def _task_update(self, db, r, now):
        row = self._task_row(db, r.project, r.task_id)
        self._check_revision(row, r.expected_revision)
        if row["status"] in ("running", "done"):
            raise CoordinationError(
                "invalid_transition", "Pause running work or reopen done work before editing"
            )
        if r.role_id:
            self._registry_row(db, "roles", r.project, r.role_id)
        self._validate_task_dependencies(db, r.project, r.task_id, r.depends_on)
        db.execute(
            "UPDATE tasks SET title=?,description=?,inputs=?,role_id=?,priority=?,due_at=?,depends_on=?,revision=revision+1,updated_at=? WHERE project=? AND task_id=?",
            (
                r.title,
                r.description,
                self.encode(r.inputs),
                r.role_id,
                r.priority,
                r.due_at,
                self.encode(r.depends_on),
                now,
                r.project,
                r.task_id,
            ),
        )
        self._event(db, r, now, "task.updated", r.task_id, {"revision": row["revision"] + 1})
        return self._task_get(db, r, now)

    def _task_control(self, db, r, now):
        row = self._task_row(db, r.project, r.task_id)
        self._check_revision(row, r.expected_revision)
        transitions = {
            "pause": ({"ready", "running"}, "paused"),
            "resume": ({"paused"}, "ready"),
            "cancel": ({"ready", "running", "paused", "failed"}, "cancelled"),
            "retry": ({"failed", "cancelled"}, "ready"),
        }
        allowed, target = transitions[r.action]
        expired_retry = (
            r.action == "retry" and row["status"] == "running" and row["expires_at"] <= now
        )
        if row["status"] not in allowed and not expired_retry:
            raise CoordinationError(
                "invalid_transition", "This action is not allowed in the current task state"
            )
        db.execute(
            "UPDATE tasks SET status=?,revision=revision+1,owner=NULL,lease_hash=NULL,expires_at=NULL,failure=NULL,updated_at=? WHERE project=? AND task_id=?",
            (target, now, r.project, r.task_id),
        )
        self._event(
            db,
            r,
            now,
            "task." + r.action,
            r.task_id,
            {"previous_status": row["status"], "status": target, "reason": r.reason},
        )
        return self._task_get(db, r, now)

    def _task_fail(self, db, r, now):
        self._assert_lease(db, r, now)
        failure = {"code": r.error_code, "message": r.message, "fence": r.fence, "at": now}
        db.execute(
            "UPDATE tasks SET status='failed',revision=revision+1,lease_hash=NULL,expires_at=NULL,failure=?,updated_at=? WHERE project=? AND task_id=?",
            (self.encode(failure), now, r.project, r.task_id),
        )
        self._event(db, r, now, "task.failed", r.task_id, failure)
        return self._task_get(db, r, now)

    def _runner_heartbeat(self, db, r, now):
        self._registry_row(db, "agents", r.project, r.actor)
        db.execute(
            "INSERT INTO runners VALUES (?,?,?,?,?) ON CONFLICT(project,agent_id) DO UPDATE SET runner_id=excluded.runner_id,active_task=excluded.active_task,updated_at=excluded.updated_at",
            (r.project, r.actor, r.runner_id, r.active_task, now),
        )
        return {"runner_id": r.runner_id, "updated_at": now}

    @staticmethod
    def _runners_list(db, r, now):
        return {
            "runners": [
                {**dict(row), "online": now - row["updated_at"] < 20}
                for row in db.execute(
                    "SELECT * FROM runners WHERE project=? ORDER BY agent_id", (r.project,)
                )
            ]
        }
