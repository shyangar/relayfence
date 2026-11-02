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
            try:
                parent = self._task_row(db, project, ident)
            except CoordinationError as exc:
                if exc.code != "not_found":
                    raise
                # A snapshot may outlive a deleted prerequisite in an old ledger.
                # Treat it as changed instead of turning the whole claim into 500.
                changed.append(ident)
                continue
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
        decision = db.execute(
            "SELECT decision_id FROM decisions WHERE project=? AND status='pending' AND (task_id=? OR meeting_id IN (SELECT meeting_id FROM meeting_tasks WHERE project=? AND task_id=? AND meeting_id!='')) LIMIT 1",
            (project, row["task_id"], project, row["task_id"]),
        ).fetchone()
        if decision:
            blockers.append(
                {"kind": "decision", "key": decision["decision_id"], "reason": "waiting_user"}
            )
        meeting = db.execute(
            "SELECT m.meeting_id,m.definition FROM meetings m JOIN meeting_tasks t ON m.project=t.project AND m.meeting_id=t.meeting_id WHERE t.project=? AND t.task_id=?",
            (project, row["task_id"]),
        ).fetchone()
        if meeting and json.loads(meeting["definition"]).get("paused"):
            blockers.append({"kind": "meeting", "key": meeting["meeting_id"], "reason": "paused"})
        for ident in json.loads(row["depends_on"]):
            try:
                dependency = self._task_row(db, project, ident)
            except CoordinationError as exc:
                if exc.code != "not_found":
                    raise
                blockers.append({"kind": "task", "key": ident, "reason": "missing"})
                continue
            if dependency["status"] != "done":
                blockers.append({"kind": "task", "key": ident, "reason": dependency["status"]})
            elif not self._delivery_accepted(db, project, dependency):
                blockers.append({"kind": "review", "key": ident, "reason": "awaiting_review"})
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
        if r.review_required is not None:
            db.execute(
                "INSERT INTO task_policies VALUES (?,?,?) ON CONFLICT(project,task_id) DO UPDATE SET review_required=excluded.review_required",
                (r.project, r.task_id, int(r.review_required)),
            )
        self._event(db, r, now, "task.updated", r.task_id, {"revision": row["revision"] + 1})
        return self._task_get(db, r, now)

    def _task_control(self, db, r, now):
        row = self._task_row(db, r.project, r.task_id)
        self._check_revision(row, r.expected_revision)
        if (
            r.action in ("resume", "retry", "cancel")
            and db.execute(
                "SELECT 1 FROM decisions WHERE project=? AND task_id=? AND status='pending'",
                (r.project, r.task_id),
            ).fetchone()
        ):
            raise CoordinationError("decision_required", "请先在待确认事项中回复用户决策")
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

    def _task_delete(self, db, r, now):
        """Permanently remove a task and every row that points at it.

        Foreign keys are enforced, so children go first. A running task is refused:
        its worker still holds a lease and would publish into a deleted row. Tasks
        other tasks depend on are refused too, because the dependency closure would
        otherwise resolve to a task that no longer exists.
        """
        row = self._task_row(db, r.project, r.task_id)
        self._check_revision(row, r.expected_revision)
        if row["status"] == "running" and (row["expires_at"] or 0) > now:
            raise CoordinationError(
                "task_running",
                "Cancel the task and wait for the process to exit before deleting it",
            )
        if db.execute(
            "SELECT 1 FROM decisions WHERE project=? AND task_id=? AND status='pending'",
            (r.project, r.task_id),
        ).fetchone():
            raise CoordinationError("decision_required", "请先处理该任务的待确认事项")
        dependents = [
            other
            for other, depends_on in db.execute(
                "SELECT task_id, depends_on FROM tasks WHERE project=? AND task_id<>?",
                (r.project, r.task_id),
            )
            if r.task_id in json.loads(depends_on or "[]")
        ]
        # A task can be referenced by a claim snapshot or by a meeting's waiting
        # list without appearing in depends_on. Both would dangle after deletion.
        for other, snapshot in db.execute(
            "SELECT task_id, task_snapshot FROM tasks WHERE project=? AND task_id<>?",
            (r.project, r.task_id),
        ):
            try:
                refs = json.loads(snapshot or "[]")
            except (TypeError, ValueError):
                continue
            if isinstance(refs, list) and any(
                isinstance(ref, dict) and ref.get("task_id") == r.task_id for ref in refs
            ):
                dependents.append(other)
        waiting_meetings = []
        for meeting_row in db.execute(
            "SELECT meeting_id, definition FROM meetings WHERE project=?", (r.project,)
        ):
            try:
                definition = json.loads(meeting_row["definition"])
            except (TypeError, ValueError):
                continue
            if isinstance(definition, dict) and r.task_id in (definition.get("waiting") or []):
                waiting_meetings.append(meeting_row["meeting_id"])
        if waiting_meetings:
            raise CoordinationError(
                "task_depended_on",
                "A meeting still waits for this task; finish or cancel the meeting first: "
                + ", ".join(sorted(waiting_meetings)),
            )
        if dependents:
            raise CoordinationError(
                "task_depended_on",
                "Other tasks depend on this task; delete them first: "
                + ", ".join(sorted(set(dependents))),
            )
        # Record before removing the assignment row: the audit entry must land
        # in the group that actually owned the task, not in whichever group the
        # member's role happens to resolve to after a transfer or group change.
        self._event(
            db,
            r,
            now,
            "task.deleted",
            r.task_id,
            {"previous_status": row["status"], "title": row["title"]},
        )
        db.execute(
            "DELETE FROM member_messages WHERE project=? AND task_id=?", (r.project, r.task_id)
        )
        db.execute(
            "DELETE FROM meeting_tasks WHERE project=? AND task_id=?", (r.project, r.task_id)
        )
        # Runtime activity belongs to the run, so it leaves with the task.
        db.execute(
            "DELETE FROM group_logs WHERE project=? AND task_id=? AND kind='run.activity'",
            (r.project, r.task_id),
        )
        db.execute("DELETE FROM tasks WHERE project=? AND task_id=?", (r.project, r.task_id))
        return {"task_id": r.task_id, "deleted": True}

    def _task_activity(self, db, r, now):
        """Accept activity reported by the dispatcher that is running the task.

        Only the dispatcher holds the vendor process, so it is the sole source for
        what a run is doing right now. Entries land in group_logs under one stable
        kind so the existing conversation view can render them in place.
        """
        row = self._task_row(db, r.project, r.task_id)
        if row["status"] == "cancelled":
            raise CoordinationError(
                "task_cancelled", "A cancelled task no longer accepts activity", status=409
            )
        # Agent activity is accepted only for the live attempt, just like a
        # heartbeat. Admin-only historical imports may omit the lease fields.
        agent = db.execute(
            "SELECT 1 FROM agents WHERE project=? AND agent_id=?", (r.project, r.actor)
        ).fetchone()
        if agent or r.fence is not None or r.lease_token is not None:
            if r.fence is None or r.lease_token is None:
                raise CoordinationError(
                    "lease_required", "Activity requires the current task lease", status=403
                )
            self._assert_lease(db, r, now)
        assignment = self._task_assignment(db, r.project, r.task_id)
        if r.member_id and assignment and assignment["member_id"] != r.member_id:
            raise CoordinationError(
                "assignment_mismatch", "Activity member does not own this task", status=403
            )
        group_id = self._activity_group(db, r.project, r.task_id)
        if not group_id:
            # Without a group there is nowhere to show it; accept quietly so a
            # standalone task does not fail its run over presentation.
            return {"task_id": r.task_id, "recorded": 0}
        recorded = 0
        for entry in r.entries:
            message = entry.title or entry.text
            self.record_db(
                db,
                r.project,
                group_id,
                "run.activity",
                message,
                level="error" if entry.kind == "error" else "info",
                member_id=assignment["member_id"] if assignment else None,
                task_id=r.task_id,
                details={
                    "kind": entry.kind,
                    "text": entry.text,
                    "status": entry.status,
                    "at": entry.at if entry.at is not None else now,
                    "source_agent": entry.source_agent,
                    "parent_agent": entry.parent_agent,
                },
                now=now,
            )
            recorded += 1
        return {"task_id": r.task_id, "recorded": recorded}

    def _task_activity_list(self, db, r, now):
        where = "project=? AND kind='run.activity' AND id>?"
        params: list = [r.project, r.after_id]
        if r.task_id:
            where += " AND task_id=?"
            params.append(r.task_id)
        params.append(r.limit + 1)
        rows = db.execute(
            "SELECT id,group_id,member_id,task_id,kind,level,message,details,created_at "
            f"FROM group_logs WHERE {where} ORDER BY id LIMIT ?",
            params,
        ).fetchall()
        page = rows[: r.limit]
        entries = []
        for row in page:
            try:
                details = json.loads(row["details"])
            except (TypeError, ValueError):
                details = {}
            if not isinstance(details, dict):
                details = {}
            entries.append(
                {
                    "id": row["id"],
                    "task_id": row["task_id"],
                    "source_agent": details.get("source_agent"),
                    "parent_agent": details.get("parent_agent"),
                    "group_id": row["group_id"],
                    "member_id": row["member_id"],
                    "kind": details.get("kind") or "note",
                    "status": details.get("status") or "completed",
                    "title": row["message"],
                    "text": details.get("text") or row["message"],
                    "at": details.get("at") or row["created_at"],
                }
            )
        return {
            "entries": entries,
            "next_after": page[-1]["id"] if page else r.after_id,
            "has_more": len(rows) > r.limit,
        }

    def _activity_group(self, db, project, task_id):
        assignment = self._task_assignment(db, project, task_id)
        return assignment["group_id"] if assignment else None

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
