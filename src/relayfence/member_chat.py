"""Persistent member guidance, backed by real task state and fenced restarts."""

import json
import math
import threading
from types import SimpleNamespace

from .errors import CoordinationError

# Keep headroom below storage.MAX_LOG_DETAILS (12000) so the JSON-escaped
# content cannot turn the whole details object into its lossy preview form.
_MAX_GUIDANCE_LOG_DETAILS = 11000


def _guidance_log_details(status, content, message_id):
    """Fit a guidance copy into the durable log while keeping its metadata."""
    content = content[:6000]
    cut = False
    while True:
        details = {
            "status": status,
            "content": content + (" …" if cut else ""),
            "message_id": message_id,
        }
        if (
            len(json.dumps(details, ensure_ascii=False, allow_nan=False))
            <= _MAX_GUIDANCE_LOG_DETAILS
        ):
            return details
        if len(content) <= 1:
            return {"status": status, "message_id": message_id}
        content = content[: len(content) // 2]
        cut = True


class MemberChat:
    def __init__(self, service, manager):
        self.store = service.store
        self.manager = manager

    def recover_pending(self):
        """On server startup, preserve paused tasks instead of assuming children exited."""
        detail = "服务已重启，旧执行退出状态未确认；请检查暂停任务后重新发送指导"
        with self.store.connection() as db:
            now = self.store.clock()
            pending = db.execute(
                "SELECT id,project,member_id,task_id FROM member_messages WHERE status='pending'"
            ).fetchall()
            db.execute(
                "UPDATE member_messages SET status='failed',detail=?,updated_at=? WHERE status='pending'",
                (detail, now),
            )
            # Keep the group conversation as informative as the member chat:
            # a recovered send must show why it was not applied, not just a
            # failed status pill.
            for message in pending:
                group_id = self.store.group_for_member(db, message["project"], message["member_id"])
                if not group_id:
                    continue
                self.store.record_db(
                    db,
                    message["project"],
                    group_id,
                    "chat.failed",
                    "成员引导未能应用",
                    level="error",
                    member_id=message["member_id"],
                    task_id=message["task_id"],
                    details={"detail": detail, "message_id": message["id"]},
                    now=now,
                )

    def _role(self, db, project, member_id):
        config = self.store._office_config(db, project)
        if not any(m["id"] == member_id for g in config["groups"] for m in g["members"]):
            raise CoordinationError("not_found", "成员不存在", status=404)
        return "office/" + (
            member_id if len(member_id) <= 121 else self.store.digest(member_id)[:32]
        )

    @staticmethod
    def _message(row):
        return {
            "id": f"guidance-{row['id']}",
            "role": "user",
            "kind": "guidance",
            "task_id": row["task_id"],
            "content": row["content"],
            "status": row["status"],
            "detail": row["detail"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _cursor(created_at, ident):
        return f"{float(created_at):.9f}:{ident}"

    @staticmethod
    def _parse_cursor(before):
        """Accept a legacy timestamp or the opaque ``timestamp:id`` cursor."""
        if before is None:
            return None
        if isinstance(before, bool):
            raise ValueError("对话分页游标无效")
        if isinstance(before, (int, float)):
            try:
                timestamp = float(before)
            except (OverflowError, ValueError) as exc:
                raise ValueError("对话分页游标无效") from exc
            if not math.isfinite(timestamp) or timestamp < 0:
                raise ValueError("对话分页游标无效")
            return timestamp, ""
        if isinstance(before, str):
            if len(before) > 256:
                raise ValueError("对话分页游标无效")
            timestamp, separator, ident = before.partition(":")
            if not separator or not ident:
                raise ValueError("对话分页游标无效")
            try:
                timestamp = float(timestamp)
            except (OverflowError, ValueError) as exc:
                raise ValueError("对话分页游标无效") from exc
            if not math.isfinite(timestamp) or timestamp < 0 or len(ident) > 200:
                raise ValueError("对话分页游标无效")
            return timestamp, ident
        raise ValueError("对话分页游标无效")

    def history(self, project, member_id, before=None, limit=100):
        if not isinstance(project, str) or not project.strip() or len(project) > 200:
            raise ValueError("无效的项目")
        if not isinstance(member_id, str) or not member_id.strip() or len(member_id) > 200:
            raise ValueError("无效的成员")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("对话分页大小必须在 1–200 之间")
        cursor = self._parse_cursor(before)
        with self.store.connection() as db:
            # Task state and guidance status must come from one read snapshot:
            # the background continuation commits both in one transaction.
            db.execute("BEGIN")
            role = self._role(db, project, member_id)
            task_rows = db.execute(
                "SELECT * FROM tasks WHERE project=? AND ((role_id=? AND NOT EXISTS (SELECT 1 FROM meeting_tasks mt WHERE mt.project=tasks.project AND mt.task_id=tasks.task_id)) OR task_id IN (SELECT task_id FROM meeting_tasks WHERE project=? AND member_id=?)) ORDER BY created_at DESC,task_id DESC LIMIT 201",
                (project, role, project, member_id),
            ).fetchall()
            tasks_truncated = len(task_rows) > 200
            rows = task_rows[:200]
            tasks = [self.store._task_view(db, project, r, self.store.clock()) for r in rows]
            guidance_rows = db.execute(
                "SELECT * FROM member_messages WHERE project=? AND member_id=? "
                "ORDER BY created_at DESC,id DESC LIMIT 5001",
                (project, member_id),
            ).fetchall()
            messages_truncated = len(guidance_rows) > 5000
            messages = [self._message(r) for r in guidance_rows[:5000]]
            if rows:
                ids = [row["task_id"] for row in rows]
                placeholders = ",".join("?" for _ in ids)
                activity_rows = db.execute(
                    f"SELECT id,task_id,details,created_at FROM group_logs WHERE project=? AND kind='run.activity' AND task_id IN ({placeholders}) ORDER BY id DESC LIMIT 5001",
                    [project, *ids],
                ).fetchall()
                messages_truncated = messages_truncated or len(activity_rows) > 5000
                for activity in activity_rows[:5000]:
                    detail = json.loads(activity["details"])
                    messages.append(
                        {
                            "id": f"activity-{activity['id']}",
                            "role": "assistant",
                            "kind": "activity",
                            "entry_kind": detail.get("kind", "note"),
                            "content": detail.get("text", ""),
                            "status": detail.get("status", "completed"),
                            "task_id": activity["task_id"],
                            "source_agent": detail.get("source_agent"),
                            "parent_agent": detail.get("parent_agent"),
                            "created_at": activity["created_at"],
                        }
                    )
            labels = {
                "task.created": "任务已创建",
                "task.claimed": "任务开始执行",
                "task.pause": "任务已暂停",
                "task.resume": "任务已恢复排队",
                "task.failed": "任务执行失败",
                "task.cancel": "任务已取消",
                "task.completed": "任务已完成",
                "task.retry": "任务已重新排队",
            }
            for row in rows:
                event_rows = db.execute(
                    "SELECT * FROM events WHERE project=? AND subject=? "
                    "ORDER BY seq DESC LIMIT 2001",
                    (project, row["task_id"]),
                ).fetchall()
                if len(event_rows) > 2000:
                    messages_truncated = True
                for event in event_rows[:2000]:
                    if event["kind"] in ("decision.requested", "decision.responded"):
                        detail = json.loads(event["details"])
                        messages.append(
                            {
                                "id": f"event-{event['seq']}",
                                "role": "user"
                                if event["kind"] == "decision.responded"
                                else "assistant",
                                "kind": "decision",
                                "content": detail.get("question")
                                or detail.get("answer")
                                or "确认事项已处理",
                                "task_id": row["task_id"],
                                "created_at": event["created_at"],
                            }
                        )
                        continue
                    if event["kind"] not in labels:
                        continue
                    messages.append(
                        {
                            "id": f"event-{event['seq']}",
                            "role": "system",
                            "kind": "lifecycle",
                            "content": labels[event["kind"]],
                            "task_id": row["task_id"],
                            "created_at": event["created_at"],
                        }
                    )
                # Only published ledger outputs are presented as member replies.
                for output in json.loads(row["outputs"]):
                    revision = db.execute(
                        "SELECT value,created_at FROM revisions WHERE project=? AND key=? AND version=?",
                        (project, output["key"], output["version"]),
                    ).fetchone()
                    if revision:
                        value = json.loads(revision["value"])
                        content = value.get("summary") if isinstance(value, dict) else None
                        if not isinstance(content, str):
                            content = json.dumps(value, ensure_ascii=False, indent=2)
                        messages.append(
                            {
                                "id": f"output-{row['task_id']}-{output['key']}-{output['version']}",
                                "role": "assistant",
                                "kind": "result",
                                "content": content,
                                "task_id": row["task_id"],
                                "created_at": revision["created_at"],
                                "artifact": output,
                            }
                        )
        messages.sort(key=lambda m: (m["created_at"], str(m["id"])))
        if cursor is not None:
            messages = [
                message
                for message in messages
                if (message["created_at"], str(message["id"])) < cursor
            ]
        has_more = len(messages) > limit or messages_truncated
        visible = messages[-limit:]
        return {
            "project": project,
            "member_id": member_id,
            "messages": visible,
            "has_more": has_more,
            "next_before": (
                self._cursor(visible[0]["created_at"], str(visible[0]["id"]))
                if has_more and visible
                else None
            ),
            "tasks": tasks,
            "tasks_truncated": tasks_truncated,
            "messages_truncated": messages_truncated,
        }

    def send(self, payload):
        project, member_id, task_id, content, request_id = (
            payload.get(k) for k in ("project", "member_id", "task_id", "message", "request_id")
        )
        if (
            not all(
                isinstance(v, str) and v.strip()
                for v in (project, member_id, task_id, content, request_id)
            )
            or len(content) > 6000
            or len(request_id) > 128
        ):
            raise ValueError("请选择成员任务并输入 1–6000 字指导，携带有效 request_id")
        with self.manager._lock:
            if self.manager._closed or (
                self.manager._project != project
                and not getattr(self.manager, "dispatcher_for", lambda p: None)(project)
            ):
                raise ValueError("请先连接该项目")
            dispatcher = (
                self.manager.dispatcher_for(project)
                if hasattr(self.manager, "dispatcher_for")
                else self.manager._dispatcher
            )
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                role = self._role(db, project, member_id)
                existing = db.execute(
                    "SELECT * FROM member_messages WHERE project=? AND member_id=? AND request_id=?",
                    (project, member_id, request_id),
                ).fetchone()
                if existing:
                    if existing["task_id"] != task_id or existing["content"] != content:
                        raise CoordinationError(
                            "idempotency_conflict", "request_id 已用于另一条指导"
                        )
                    return {"message": self._message(existing), "replayed": True}
                task = self.store._task_row(db, project, task_id)
                assigned = db.execute(
                    "SELECT member_id FROM meeting_tasks WHERE project=? AND task_id=?",
                    (project, task_id),
                ).fetchone()
                if (assigned and assigned["member_id"] != member_id) or (
                    not assigned and task["role_id"] != role
                ):
                    raise ValueError("该任务不属于所选成员")
                if task["status"] not in ("running", "ready", "paused"):
                    raise ValueError("该任务已结束；请先重新打开或创建任务")
                if db.execute(
                    "SELECT 1 FROM decisions WHERE project=? AND task_id=? AND status='pending'",
                    (project, task_id),
                ).fetchone():
                    raise CoordinationError(
                        "decision_required", "该任务正在等待确认，请先在决策收件箱中回复"
                    )
                if len(task["description"] + "\n\n用户追加指导：\n" + content) > 8000:
                    raise ValueError("累计任务说明超过 8000 字；请先编辑任务说明")
                if db.execute(
                    "SELECT 1 FROM member_messages WHERE project=? AND task_id=? AND status='pending'",
                    (project, task_id),
                ).fetchone():
                    raise ValueError("正在停止该任务的旧执行，请稍后再发送指导")
                now = self.store.clock()
                running = task["status"] == "running"
                if running:
                    if dispatcher is None or not hasattr(dispatcher, "interrupt_task"):
                        raise ValueError("当前运行器不支持安全中断，指导未发送")
                    request = SimpleNamespace(
                        project=project,
                        actor="local-office",
                        request_id=request_id,
                        task_id=task_id,
                        expected_revision=task["revision"],
                        action="pause",
                        reason="等待旧进程退出后应用成员指导",
                    )
                    self.store._task_control(db, request, now)
                status = "pending" if running else "applied"
                paused_revision = task["revision"] + 1 if running else None
                cursor = db.execute(
                    "INSERT INTO member_messages(project,member_id,task_id,request_id,content,status,paused_revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        project,
                        member_id,
                        task_id,
                        request_id,
                        content,
                        status,
                        paused_revision,
                        now,
                        now,
                    ),
                )
                ident = cursor.lastrowid
                group_id = self.store.group_for_member(db, project, member_id)
                self.store.record_db(
                    db,
                    project,
                    group_id,
                    "chat.guidance",
                    "收到成员引导，等待应用",
                    member_id=member_id,
                    task_id=task_id,
                    # Task deletion removes member_messages, so keep the
                    # guidance text in the durable log as well. The helper
                    # preserves status/message_id even when escaping a long
                    # input would otherwise exceed the log detail bound.
                    details=_guidance_log_details(status, content, ident),
                    now=now,
                )
                if not running:
                    self._append(db, project, task_id, content, now)
                db.commit()
                message = self._message(
                    db.execute("SELECT * FROM member_messages WHERE id=?", (ident,)).fetchone()
                )
            if running:
                # Capture the matching future before another task can be dispatched.
                try:
                    future = dispatcher.interrupt_task(project, task_id)
                except Exception:
                    future = None
                if future is None:
                    self._failed(ident, "旧执行退出状态无法确认；任务保持暂停，请检查运行器")
                    message["status"] = "failed"
                    message["detail"] = "旧执行退出状态无法确认；任务保持暂停，请检查运行器"
                else:
                    threading.Thread(
                        target=self._finish,
                        args=(ident, future),
                        name="relayfence-guidance",
                        daemon=True,
                    ).start()
        return {"message": message, "replayed": False}

    def _append(self, db, project, task_id, content, now):
        row = self.store._task_row(db, project, task_id)
        description = row["description"] + "\n\n用户追加指导：\n" + content
        if len(description) > 8000:
            raise ValueError("累计任务说明超过 8000 字；请先编辑任务说明")
        db.execute(
            "UPDATE tasks SET description=?,revision=revision+1,updated_at=? WHERE project=? AND task_id=?",
            (description, now, project, task_id),
        )
        self.store._event(
            db,
            SimpleNamespace(project=project, actor="local-office"),
            now,
            "task.guidance",
            task_id,
            {"message": content},
        )

    def _failed(self, ident, detail):
        with self.store.connection() as db:
            now = self.store.clock()
            updated = db.execute(
                "UPDATE member_messages SET status='failed',detail=?,updated_at=? WHERE id=? AND status='pending'",
                (detail, now, ident),
            )
            if updated.rowcount:
                message = db.execute(
                    "SELECT project,member_id,task_id FROM member_messages WHERE id=?", (ident,)
                ).fetchone()
                self.store.record_db(
                    db,
                    message["project"],
                    self.store.group_for_member(db, message["project"], message["member_id"]),
                    "chat.failed",
                    "成员引导未能应用",
                    level="error",
                    member_id=message["member_id"],
                    task_id=message["task_id"],
                    details={"detail": detail[:1000], "message_id": ident},
                    now=now,
                )

    def _finish(self, ident, future):
        try:
            try:
                future.result(timeout=60)
            except TimeoutError:
                self._failed(ident, "旧执行停止超时；任务保持暂停，请检查运行器")
                return
            except Exception:
                # A finished worker may report its revoked lease as an error.
                if not future.done():
                    raise
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                message = db.execute(
                    "SELECT * FROM member_messages WHERE id=?", (ident,)
                ).fetchone()
                if message["status"] != "pending":
                    return
                task = self.store._task_row(db, message["project"], message["task_id"])
                if task["status"] != "paused" or task["revision"] != message["paused_revision"]:
                    raise ValueError("停止期间任务已被修改；任务未自动恢复，请检查后重试")
                now = self.store.clock()
                self._append(db, message["project"], message["task_id"], message["content"], now)
                self.store._task_control(
                    db,
                    SimpleNamespace(
                        project=message["project"],
                        actor="local-office",
                        task_id=message["task_id"],
                        expected_revision=task["revision"] + 1,
                        action="resume",
                        reason="旧执行已退出，追加指导已应用",
                    ),
                    now,
                )
                db.execute(
                    "UPDATE member_messages SET status='applied',updated_at=? WHERE id=?",
                    (now, ident),
                )
                self.store.record_db(
                    db,
                    message["project"],
                    self.store.group_for_member(db, message["project"], message["member_id"]),
                    "chat.applied",
                    "成员引导已应用，任务继续执行",
                    member_id=message["member_id"],
                    task_id=message["task_id"],
                    details={"status": "applied", "message_id": ident},
                    now=now,
                )
                db.commit()
        except Exception as exc:
            self._failed(ident, str(exc)[:500])
