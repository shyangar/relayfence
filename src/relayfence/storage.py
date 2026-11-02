"""SQLite-backed local project registry and group log analytics.

The coordination ledger already owns project business data.  This module keeps the
remaining local application data (the project picker and per-group operational
logs) in the same SQLite database, so a project directory has one durable store
instead of a mix of JSON side files and database rows.
"""

import hashlib
import json
import os
from pathlib import Path

LOG_LEVELS = ("debug", "info", "warning", "error")
MAX_LOG_MESSAGE = 2000
MAX_LOG_DETAILS = 12000
MAX_LEGACY_PROJECTS = 1000
_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "lease_token",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_token",
}
_EVENT_LABELS = {
    "decision.requested": ("warning", "等待用户确认"),
    "decision.responded": ("info", "用户已回复确认事项"),
    "task.created": ("info", "任务已派发"),
    "task.updated": ("info", "任务说明已更新"),
    "task.assigned": ("info", "任务已分配"),
    "task.claimed": ("info", "成员开始执行"),
    "task.released": ("warning", "执行已释放"),
    "task.completed": ("info", "任务已完成"),
    "task.reviewed": ("info", "交付验收已处理"),
    "task.failed": ("error", "任务执行失败"),
    "task.pause": ("warning", "任务已暂停"),
    "task.resume": ("info", "任务已恢复排队"),
    "task.cancel": ("warning", "任务已取消"),
    "task.retry": ("info", "任务已重新排队"),
    "task.reopened": ("info", "任务已重新打开"),
    "task.deleted": ("warning", "任务已删除"),
    "office.transferred": ("info", "任务已交接"),
    "office.handoff": ("info", "任务已安全交接"),
}
_SKIPPED_EVENTS = {"runner.heartbeat", "task.heartbeat", "task.guidance"}


def _clean_detail(value, depth=0):
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {
            str(key): _clean_detail(item, depth + 1)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_clean_detail(item, depth + 1) for item in value[:100]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def safe_details(value):
    """Return a bounded, JSON-serialisable copy without credential-like fields."""
    if not isinstance(value, dict):
        return {}
    clean = _clean_detail(value)
    try:
        text = json.dumps(clean, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return {}
    if len(text) > MAX_LOG_DETAILS:
        return {"truncated": True, "preview": text[:MAX_LOG_DETAILS]}
    return clean


def project_id(path):
    """Match LocalWorkspaceManager's stable project identity."""
    return "local-" + hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest()[:24]


class ProjectStore:
    """Durable local project picker backed by ``local_projects`` in SQLite."""

    def __init__(self, store, directory):
        self.store = store
        self.directory = Path(directory)

    def migrate_legacy(self):
        """Import the old projects.json once, then stop reading that side file."""
        path = self.directory / "projects.json"
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        except OSError:
            return
        if size > 1_048_576:
            raise ValueError("Project list is too large")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        if not isinstance(data, dict):
            return
        if len(data) > MAX_LEGACY_PROJECTS:
            # Refuse instead of silently importing a truncated prefix and then
            # deleting the only copy of the remaining entries.
            raise ValueError("Project list is too large")
        imported = []
        for item in data.values():
            if not isinstance(item, dict):
                continue
            folder = item.get("path")
            if not isinstance(folder, str):
                continue
            try:
                resolved = Path(folder).expanduser()
                if not resolved.is_absolute():
                    continue
                resolved = resolved.resolve()
            except OSError:
                continue
            ident = project_id(resolved)
            imported.append((ident, str(resolved), resolved.name or str(resolved)))
        if not imported:
            return
        now = self.store.clock()
        with self.store.connection() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                for ident, folder, name in imported:
                    db.execute(
                        "INSERT INTO local_projects(project,path,name,created_at,updated_at) "
                        "VALUES (?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                        "name=excluded.name,updated_at=excluded.updated_at",
                        (ident, folder, name, now, now),
                    )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        # Remove the legacy file only when every entry was imported. A partial
        # import keeps it so the remaining paths are not lost.
        if len(imported) == len(data):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # The rows are already durable; a read-only legacy file is
                # re-imported idempotently on the next start instead of aborting.
                pass

    def as_dict(self):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT project,path,name FROM local_projects ORDER BY name COLLATE NOCASE,path"
            ).fetchall()
        return {
            row["project"]: {"id": row["project"], "name": row["name"], "path": row["path"]}
            for row in rows
        }

    def upsert(self, ident, path, name):
        now = self.store.clock()
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO local_projects(project,path,name,created_at,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET "
                "path=excluded.path,name=excluded.name,updated_at=excluded.updated_at",
                (ident, path, name, now, now),
            )


class GroupLogs:
    """Transactional per-group operational log, with cursor pagination/analysis."""

    def record_db(
        self,
        db,
        project,
        group_id,
        kind,
        message,
        *,
        level="info",
        member_id=None,
        task_id=None,
        details=None,
        now=None,
    ):
        if not group_id:
            return None
        level = level if level in LOG_LEVELS else "info"
        text = str(message or "")[:MAX_LOG_MESSAGE]
        payload = safe_details(details or {})
        cursor = db.execute(
            "INSERT INTO group_logs(project,group_id,member_id,task_id,kind,level,message,details,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                project,
                group_id,
                member_id,
                task_id,
                str(kind or "event")[:128],
                level,
                text,
                self.encode(payload),
                self.clock() if now is None else now,
            ),
        )
        return cursor.lastrowid

    def record(self, project, group_id, kind, message, **kwargs):
        with self.connection() as db:
            return self.record_db(db, project, group_id, kind, message, **kwargs)

    @staticmethod
    def _task_assignment(db, project, task_id):
        row = db.execute(
            "SELECT group_id,member_id FROM meeting_tasks WHERE project=? AND task_id=?",
            (project, task_id),
        ).fetchone()
        if row is not None and row["group_id"]:
            return row
        # Independent office tasks have no meeting_tasks row. Their role id
        # still identifies the owning member, so their lifecycle must land in
        # the same group conversation as meeting-dispatched work.
        task = db.execute(
            "SELECT role_id FROM tasks WHERE project=? AND task_id=?",
            (project, task_id),
        ).fetchone()
        role = (task["role_id"] or "") if task is not None else ""
        if not role.startswith("office/"):
            return None
        config = db.execute(
            "SELECT definition FROM office_configs WHERE project=?", (project,)
        ).fetchone()
        if config is None:
            return None
        try:
            groups = json.loads(config["definition"])
        except (TypeError, ValueError):
            return None
        if not isinstance(groups, list):
            return None
        for group in groups:
            if not isinstance(group, dict):
                continue
            for member in group.get("members") or []:
                member_id = member.get("id") if isinstance(member, dict) else None
                if not isinstance(member_id, str) or not member_id:
                    continue
                candidate = "office/" + (
                    member_id
                    if len(member_id) <= 121
                    else hashlib.sha256(member_id.encode("utf-8")).hexdigest()[:32]
                )
                if candidate == role:
                    return {"group_id": group.get("id"), "member_id": member_id}
        return None

    def group_for_member(self, db, project, member_id):
        row = db.execute(
            "SELECT group_id FROM meeting_tasks WHERE project=? AND member_id=? "
            "ORDER BY rowid DESC LIMIT 1",
            (project, member_id),
        ).fetchone()
        if row and row["group_id"]:
            return row["group_id"]
        try:
            config = json.loads(
                db.execute(
                    "SELECT definition FROM office_configs WHERE project=?", (project,)
                ).fetchone()["definition"]
            )
        except (AttributeError, TypeError, ValueError):
            return None
        for group in config:
            if any(member.get("id") == member_id for member in group.get("members", [])):
                return group.get("id")
        return None

    def record_event(self, db, project, kind, subject, details, now):
        """Mirror a task event into its owning group's durable log.

        Meeting assignments win; an independent office task is resolved from
        its ``office/`` role so its conversation remains complete.
        """
        if kind in _SKIPPED_EVENTS:
            return
        assignment = self._task_assignment(db, project, subject)
        if assignment is None or not assignment["group_id"]:
            return
        level, default_message = _EVENT_LABELS.get(kind, ("info", kind))
        detail = safe_details(details if isinstance(details, dict) else {})
        if kind == "task.completed" and detail.get("review_required"):
            default_message = "交付已提交，等待验收"
        if kind == "task.reviewed":
            default_message = (
                "交付验收通过：" if detail.get("status") == "approved" else "交付已驳回并暂停："
            ) + str(detail.get("note", ""))
        if kind == "task.failed":
            level = "error"
        elif kind in {"task.pause", "task.cancel", "task.released"}:
            level = "warning"
        self.record_db(
            db,
            project,
            assignment["group_id"],
            kind,
            default_message,
            level=level,
            member_id=assignment["member_id"],
            task_id=subject,
            details=detail,
            now=now,
        )

    def _group_logs_list(self, db, r, now):
        where = ["project=?"]
        params = [r.project]
        if r.group_id:
            where.append("group_id=?")
            params.append(r.group_id)
        if r.before:
            where.append("id<?")
            params.append(r.before)
        if r.level:
            where.append("level=?")
            params.append(r.level)
        if r.query:
            where.append(
                "instr(lower(message || ' ' || kind || ' ' || coalesce(member_id,'') || "
                "' ' || coalesce(task_id,'')),lower(?))>0"
            )
            params.append(r.query)
        rows = db.execute(
            "SELECT * FROM group_logs WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT ?",
            [*params, r.limit + 1],
        ).fetchall()
        logs = []
        for row in rows[: r.limit]:
            item = dict(row)
            try:
                item["details"] = json.loads(item["details"])
            except (TypeError, ValueError):
                item["details"] = {}
            logs.append(item)
        has_more = len(rows) > r.limit
        return {
            "logs": logs,
            "next_cursor": logs[-1]["id"] if has_more and logs else None,
            "has_more": has_more,
            "analysis": self._log_analysis(db, r.project, r.group_id, now),
        }

    def _log_analysis(self, db, project, group_id, now):
        group_filter = " AND group_id=?" if group_id else ""
        group_params = [project] + ([group_id] if group_id else [])

        by_level = {
            row["level"]: row["count"]
            for row in db.execute(
                "SELECT level,count(*) AS count FROM group_logs WHERE project=?"
                + group_filter
                + " GROUP BY level",
                group_params,
            )
        }
        by_kind = [
            {"kind": row["kind"], "count": row["count"]}
            for row in db.execute(
                "SELECT kind,count(*) AS count FROM group_logs WHERE project=?"
                + group_filter
                + " GROUP BY kind ORDER BY count DESC,kind LIMIT 20",
                group_params,
            )
        ]
        by_member = [
            {"member_id": row["member_id"], "count": row["count"]}
            for row in db.execute(
                "SELECT member_id,count(*) AS count FROM group_logs WHERE project=?"
                + group_filter
                + " AND member_id IS NOT NULL GROUP BY member_id ORDER BY count DESC LIMIT 20",
                group_params,
            )
        ]
        by_group = [
            {"group_id": row["group_id"], "count": row["count"]}
            for row in db.execute(
                "SELECT group_id,count(*) AS count FROM group_logs WHERE project=? "
                "GROUP BY group_id ORDER BY count DESC,group_id LIMIT 50",
                (project,),
            )
        ]

        assignment_filter = " AND mt.group_id=?" if group_id else ""
        assignment_params = [project] + ([group_id] if group_id else [])
        status_rows = db.execute(
            "SELECT t.status,count(*) AS count,"
            "sum(CASE WHEN t.status='done' THEN max(0,t.updated_at-t.created_at) ELSE 0 END) AS elapsed "
            "FROM tasks t JOIN meeting_tasks mt ON mt.project=t.project AND mt.task_id=t.task_id "
            "WHERE t.project=?" + assignment_filter + " GROUP BY t.status",
            assignment_params,
        ).fetchall()
        task_status = {row["status"]: row["count"] for row in status_rows}
        done_elapsed = sum(row["elapsed"] or 0 for row in status_rows if row["status"] == "done")
        done_count = task_status.get("done", 0)
        latest = db.execute(
            "SELECT max(id) AS id,max(created_at) AS created_at FROM group_logs WHERE project=?"
            + group_filter,
            group_params,
        ).fetchone()
        recent_errors = []
        for row in db.execute(
            "SELECT id,group_id,member_id,task_id,kind,message,created_at FROM group_logs "
            "WHERE project=?"
            + group_filter
            + " AND level IN ('error','warning') ORDER BY id DESC LIMIT 8",
            group_params,
        ):
            recent_errors.append(dict(row))
        return {
            "generated_at": now,
            "total_logs": sum(by_level.values()),
            "by_level": by_level,
            "by_kind": by_kind,
            "by_member": by_member,
            "by_group": by_group,
            "tasks": {
                "total": sum(task_status.values()),
                "status": task_status,
                "avg_done_seconds": round(done_elapsed / done_count, 2) if done_count else None,
            },
            "latest_log_id": latest["id"] if latest else None,
            "latest_at": latest["created_at"] if latest else None,
            "recent_alerts": recent_errors,
        }
