"""Read-only group conversation projection over durable office logs.

The office runtime does not invent assistant dialogue: this feed mirrors real
guidance, lifecycle events and published task results. Every entry is backed by
a committed ``group_logs`` row so incremental polling and pagination remain
stable across page reloads.
"""

import json

from .errors import CoordinationError

MAX_RESULT_CHARS = 4000
_MAX_CURSOR = 2**63 - 1
_STATUS_LABELS = {
    "pending": "等待旧执行退出",
    "applied": "已应用",
    "failed": "未能应用",
    "received": "已收到",
}


def _content(value):
    """Prefer a published result's human summary, then stable JSON."""
    if isinstance(value, dict):
        summary = value.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


def _output_refs(value):
    """Return only well-formed published-output references."""
    if not isinstance(value, list):
        return []
    refs = []
    for item in value:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        version = item.get("version")
        if (
            isinstance(key, str)
            and key
            and isinstance(version, int)
            and not isinstance(version, bool)
            and version > 0
        ):
            refs.append((key, version))
    return refs


class GroupChat:
    def __init__(self, service):
        self.store = service.store

    @staticmethod
    def _bounded(value, limit):
        text = str(value or "")
        if len(text) <= limit:
            return text, False
        return text[:limit].rstrip() + " …", True

    def _group(self, db, project, group_id):
        try:
            config = self.store._office_config(db, project)
        except (AttributeError, TypeError, ValueError):
            config = {"groups": []}
        groups = config.get("groups") if isinstance(config, dict) else None
        for group in groups if isinstance(groups, list) else []:
            if isinstance(group, dict) and group.get("id") == group_id:
                return group
        raise CoordinationError("not_found", "小组不存在", status=404)

    def _messages(self, db, project, group, rows):
        group_members = group.get("members")
        if not isinstance(group_members, list):
            group_members = []
        members = {
            member.get("id"): member
            for member in group_members
            if isinstance(member, dict) and member.get("id")
        }
        task_ids = {row["task_id"] for row in rows if row["task_id"]}
        tasks = {}
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            for task in db.execute(
                f"SELECT task_id,title,status FROM tasks "
                f"WHERE project=? AND task_id IN ({placeholders})",
                [project, *sorted(task_ids)],
            ):
                tasks[task["task_id"]] = task
        # Member messages carry the live guidance status, but task deletion
        # removes them. The follow-up outcome log lets retained guidance keep
        # its final applied/failed state instead of looking pending forever.
        guidance_tasks = {
            row["task_id"] for row in rows if row["kind"] == "chat.guidance" and row["task_id"]
        }
        outcome_status = {}
        if guidance_tasks:
            placeholders = ",".join("?" for _ in guidance_tasks)
            for outcome in db.execute(
                f"SELECT task_id,kind,details FROM group_logs WHERE project=? "
                f"AND task_id IN ({placeholders}) "
                f"AND kind IN ('chat.applied','chat.failed') ORDER BY id",
                [project, *sorted(guidance_tasks)],
            ):
                try:
                    outcome_details = json.loads(outcome["details"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(outcome_details, dict):
                    continue
                message_id = outcome_details.get("message_id")
                if (
                    not isinstance(message_id, int)
                    or isinstance(message_id, bool)
                    or message_id <= 0
                ):
                    continue
                outcome_status[(outcome["task_id"], message_id)] = (
                    "applied" if outcome["kind"] == "chat.applied" else "failed"
                )
        # Deleted tasks are deliberately absent from ``tasks``, but their
        # deletion event keeps the title so the retained conversation remains
        # attributable instead of showing only an opaque id.
        deleted_titles = {}
        missing = sorted(task_ids - tasks.keys())
        if missing:
            placeholders = ",".join("?" for _ in missing)
            for event in db.execute(
                f"SELECT subject,details FROM events WHERE project=? "
                f"AND kind='task.deleted' AND subject IN ({placeholders}) "
                f"ORDER BY seq DESC",
                [project, *missing],
            ):
                try:
                    event_details = json.loads(event["details"])
                except (TypeError, ValueError):
                    continue
                title = event_details.get("title") if isinstance(event_details, dict) else None
                if isinstance(title, str) and title:
                    deleted_titles.setdefault(event["subject"], title)
        messages = []
        for row in rows:
            details = {}
            try:
                details = json.loads(row["details"])
            except (TypeError, ValueError):
                pass
            if not isinstance(details, dict):
                details = {}
            task = tasks.get(row["task_id"])
            member = members.get(row["member_id"]) or {}
            kind = row["kind"]
            role = "system"
            chat_kind = "lifecycle"
            content = row["message"]
            status = details.get("status")
            if not isinstance(status, str):
                status = None
            truncated = False
            if kind == "chat.guidance":
                role = "user"
                chat_kind = "guidance"
                # Prefer the full member message. New logs carry the durable
                # message id (which disambiguates two guidance rows written in
                # the same clock tick) and a full fallback copy, so retained
                # history survives task deletion.
                full = None
                message_id = details.get("message_id")
                if (
                    isinstance(message_id, int)
                    and not isinstance(message_id, bool)
                    and message_id > 0
                ):
                    full = db.execute(
                        "SELECT content,status FROM member_messages "
                        "WHERE project=? AND id=? AND member_id=? AND task_id=?",
                        (project, message_id, row["member_id"], row["task_id"]),
                    ).fetchone()
                if full is None:
                    full = db.execute(
                        "SELECT content,status FROM member_messages WHERE project=? AND member_id=? "
                        "AND task_id=? AND created_at=? ORDER BY id DESC LIMIT 1",
                        (project, row["member_id"], row["task_id"], row["created_at"]),
                    ).fetchone()
                if full is None:
                    full = db.execute(
                        "SELECT content,status FROM member_messages WHERE project=? AND member_id=? "
                        "AND task_id=? ORDER BY id DESC LIMIT 1",
                        (project, row["member_id"], row["task_id"]),
                    ).fetchone()
                if full is not None:
                    content = full["content"]
                    status = full["status"]
                else:
                    outcome = outcome_status.get((row["task_id"], message_id))
                    if outcome:
                        status = outcome
                    content = details.get("content") or row["message"]
                content, truncated = self._bounded(content, 6000)
            elif kind == "task.completed":
                role = "assistant"
                chat_kind = "result"
                # A task can be reopened and completed again. Read the outputs
                # published by *this* completion rather than the task's current
                # outputs, otherwise history would rewrite itself.
                refs = _output_refs(details.get("outputs"))
                if not refs and row["task_id"]:
                    event = db.execute(
                        "SELECT details FROM events WHERE project=? AND subject=? "
                        "AND kind='task.completed' AND created_at=? "
                        "ORDER BY seq DESC LIMIT 1",
                        (project, row["task_id"], row["created_at"]),
                    ).fetchone()
                    if event is not None:
                        try:
                            event_details = json.loads(event["details"])
                        except (TypeError, ValueError):
                            event_details = {}
                        if isinstance(event_details, dict):
                            refs = _output_refs(event_details.get("outputs"))
                # Do not fall back to the task's current outputs: a later
                # revision would rewrite the historical completion shown here.
                published = []
                for key, version in refs:
                    revision = db.execute(
                        "SELECT value FROM revisions WHERE project=? AND key=? AND version=?",
                        (project, key, version),
                    ).fetchone()
                    if revision is None:
                        continue
                    try:
                        value = json.loads(revision["value"])
                    except (TypeError, ValueError):
                        value = revision["value"]
                    published.append(_content(value))
                if published:
                    content, truncated = self._bounded(
                        "\n\n---\n\n".join(published), MAX_RESULT_CHARS
                    )
                else:
                    content = row["message"]
            elif kind in ("decision.requested", "decision.responded"):
                chat_kind = "decision"
                role = "user" if kind == "decision.responded" else "assistant"
                content = (
                    details.get("question")
                    if kind == "decision.requested"
                    else details.get("answer")
                    or {
                        "continue": "确认继续执行",
                        "finish_discussion": "仅保存方案，结束讨论",
                        "revise": "补充要求后重新讨论",
                        "cancel": "取消任务",
                    }.get(details.get("action"), "已处理确认事项")
                )
                content, truncated = self._bounded(content, 4000)
            elif kind == "run.activity":
                # Reported by the dispatcher while the adapter is still working.
                role = "assistant"
                chat_kind = "activity"
                content = details.get("text") or row["message"]
                content, truncated = self._bounded(content, 4000)
                status = details.get("status")
                if not isinstance(status, str) or not status:
                    status = "completed"
            elif kind == "task.failed":
                role = "system"
                chat_kind = "failure"
                failure = details.get("message")
                content = failure if isinstance(failure, str) and failure else row["message"]
                content, truncated = self._bounded(content, 2000)
            elif kind in {"chat.applied", "chat.failed"}:
                chat_kind = "guidance-status"
                content = details.get("detail") or row["message"]
                content, truncated = self._bounded(content, 2000)
            elif kind.startswith("dispatch."):
                chat_kind = "dispatch"
            else:
                content, truncated = self._bounded(content, 2000)
            guidance_id = None
            if kind in ("chat.guidance", "chat.applied", "chat.failed"):
                candidate = details.get("message_id")
                if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
                    guidance_id = candidate
            messages.append(
                {
                    "id": f"log-{row['id']}",
                    "log_id": row["id"],
                    "role": role,
                    "kind": chat_kind,
                    "event_kind": kind,
                    "guidance_id": guidance_id,
                    "content": content,
                    "status": status,
                    "status_label": (
                        {"completed": "已完成", "failed": "失败"}.get(status, "")
                        if chat_kind == "activity" and isinstance(status, str)
                        else _STATUS_LABELS.get(status, "")
                        if isinstance(status, str)
                        else ""
                    ),
                    "level": row["level"],
                    "entry_kind": (
                        details.get("kind")
                        if kind == "run.activity" and isinstance(details.get("kind"), str)
                        else ""
                    ),
                    "source_agent": details.get("source_agent"),
                    "parent_agent": details.get("parent_agent"),
                    "member_id": row["member_id"],
                    "member_name": member.get("name") or row["member_id"] or "",
                    "task_id": row["task_id"],
                    "task_title": (
                        task["title"]
                        if task is not None
                        else deleted_titles.get(row["task_id"], "")
                    ),
                    "task_status": task["status"] if task is not None else "",
                    "created_at": row["created_at"],
                    "truncated": truncated,
                }
            )
        return messages

    def history(self, project, group_id, after=None, before=None, limit=100):
        if not isinstance(project, str) or not project.strip() or len(project) > 200:
            raise ValueError("无效的项目")
        if not isinstance(group_id, str) or not group_id.strip() or len(group_id) > 200:
            raise ValueError("无效的小组")
        if after is not None and before is not None:
            raise ValueError("增量读取和加载更早记录不能同时使用")
        for name, value in (("after", after), ("before", before)):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > _MAX_CURSOR
            ):
                raise ValueError(f"无效的 {name} 游标")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("对话分页大小必须在 1–200 之间")
        with self.store.connection() as db:
            group = self._group(db, project, group_id)
            where = ["project=?", "group_id=?"]
            params = [project, group_id]
            if after is not None:
                where.append("id>?")
                params.append(after)
                order, reverse = "ASC", False
            else:
                if before is not None:
                    where.append("id<?")
                    params.append(before)
                order, reverse = "DESC", True
            rows = db.execute(
                "SELECT * FROM group_logs WHERE "
                + " AND ".join(where)
                + f" ORDER BY id {order} LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
            page = rows[:limit]
            has_more = len(rows) > limit
            if reverse:
                page = list(reversed(page))
            messages = self._messages(db, project, group, page)
        group_members = group.get("members")
        return {
            "project": project,
            "group": {
                "id": group.get("id"),
                "name": group.get("name") or group.get("id"),
                "software": group.get("software") or "",
                "member_count": (len(group_members) if isinstance(group_members, list) else 0),
            },
            "messages": messages,
            "has_more": has_more,
            "next_before": page[0]["id"] if reverse and has_more and page else None,
            "next_after": max((row["id"] for row in page), default=(after or 0)),
            "server_time": self.store.clock(),
        }
