"""Transactional office configuration, briefings and explicit quota handoffs.

The office is a projection of real registry identities, never a simulated runner.
Saving a character does not spawn software or assert native child relationships.
"""

import json

from .errors import CoordinationError
from .models import OfficePut, RolePut, Selection, TaskCreate


class Office:
    def _office_activity(self, db, r, now):
        config = self._office_config(db, r.project)
        members = []
        for group in config["groups"]:
            for member in group["members"]:
                mid = member["id"]
                role = "office/" + (mid if len(mid) <= 121 else self.digest(mid)[:32])
                task = db.execute(
                    "SELECT t.* FROM tasks t LEFT JOIN meeting_tasks a ON a.project=t.project AND a.task_id=t.task_id WHERE t.project=? AND t.status!='cancelled' AND (a.member_id=? OR (a.member_id IS NULL AND t.role_id=?)) ORDER BY (t.status='running') DESC,(t.status IN ('ready','paused')) DESC,t.updated_at DESC LIMIT 1",
                    (r.project, mid, role),
                ).fetchone()
                messages = []
                if task:
                    for msg in db.execute(
                        "SELECT content,status,created_at FROM member_messages WHERE project=? AND task_id=? ORDER BY id DESC LIMIT 2",
                        (r.project, task["task_id"]),
                    ):
                        messages.append(
                            {
                                "role": "user",
                                "text": msg["content"][:1200],
                                "status": msg["status"],
                                "created_at": msg["created_at"],
                            }
                        )
                    if task["status"] == "done":
                        for ref in json.loads(task["outputs"])[:1]:
                            value = self._item(db, r.project, ref["key"], ref["version"])["value"]
                            messages.append(
                                {
                                    "role": "assistant",
                                    "text": str(
                                        value.get("summary", "")
                                        if isinstance(value, dict)
                                        else value
                                    )[:2000],
                                    "created_at": task["updated_at"],
                                }
                            )
                    if task["failure"]:
                        messages.append(
                            {
                                "role": "system",
                                "text": json.loads(task["failure"])["message"],
                                "created_at": task["updated_at"],
                            }
                        )
                if task and task["status"] in ("running", "paused", "ready"):
                    for activity in db.execute(
                        "SELECT details,created_at FROM group_logs WHERE project=? AND task_id=? AND kind='run.activity' ORDER BY id DESC LIMIT 3",
                        (r.project, task["task_id"]),
                    ):
                        detail = json.loads(activity["details"])
                        messages.append(
                            {
                                "role": "assistant",
                                "kind": "activity",
                                "text": str(detail.get("text", ""))[:2000],
                                "source_agent": detail.get("source_agent"),
                                "parent_agent": detail.get("parent_agent"),
                                "created_at": activity["created_at"],
                            }
                        )
                members.append(
                    {
                        "member_id": mid,
                        "group_id": group["id"],
                        "task": self._task_view(db, r.project, task, now) if task else None,
                        "messages": sorted(messages, key=lambda m: m["created_at"]),
                    }
                )
        return {"members": members, "server_time": now}

    def _office_transfer(self, db, r, now):
        row = self._task_row(db, r.project, r.task_id)
        self._check_revision(row, r.expected_revision)
        if row["status"] not in ("ready", "paused", "failed"):
            raise CoordinationError("invalid_transition", "请先暂停任务并确认原执行已停止")
        if db.execute(
            "SELECT 1 FROM member_messages WHERE project=? AND task_id=? AND status='pending'",
            (r.project, r.task_id),
        ).fetchone():
            raise CoordinationError("guidance_pending", "请等待正在处理的成员引导结束")
        if db.execute(
            "SELECT 1 FROM decisions WHERE project=? AND task_id=? AND status='pending'",
            (r.project, r.task_id),
        ).fetchone():
            raise CoordinationError("decision_required", "请先处理待确认事项再交接任务")
        people = self._office_routes(self._office_config(db, r.project))
        if r.member_id not in people or "/fallback/" in r.member_id:
            raise CoordinationError("not_found", "接收成员不存在", status=404)
        group, member = people[r.member_id]
        selection = self._office_binding(db, r.project, group, member)
        old_role = self._registry_view(self._registry_row(db, "roles", r.project, row["role_id"]))
        previous = self._task_assignment(db, r.project, r.task_id)
        checkpoint = f"tasks/{r.task_id}/handoff/{row['revision']}"
        inputs = json.loads(row["inputs"])
        if len(inputs) >= 32:
            raise CoordinationError("input_limit", "任务输入已满，无法保存交接单")
        handoff = {
            "from_member": previous["member_id"] if previous else None,
            "to_member": r.member_id,
            "note": r.note,
            "previous_fence": row["fence"],
            "outputs": json.loads(row["outputs"]),
            "failure": json.loads(row["failure"]) if row["failure"] else None,
            "instruction": "先检查工作区现有改动及已发布结果，再按交接说明继续；不要假定前一执行已完成验收。",
        }
        self._write_item(db, r, now, checkpoint, "state", 1, handoff, [])
        role_id = "transfer/" + self.digest(r.task_id)[:32]
        old = db.execute(
            "SELECT revision FROM roles WHERE project=? AND role_id=?", (r.project, role_id)
        ).fetchone()
        self._role_put(
            db,
            RolePut(
                project=r.project,
                actor=r.actor,
                request_id=r.request_id,
                role_id=role_id,
                expected_revision=old["revision"] if old else 0,
                label=old_role["label"],
                instructions=old_role["instructions"],
                deliverables=old_role["deliverables"],
                required_capabilities=old_role["required_capabilities"],
                agent_id=member["agent_id"],
                selection=Selection.model_validate(
                    {**selection, "skills": list(member.get("skills") or [])}
                ),
            ),
            now,
        )
        description = (
            row["description"] + "\n\n人工交接说明（已有文件修改保留，请先检查）：\n" + r.note
        )
        if len(description) > 8000:
            raise CoordinationError("description_limit", "交接后任务说明超过 8000 字")
        db.execute(
            "UPDATE tasks SET role_id=?,description=?,inputs=?,status='paused',revision=revision+1,owner=NULL,lease_hash=NULL,expires_at=NULL,updated_at=? WHERE project=? AND task_id=?",
            (role_id, description, self.encode([*inputs, checkpoint]), now, r.project, r.task_id),
        )
        db.execute(
            "INSERT INTO meeting_tasks VALUES (?,?,?,?,?,?) ON CONFLICT(project,task_id) DO UPDATE SET member_id=excluded.member_id,group_id=excluded.group_id",
            (r.project, r.task_id, "", member["id"], group["id"], "成员执行"),
        )
        # Explicitly rebinding the same member also repairs its future stages.
        # Keep the original shared context immutable: changing that item would
        # invalidate already completed planning results and force a full replay.
        source = db.execute(
            "SELECT meeting_id FROM meeting_tasks WHERE project=? AND task_id=?",
            (r.project, r.task_id),
        ).fetchone()
        if previous and previous["member_id"] == r.member_id and source and source["meeting_id"]:
            definition = db.execute(
                "SELECT definition FROM meetings WHERE project=? AND meeting_id=?",
                (r.project, source["meeting_id"]),
            ).fetchone()
            if definition:
                meeting = json.loads(definition[0])
                for saved_group in meeting["groups"]:
                    if saved_group["software"] != group["software"]:
                        continue
                    for saved_member in saved_group["members"]:
                        if saved_member["id"] == r.member_id:
                            for key in (
                                "agent_id",
                                "provider",
                                "model",
                                "parameters",
                                "skills",
                                "fallback_models",
                            ):
                                if key in member:
                                    saved_member[key] = member[key]
                self._meeting_save(db, r, meeting)
        self._event(
            db, r, now, "office.transferred", r.task_id, {**handoff, "checkpoint": checkpoint}
        )
        return self._task_get(db, r, now)

    @staticmethod
    def _office_routes(config):
        routes = {}
        for group in config["groups"]:
            for member in group["members"]:
                routes[member["id"]] = (group, member)
                for index, fallback in enumerate(member.get("fallback_models", []), 1):
                    route_id = f"{member['id']}/fallback/{index}"
                    routes[route_id] = (group, {**member, **fallback, "id": route_id})
        return routes

    @staticmethod
    def _office_config(db, project):
        row = db.execute("SELECT * FROM office_configs WHERE project=?", (project,)).fetchone()
        return (
            {
                "revision": row["revision"],
                "groups": json.loads(row["definition"]),
                "updated_at": row["updated_at"],
            }
            if row
            else {"revision": 0, "groups": [], "updated_at": None}
        )

    def _office_get(self, db, r, now):
        from .office_roles import POSITIONS

        config = self._office_config(db, r.project)
        return {
            **config,
            # The UI matches an independent task by its role id. Long member
            # ids are shortened to a digest when the role is created, so expose
            # the exact mapping instead of making the browser duplicate it.
            "member_roles": {
                member["id"]: "office/"
                + (member["id"] if len(member["id"]) <= 121 else self.digest(member["id"])[:32])
                for group in config.get("groups") or []
                if isinstance(group, dict)
                for member in (
                    group.get("members") if isinstance(group.get("members"), list) else []
                )
                if isinstance(member, dict) and isinstance(member.get("id"), str) and member["id"]
            },
            "positions": POSITIONS,
            "capabilities": {
                "native_subagent": {
                    "supported": False,
                    "code": "native_binding_required",
                    "message": "原生子代理配置可保存为草稿；当前没有已验证的运行时会话适配器，不能执行。",
                }
            },
        }

    def _team_groups_list(self, db, r, now):
        """Return every saved office group with its project attribution.

        Groups remain stored in each project's ``office_configs`` row.  The team
        manager is an administrative projection over those durable rows, not a
        second copy that could drift from the active office.
        """
        current = {
            group["id"]
            for group in self._office_config(db, r.project)["groups"]
            if isinstance(group, dict) and group.get("id")
        }
        rows = db.execute(
            "SELECT o.project,o.revision,o.definition,o.updated_at,"
            "p.name AS project_name,p.path AS project_path "
            "FROM office_configs o LEFT JOIN local_projects p ON p.project=o.project "
            "ORDER BY COALESCE(p.name,o.project) COLLATE NOCASE,o.project"
        ).fetchall()
        needle = r.query.strip().casefold()
        projects = []
        groups = []
        for row in rows:
            try:
                definition = json.loads(row["definition"])
            except (TypeError, ValueError):
                definition = []
            items = definition if isinstance(definition, list) else []
            project_name = row["project_name"] or row["project"]
            projects.append(
                {
                    "project": row["project"],
                    "project_name": project_name,
                    "project_path": row["project_path"] or "",
                    "revision": row["revision"],
                    "group_count": len(items),
                    "updated_at": row["updated_at"],
                }
            )
            if r.project_filter and row["project"] != r.project_filter:
                continue
            for group in items:
                if not isinstance(group, dict):
                    continue
                group_id = group.get("id")
                name = group.get("name")
                software = group.get("software")
                members = group.get("members")
                if not isinstance(group_id, str) or not isinstance(name, str):
                    continue
                if not isinstance(members, list):
                    members = []
                if needle:
                    haystack = " ".join(
                        [
                            project_name,
                            row["project"],
                            name,
                            str(software or ""),
                            *[
                                str(member.get("name", ""))
                                for member in members
                                if isinstance(member, dict)
                            ],
                        ]
                    ).casefold()
                    if needle not in haystack:
                        continue
                groups.append(
                    {
                        "project": row["project"],
                        "project_name": project_name,
                        "project_path": row["project_path"] or "",
                        "revision": row["revision"],
                        "updated_at": row["updated_at"],
                        "group": group,
                        "member_count": len(members),
                        "workload": self._group_workload(
                            db, row["project"], group_id, members, now
                        ),
                        "in_office": row["project"] == r.project and group_id in current,
                    }
                )
        groups.sort(
            key=lambda item: (
                item["project_name"].casefold(),
                item["group"]["name"].casefold(),
            )
        )
        truncated = len(groups) > 5000
        return {
            "groups": groups[:5000],
            "projects": projects,
            "total": len(groups),
            "truncated": truncated,
            "current_project": r.project,
            "server_time": now,
        }

    def _office_put(self, db, r, now):
        config = self._office_config(db, r.project)
        self._check_revision(config, r.expected_revision)
        groups = [g.model_dump(mode="json") for g in r.groups]
        for group in groups:
            for member in group["members"]:
                if member["relationship"] == "native_subagent":
                    # An automatically selected independent runner is not a native child.
                    member["agent_id"] = None
        old_routes = self._office_routes(config)
        new_routes = self._office_routes({"groups": groups})
        for group, member in new_routes.values():
            if member["agent_id"]:
                self._office_binding(db, r.project, group, member)
        for route_id, (old_group, old_member) in old_routes.items():
            new = new_routes.get(route_id)
            identity = ("agent_id", "provider", "model", "parameters")
            if (
                not new
                or old_group["software"] != new[0]["software"]
                or any(old_member.get(k) != new[1].get(k) for k in identity)
            ):
                db.execute(
                    "DELETE FROM quota_observations WHERE project=? AND route_id=?",
                    (r.project, route_id),
                )
        db.execute(
            "INSERT INTO office_configs VALUES (?,?,?,?) ON CONFLICT(project) DO UPDATE SET "
            "revision=excluded.revision,definition=excluded.definition,updated_at=excluded.updated_at",
            (r.project, config["revision"] + 1, self.encode(groups), now),
        )
        previous_groups = {group["id"] for group in config["groups"]}
        for group in groups:
            if group["id"] not in previous_groups:
                message = "小组已创建：" + group["name"]
                kind = "group.created"
            else:
                message = "小组配置已更新：" + group["name"]
                kind = "group.configured"
            self.record_db(
                db,
                r.project,
                group["id"],
                kind,
                message,
                details={
                    "name": group["name"],
                    "software": group["software"],
                    "members": [member["id"] for member in group["members"]],
                },
                now=now,
            )
        self._event(db, r, now, "office.configured", "office", {"revision": config["revision"] + 1})
        return self._office_get(db, r, now)

    def _office_snapshot_rows(self, db, project, limit):
        # Read one extra row so the response can report truncation without
        # loading an unbounded snapshot history into memory.
        return db.execute(
            "SELECT snapshot_id,label,group_count,member_count,created_at "
            "FROM office_snapshots WHERE project=? "
            "ORDER BY created_at DESC,snapshot_id LIMIT ?",
            (project, limit + 1),
        ).fetchall()

    def _office_snapshot_list(self, db, r, now):
        limit = max(1, getattr(r, "limit", 50))
        rows = self._office_snapshot_rows(db, r.project, limit)
        return {
            "snapshots": [dict(row) for row in rows[:limit]],
            "truncated": len(rows) > limit,
        }

    def _office_snapshot_save(self, db, r, now):
        config = self._office_config(db, r.project)
        self._check_revision(config, r.expected_revision)
        label = " ".join(r.label.split())
        if not label:
            raise CoordinationError("invalid_label", "A snapshot label is required")
        groups = config["groups"]
        snapshot_id = "snap_" + self.digest(f"{r.project}:{label}:{now}:{r.request_id}")[:24]
        db.execute(
            "INSERT INTO office_snapshots VALUES (?,?,?,?,?,?,?)",
            (
                r.project,
                snapshot_id,
                label,
                self.encode(groups),
                len(groups),
                sum(len(group.get("members") or []) for group in groups),
                now,
            ),
        )
        self._event(
            db,
            r,
            now,
            "office.snapshot_saved",
            "office",
            {"snapshot_id": snapshot_id, "label": label, "groups": len(groups)},
        )
        return {
            "snapshot_id": snapshot_id,
            "label": label,
            "group_count": len(groups),
            "member_count": sum(len(group.get("members") or []) for group in groups),
        }

    def _office_snapshot_restore(self, db, r, now):
        row = db.execute(
            "SELECT * FROM office_snapshots WHERE project=? AND snapshot_id=?",
            (r.project, r.snapshot_id),
        ).fetchone()
        if row is None:
            raise CoordinationError("snapshot_unknown", "No such snapshot", 404)
        # Reuse the normal write path so bindings, quotas and audit stay consistent.
        request = OfficePut.model_validate(
            {
                "project": r.project,
                "actor": r.actor,
                "request_id": r.request_id,
                "expected_revision": r.expected_revision,
                "groups": json.loads(row["definition"]),
            }
        )
        result = self._office_put(db, request, now)
        self._event(
            db,
            r,
            now,
            "office.snapshot_restored",
            "office",
            {"snapshot_id": r.snapshot_id, "revision": result["revision"]},
        )
        return result

    def _office_snapshot_delete(self, db, r, now):
        row = db.execute(
            "SELECT label FROM office_snapshots WHERE project=? AND snapshot_id=?",
            (r.project, r.snapshot_id),
        ).fetchone()
        if row is None:
            raise CoordinationError("snapshot_unknown", "No such snapshot", 404)
        db.execute(
            "DELETE FROM office_snapshots WHERE project=? AND snapshot_id=?",
            (r.project, r.snapshot_id),
        )
        self._event(
            db,
            r,
            now,
            "office.snapshot_deleted",
            "office",
            {"snapshot_id": r.snapshot_id, "label": row["label"]},
        )
        return {"snapshot_id": r.snapshot_id, **self._office_snapshot_list(db, r, now)}

    def _office_binding(self, db, project, group, member):
        if member["relationship"] == "native_subagent":
            raise CoordinationError(
                "native_binding_required",
                "原生子代理当前只能保存为草稿；执行需要已验证的运行时会话适配器。",
                {"member_id": member["id"]},
            )
        if not member["agent_id"]:
            raise CoordinationError(
                "unbound_member",
                "Connect this person to a registered runner first",
                {"member_id": member["id"]},
            )
        agent = self._registry_view(self._registry_row(db, "agents", project, member["agent_id"]))
        if agent["runtime"] != group["software"]:
            raise CoordinationError(
                "runtime_mismatch", "Runner software does not match the office group"
            )
        selection = {"model": member["model"], "parameters": member["parameters"]}
        if member.get("mcp_servers") is not None:
            selection["mcp_servers"] = list(member["mcp_servers"])
        from .mcp_catalogue import validate_mcp_selection

        validate_mcp_selection(
            group["software"], selection.get("mcp_servers"), self.skill_workspace(project)
        )
        self._validate_binding(agent, selection, [])
        if group["software"] == "codex" and member["provider"] != "openai":
            raise CoordinationError(
                "provider_mismatch", "The built-in Codex adapter uses the OpenAI account provider"
            )
        # Providers must be declared by the runner, except Codex's account-backed built-in adapter.
        if (
            group["software"] != "codex"
            and selection["parameters"].get("provider") != member["provider"]
        ):
            raise CoordinationError(
                "provider_mismatch", "Runner selection must explicitly bind this provider"
            )
        return selection

    def _office_role(self, db, r, now, group, member, *, role_id=None, position=None):
        from .office_roles import POSITIONS

        selection = self._office_binding(db, r.project, group, member)
        role_id = role_id or "office/" + (
            member["id"] if len(member["id"]) <= 121 else self.digest(member["id"])[:32]
        )
        role = db.execute(
            "SELECT revision FROM roles WHERE project=? AND role_id=?", (r.project, role_id)
        ).fetchone()
        selected_position = position or member["position"]
        spec = (
            {
                "label": member["custom_position"],
                "instructions": (
                    "按照自定义岗位职责完成当前任务，先阅读需求、前置结果和项目约束。"
                    "使用 RelayFence 交接结果，如实报告变更、检查及未验证事项；"
                    "未经授权不得修改凭据、密钥或认证设置。"
                ),
                "deliverables": [
                    "任务结果与变更文件说明",
                    "验证结果及未验证事项",
                    "后续工作和交接说明",
                ],
            }
            if selected_position == "custom"
            else POSITIONS[selected_position]
        )
        self._role_put(
            db,
            RolePut(
                project=r.project,
                actor=r.actor,
                request_id=r.request_id,
                role_id=role_id,
                expected_revision=role["revision"] if role else 0,
                label=spec["label"] + " · " + member["name"],
                instructions=(
                    member["prompt"] if member.get("prompt") is not None else spec["instructions"]
                ),
                deliverables=spec["deliverables"],
                agent_id=member["agent_id"],
                selection=Selection.model_validate(
                    {**selection, "skills": list(member.get("skills") or [])}
                ),
            ),
            now,
        )
        return role_id

    def _office_dispatch(self, db, r, now):
        config = self._office_config(db, r.project)
        self._check_revision(config, r.expected_revision)
        # Apply to a private dispatch snapshot, never the saved team.
        overrides = getattr(r, "member_overrides", {})
        if overrides:
            allowed = set(r.member_ids) | ({r.leader_id} if r.leader_id else set())
            for group in config["groups"]:
                if any(m["id"] in allowed for m in group["members"]):
                    allowed.update(
                        m["id"] for m in group["members"] if m["relationship"] == "leader"
                    )
            if set(overrides) - allowed:
                raise CoordinationError(
                    "invalid_capability_member", "只能配置本次参会成员的能力", status=422
                )
            config = json.loads(json.dumps(config))
            for group in config["groups"]:
                for member in group["members"]:
                    override = overrides.get(member["id"])
                    if override:
                        for key, value in override.model_dump(exclude_unset=True).items():
                            if key != "skills" or value is not None:
                                member[key] = value
                        if member.get("skills"):
                            from .skill_catalogue import validate_skill_names

                            validate_skill_names(
                                group["software"], member["skills"], self.skill_workspace(r.project)
                            )
        people = {m["id"]: (g, m) for g in config["groups"] for m in g["members"]}
        if any(mid not in people for mid in r.member_ids):
            raise CoordinationError(
                "not_found", "A selected office member no longer exists", status=404
            )
        if r.leader_id:
            return self._meeting_start(db, r, now, config)
        tasks = []
        for index, mid in enumerate(r.member_ids):
            group, member = people[mid]
            task_id = f"{r.briefing_id}/{index + 1}"
            role_id = self._office_role(
                db, r, now, group, member, role_id="office-task/" + self.digest(task_id)[:40]
            )
            task = self._task_create(
                db,
                TaskCreate(
                    project=r.project,
                    actor=r.actor,
                    request_id=r.request_id,
                    task_id=task_id,
                    title=f"{r.title} · {member['name']}",
                    review_required=r.review_required,
                    description=r.description
                    + "\n\nWork in parallel with the other assigned members; do not wait for "
                    "their tasks or overwrite their files. Fulfil your assigned role and report "
                    "changed files, checks and unresolved issues accurately.",
                    role_id=role_id,
                    depends_on=[],
                    inputs=[],
                ),
                now,
            )
            # Assign the task to its office member.  This also lets activity,
            # group logs and parallel dispatch resolve the same owner.
            db.execute(
                "INSERT INTO meeting_tasks VALUES (?,?,?,?,?,?)",
                (r.project, task_id, "", member["id"], group["id"], "成员执行"),
            )
            self.record_db(
                db,
                r.project,
                group["id"],
                "dispatch.created",
                f"已派发并行任务：{task['title']}",
                member_id=member["id"],
                task_id=task_id,
                details={"briefing_id": r.briefing_id, "parallel": True},
                now=now,
            )
            tasks.append(task)
        self._event(
            db,
            r,
            now,
            "office.briefed",
            r.briefing_id,
            {
                "members": r.member_ids,
                "tasks": [t["task_id"] for t in tasks],
                "parallel": True,
            },
        )
        return {"tasks": tasks}

    def _quota_observe(self, db, r, now):
        from .quota import QuotaObservation

        config = self._office_config(db, r.project)
        if r.route_id not in self._office_routes(config):
            raise CoordinationError("not_found", "Quota route is not in this office", status=404)
        observation = QuotaObservation.model_validate(
            r.model_dump(exclude={"actor", "project", "request_id"})
        )
        if observation.observed_at > now + 5 or observation.expires_at > now + 86400:
            raise CoordinationError(
                "invalid_observation",
                "Observation cannot be future-dated or valid for over 24 hours",
            )
        old = db.execute(
            "SELECT observed_at FROM quota_observations WHERE project=? AND route_id=?",
            (r.project, r.route_id),
        ).fetchone()
        if old and old["observed_at"] >= observation.observed_at:
            raise CoordinationError(
                "stale_observation", "A newer or equal observation already exists"
            )
        db.execute(
            "INSERT INTO quota_observations VALUES (?,?,?,?) ON CONFLICT(project,route_id) DO UPDATE SET observation=excluded.observation,observed_at=excluded.observed_at",
            (r.project, r.route_id, observation.model_dump_json(), observation.observed_at),
        )
        self._event(
            db, r, now, "quota.observed", r.route_id, {"status": r.status, "source": r.source}
        )
        return observation.model_dump(mode="json")

    @staticmethod
    def _quota_list(db, r, now):
        observations = [
            json.loads(row["observation"])
            for row in db.execute(
                "SELECT observation FROM quota_observations WHERE project=? ORDER BY route_id",
                (r.project,),
            )
        ]
        return {
            "observations": [{**o, "stale": now >= o["expires_at"]} for o in observations],
            "server_time": now,
        }

    def _office_route_preview(self, db, r, now):
        from .quota import QuotaObservation, RouteOption, choose_route

        config = self._office_config(db, r.project)

        def executable(group, member):
            try:
                self._office_binding(db, r.project, group, member)
                return True
            except CoordinationError:
                return False

        routes = [
            RouteOption(
                id=m["id"],
                software=g["software"],
                provider=m["provider"],
                model=m["model"],
                enabled=executable(g, m),
            )
            for g, m in self._office_routes(config).values()
        ]
        observations = [
            QuotaObservation.model_validate({k: v for k, v in o.items() if k != "stale"})
            for o in self._quota_list(db, r, now)["observations"]
        ]
        return choose_route(
            routes, observations, r.current_route_id, now, getattr(r, "allow_unknown", False)
        ).model_dump(mode="json")

    def _office_handoff(self, db, r, now):
        row = self._task_row(db, r.project, r.task_id)
        self._check_revision(row, r.expected_revision)
        failure = json.loads(row["failure"]) if row["failure"] else {}
        if row["status"] != "failed" or failure.get("code") != "quota_exhausted":
            raise CoordinationError(
                "unsafe_handoff", "Only stopped, explicitly quota-exhausted tasks can be handed off"
            )
        checkpoint = self._item(db, r.project, r.checkpoint_key)
        if not checkpoint["fresh"]:
            raise CoordinationError("stale_checkpoint", "Refresh the checkpoint before handoff")
        config = self._office_config(db, r.project)
        people = self._office_routes(config)
        current = people.get(r.current_route_id)
        if (
            not current
            or not row["execution"]
            or json.loads(row["execution"])["agent"]["agent_id"] != current[1]["agent_id"]
        ):
            raise CoordinationError(
                "route_mismatch", "Current route does not own the failed attempt"
            )
        execution = json.loads(row["execution"])
        requested = execution["requested"]
        # Skills are not part of route identity; comparing the full selection
        # made every skill-enabled role impossible to hand off.
        if {
            "model": requested.get("model"),
            "parameters": requested.get("parameters"),
        } != {
            "model": current[1]["model"],
            "parameters": current[1]["parameters"],
        }:
            raise CoordinationError(
                "route_mismatch", "Current route model differs from the failed attempt"
            )
        decision = self._office_route_preview(db, r, now)
        target = decision["route_id"]
        if not target or target == r.current_route_id or decision["requires_probe"]:
            raise CoordinationError(
                "no_available_route", "No verified alternate route is available"
            )
        count = db.execute(
            "SELECT count(*) FROM events WHERE project=? AND subject=? AND kind='office.handoff'",
            (r.project, r.task_id),
        ).fetchone()[0]
        if count >= 3:
            raise CoordinationError(
                "handoff_limit", "Three handoffs reached; inspect the task manually"
            )
        group, member = people[target]
        selection = self._office_binding(db, r.project, group, member)
        original_role = execution["role"]
        original_skills = list(
            (original_role.get("selection") or {}).get("skills") or requested.get("skills") or []
        )
        role_id = "handoff/" + self.digest(r.task_id)[:32]
        existing = db.execute(
            "SELECT revision FROM roles WHERE project=? AND role_id=?", (r.project, role_id)
        ).fetchone()
        self._role_put(
            db,
            RolePut(
                project=r.project,
                actor=r.actor,
                request_id=r.request_id,
                role_id=role_id,
                expected_revision=existing["revision"] if existing else 0,
                label=original_role["label"],
                instructions=original_role["instructions"],
                deliverables=original_role["deliverables"],
                required_capabilities=original_role["required_capabilities"],
                agent_id=member["agent_id"],
                selection=Selection.model_validate(
                    {
                        **selection,
                        # Keep the work's skills unless the receiving member declares its own.
                        "skills": list(member.get("skills") or original_skills),
                    }
                ),
            ),
            now,
        )
        inputs = list(dict.fromkeys([*json.loads(row["inputs"]), r.checkpoint_key]))
        if len(inputs) > 32:
            raise CoordinationError("input_limit", "No room for the handoff checkpoint")
        db.execute(
            "UPDATE tasks SET status='ready',role_id=?,inputs=?,revision=revision+1,owner=NULL,lease_hash=NULL,expires_at=NULL,failure=NULL,updated_at=? WHERE project=? AND task_id=?",
            (role_id, self.encode(inputs), now, r.project, r.task_id),
        )
        self._event(
            db,
            r,
            now,
            "office.handoff",
            r.task_id,
            {
                "from": r.current_route_id,
                "to": target,
                "checkpoint": r.checkpoint_key,
                "reason": decision["reason"],
            },
        )
        return self._task_get(db, r, now)
