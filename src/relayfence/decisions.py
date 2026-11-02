"""Durable human checkpoints; no work proceeds on an unanswered decision."""

import json
from types import SimpleNamespace

from .errors import CoordinationError


class Decisions:
    def _decision_create(self, db, r, now, meeting_id, task_id, kind, question, task_revision=None):
        ordinal = db.execute(
            "SELECT count(*) FROM decisions WHERE project=?", (r.project,)
        ).fetchone()[0]
        ident = (
            "decision-"
            + self.digest(f"{r.project}:{meeting_id}:{task_id}:{r.request_id}:{kind}:{ordinal}")[
                :32
            ]
        )
        db.execute(
            "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,1,?,NULL,?,?)",
            (
                r.project,
                ident,
                meeting_id,
                task_id,
                kind,
                self.encode(question),
                "pending",
                task_revision,
                now,
                now,
            ),
        )
        self._event(
            db,
            r,
            now,
            "decision.requested",
            task_id or meeting_id,
            {"decision_id": ident, "question": question["question"]},
        )
        if meeting_id and not task_id:
            meeting = json.loads(
                db.execute(
                    "SELECT definition FROM meetings WHERE project=? AND meeting_id=?",
                    (r.project, meeting_id),
                ).fetchone()[0]
            )
            for group in meeting["groups"]:
                self.record_db(
                    db,
                    r.project,
                    group["id"],
                    "decision.requested",
                    "等待用户确认",
                    details={"decision_id": ident, "question": question["question"]},
                    now=now,
                )
        return ident

    def _task_question(self, db, r, now):
        self._assert_lease(db, r, now)
        assignment = db.execute(
            "SELECT meeting_id FROM meeting_tasks WHERE project=? AND task_id=?",
            (r.project, r.task_id),
        ).fetchone()
        meeting_id = assignment["meeting_id"] if assignment and assignment["meeting_id"] else None
        self._task_control(
            db,
            SimpleNamespace(
                project=r.project,
                actor=r.actor,
                task_id=r.task_id,
                expected_revision=self._task_row(db, r.project, r.task_id)["revision"],
                action="pause",
                reason="等待用户确认",
            ),
            now,
        )
        paused = self._task_row(db, r.project, r.task_id)
        ident = self._decision_create(
            db,
            r,
            now,
            meeting_id,
            r.task_id,
            "task_question",
            r.question.model_dump(),
            paused["revision"],
        )
        if meeting_id:
            meeting = json.loads(
                db.execute(
                    "SELECT definition FROM meetings WHERE project=? AND meeting_id=?",
                    (r.project, meeting_id),
                ).fetchone()[0]
            )
            if not meeting.get("paused"):
                self._meeting_control(
                    db,
                    SimpleNamespace(
                        project=r.project,
                        actor=r.actor,
                        request_id=r.request_id,
                        meeting_id=meeting_id,
                        action="pause",
                    ),
                    now,
                )
                meeting = json.loads(
                    db.execute(
                        "SELECT definition FROM meetings WHERE project=? AND meeting_id=?",
                        (r.project, meeting_id),
                    ).fetchone()[0]
                )
                meeting["paused_for_decision"] = ident
                self._meeting_save(db, r, meeting)
        return {"decision_id": ident, "task": self._task_get(db, r, now), "waiting_user": True}

    def _decisions_list(self, db, r, now):
        result = []
        for row in db.execute(
            "SELECT * FROM decisions WHERE project=? ORDER BY (status='pending') DESC,created_at DESC LIMIT 100",
            (r.project,),
        ):
            item = dict(row)
            item["question"] = json.loads(item["question"])
            result.append(item)
        return {"decisions": result}

    def _decision_respond(self, db, r, now):
        row = db.execute(
            "SELECT * FROM decisions WHERE project=? AND decision_id=?", (r.project, r.decision_id)
        ).fetchone()
        if not row:
            raise CoordinationError("not_found", "确认事项不存在", status=404)
        self._check_revision(row, r.expected_revision)
        if row["status"] != "pending":
            raise CoordinationError("decision_answered", "该事项已经处理，请刷新")
        if (
            r.action in ("continue", "revise")
            and not r.answer.strip()
            and row["kind"] != "execution_gate"
        ):
            raise CoordinationError("answer_required", "请提供明确的回复")
        meeting = None
        if row["meeting_id"]:
            meeting = json.loads(
                db.execute(
                    "SELECT definition FROM meetings WHERE project=? AND meeting_id=?",
                    (r.project, row["meeting_id"]),
                ).fetchone()[0]
            )
        if row["task_id"]:
            task = self._task_row(db, r.project, row["task_id"])
            if r.action != "cancel" and (
                task["status"] != "paused" or task["revision"] != row["task_revision"]
            ):
                raise CoordinationError("task_changed", "等待期间任务已改变，请检查任务状态后处理")
            if r.action == "finish_discussion":
                raise CoordinationError(
                    "invalid_transition", "请先让当前分析任务完成，再保存完整方案"
                )
            description = task["description"] + "\n\n用户确认与后续要求：\n" + r.answer.strip()
            if r.action != "cancel" and len(description) > 8000:
                raise CoordinationError("description_limit", "确认回复导致任务说明超长，请先精简")
        db.execute(
            "UPDATE decisions SET status=?,answer=?,revision=revision+1,updated_at=? WHERE project=? AND decision_id=?",
            (
                "cancelled" if r.action == "cancel" else "answered",
                r.answer,
                now,
                r.project,
                r.decision_id,
            ),
        )
        if r.action == "cancel":
            if meeting:
                for task in db.execute(
                    "SELECT t.* FROM tasks t JOIN meeting_tasks a ON t.project=a.project AND t.task_id=a.task_id WHERE a.project=? AND a.meeting_id=?",
                    (r.project, row["meeting_id"]),
                ).fetchall():
                    if task["status"] in ("ready", "running", "paused", "failed"):
                        self._task_control(
                            db,
                            SimpleNamespace(
                                project=r.project,
                                actor=r.actor,
                                task_id=task["task_id"],
                                expected_revision=task["revision"],
                                action="cancel",
                                reason="用户拒绝方案",
                            ),
                            now,
                        )
                meeting["stage"] = "cancelled"
                self._meeting_save(db, r, meeting)
            elif task["status"] in ("ready", "running", "paused", "failed"):
                self._task_control(
                    db,
                    SimpleNamespace(
                        project=r.project,
                        actor=r.actor,
                        task_id=row["task_id"],
                        expected_revision=task["revision"],
                        action="cancel",
                        reason="用户取消",
                    ),
                    now,
                )
        elif row["kind"] == "execution_gate":
            if not meeting or meeting["stage"] != "ready_for_execution":
                raise CoordinationError("invalid_transition", "会议阶段已经改变")
            if any(
                self._task_row(db, r.project, tid)["status"] != "done"
                or self._meeting_result(db, r.project, tid).get("can_complete") is not True
                for tid in meeting["waiting"]
            ):
                raise CoordinationError("stale_discussion", "组长评估已变化，请重新讨论")
            if r.action == "finish_discussion":
                meeting["stage"] = "discussion_done"
                self._meeting_save(db, r, meeting)
            elif r.action == "revise":
                if not r.answer.strip():
                    raise CoordinationError("answer_required", "请填写需要调整的内容")
                key = "decisions/" + r.decision_id + "/answer"
                self._write_item(db, r, now, key, "state", 1, {"answer": r.answer}, [])
                meeting["decision_inputs"] = [*meeting.get("decision_inputs", []), key][-4:]
                meeting["round"] += 1
                self._meeting_plan(db, r, now, meeting, meeting["waiting"])
                self._meeting_save(db, r, meeting)
            else:
                self._meeting_execute(
                    db,
                    SimpleNamespace(
                        project=r.project,
                        actor=r.actor,
                        request_id=r.request_id,
                        meeting_id=meeting["id"],
                    ),
                    now,
                )
        else:
            key = "decisions/" + r.decision_id + "/answer"
            self._write_item(
                db,
                r,
                now,
                key,
                "state",
                1,
                {"answer": r.answer, "question": json.loads(row["question"])},
                [],
            )
            inputs = [*json.loads(task["inputs"]), key]
            if len(inputs) > 32:
                raise CoordinationError("input_limit", "任务输入已满，无法追加确认回复")
            db.execute(
                "UPDATE tasks SET description=?,inputs=? WHERE project=? AND task_id=?",
                (description, self.encode(inputs), r.project, row["task_id"]),
            )
            self._task_control(
                db,
                SimpleNamespace(
                    project=r.project,
                    actor=r.actor,
                    task_id=row["task_id"],
                    expected_revision=task["revision"],
                    action="resume",
                    reason="用户已确认，重新执行",
                ),
                now,
            )
            if meeting and meeting.get("paused_for_decision") == r.decision_id:
                self._meeting_control(
                    db,
                    SimpleNamespace(
                        project=r.project,
                        actor=r.actor,
                        request_id=r.request_id,
                        meeting_id=meeting["id"],
                        action="resume",
                    ),
                    now,
                )
                meeting = json.loads(
                    db.execute(
                        "SELECT definition FROM meetings WHERE project=? AND meeting_id=?",
                        (r.project, meeting["id"]),
                    ).fetchone()[0]
                )
                meeting.pop("paused_for_decision", None)
                self._meeting_save(db, r, meeting)
        self._event(
            db,
            r,
            now,
            "decision.responded",
            row["task_id"] or row["meeting_id"],
            {"decision_id": r.decision_id, "action": r.action, "answer": r.answer},
        )
        if meeting and not row["task_id"]:
            for group in meeting["groups"]:
                self.record_db(
                    db,
                    r.project,
                    group["id"],
                    "decision.responded",
                    "用户已回复确认事项",
                    details={"decision_id": r.decision_id, "action": r.action, "answer": r.answer},
                    now=now,
                )
        return {
            "decision_id": r.decision_id,
            "status": "cancelled" if r.action == "cancel" else "answered",
        }
