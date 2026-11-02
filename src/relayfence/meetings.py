"""Durable, bounded planning/negotiation driven by published agent results."""

import json
import re
from types import SimpleNamespace

from .errors import CoordinationError
from .models import TaskCreate


class Meetings:
    def _meeting_revision(self, db, r, now):
        row = db.execute(
            "SELECT definition FROM meetings WHERE project=? AND meeting_id=?",
            (r.project, r.meeting_id),
        ).fetchone()
        if not row:
            raise CoordinationError("not_found", "原任务不存在", status=404)
        parent = json.loads(row["definition"])
        root = parent.get("root_id", parent["id"])
        family = [
            json.loads(v[0])
            for v in db.execute("SELECT definition FROM meetings WHERE project=?", (r.project,))
        ]
        if any(
            m.get("root_id", m["id"]) == root and m.get("iteration", 1) > parent.get("iteration", 1)
            for m in family
        ):
            raise CoordinationError("newer_iteration", "已有更新轮次，请从最新轮次继续修改")
        unfinished = db.execute(
            "SELECT 1 FROM tasks t JOIN meeting_tasks a ON t.project=a.project AND t.task_id=a.task_id WHERE a.project=? AND a.meeting_id=? AND t.status!='done' LIMIT 1",
            (r.project, parent["id"]),
        ).fetchone()
        if parent["stage"] not in ("done", "discussion_done") or unfinished:
            raise CoordinationError(
                "iteration_active", "当前轮次尚未完成；执行中请使用成员引导，完成后再创建修改轮次"
            )
        config = self._office_config(db, r.project)
        roster = {m["id"] for g in config["groups"] for m in g["members"]}
        selected = [m["id"] for g in parent["groups"] for m in g["members"]]
        if not set(selected).issubset(roster):
            raise CoordinationError("roster_changed", "原轮次部分成员已删除，请先恢复成员配置")
        original_goal = parent.get("original_goal", parent["goal"])
        request = SimpleNamespace(
            project=r.project,
            actor=r.actor,
            request_id=r.request_id,
            briefing_id=r.briefing_id,
            title=parent["title"],
            description=original_goal,
            member_ids=selected,
            leader_id=parent["leader"],
            mode=parent["mode"],
            review_required=parent.get("review_required", False),
        )
        result = self._meeting_start(db, request, now, config)
        meeting = result["meeting"]
        meeting.update(
            root_id=root,
            parent_id=parent["id"],
            iteration=parent.get("iteration", 1) + 1,
            changes=r.changes,
            original_goal=original_goal,
        )
        meeting["change_history"] = [*parent.get("change_history", []), r.changes]
        previous = []
        refs = []
        for task in db.execute(
            "SELECT t.* FROM tasks t JOIN meeting_tasks a ON t.project=a.project AND t.task_id=a.task_id WHERE a.project=? AND a.meeting_id=? AND t.status='done' ORDER BY t.updated_at DESC LIMIT 16",
            (r.project, parent["id"]),
        ):
            for ref in json.loads(task["outputs"])[:1]:
                item = self._item(db, r.project, ref["key"], ref["version"])
                if not item["fresh"] or self._changed_task_dependencies(db, r.project, task):
                    raise CoordinationError("stale_result", "前轮结果已变化，请先检查结果")
                value = item["value"]
                previous.append(
                    {
                        "task": task["title"],
                        "key": ref["key"],
                        "version": ref["version"],
                        "summary": str(
                            value.get("summary", "") if isinstance(value, dict) else value
                        )[:2000],
                    }
                )
                refs.append(ref)
        # New context revision is written before any task can be claimed.
        self._write_item(
            db,
            r,
            now,
            meeting["context"],
            "state",
            2,
            {
                "goal": original_goal,
                "changes": r.changes,
                "change_history": meeting["change_history"],
                "instruction": "在已有成果上完成本轮修改，保留未要求修改的功能；先检查项目文件和前轮结果。",
                "groups": meeting["groups"],
                "previous_results": previous,
                "previous_iteration": parent["id"],
            },
            refs,
        )
        self._meeting_save(db, r, meeting)
        self._event(
            db,
            r,
            now,
            "meeting.revised",
            root,
            {
                "iteration": meeting["iteration"],
                "meeting_id": meeting["id"],
                "parent_id": parent["id"],
            },
        )
        return result

    def _meeting_control(self, db, r, now):
        row = db.execute(
            "SELECT definition FROM meetings WHERE project=? AND meeting_id=?",
            (r.project, r.meeting_id),
        ).fetchone()
        if not row:
            raise CoordinationError("not_found", "会议不存在", status=404)
        meeting = json.loads(row["definition"])
        if meeting["stage"] == "done" or bool(meeting.get("paused")) == (r.action == "pause"):
            raise CoordinationError("invalid_transition", "会议状态已经改变，请刷新后重试")
        if (
            r.action == "resume"
            and db.execute(
                "SELECT 1 FROM decisions WHERE project=? AND meeting_id=? AND status='pending'",
                (r.project, r.meeting_id),
            ).fetchone()
        ):
            raise CoordinationError("decision_required", "请先回复待确认事项")
        meeting["paused"] = r.action == "pause"
        tasks = db.execute(
            "SELECT t.* FROM tasks t JOIN meeting_tasks m ON t.project=m.project AND t.task_id=m.task_id WHERE m.project=? AND m.meeting_id=?",
            (r.project, r.meeting_id),
        ).fetchall()
        previous = meeting.get("paused_tasks", {})
        changed = {}
        for task in tasks:
            if (
                r.action == "pause"
                and task["status"] in ("ready", "running")
                or r.action == "resume"
                and task["status"] == "paused"
                and previous.get(task["task_id"]) == task["revision"]
            ):
                updated = self._task_control(
                    db,
                    SimpleNamespace(
                        project=r.project,
                        actor=r.actor,
                        request_id=r.request_id,
                        task_id=task["task_id"],
                        expected_revision=task["revision"],
                        action=r.action,
                        reason="会议整体" + r.action,
                    ),
                    now,
                )
                changed[task["task_id"]] = updated["revision"]
        meeting["paused_tasks"] = changed if r.action == "pause" else {}
        self._meeting_save(db, r, meeting)
        self._event(db, r, now, "meeting." + r.action, meeting["id"], {"tasks": list(changed)})
        return {"meeting": meeting, "task_ids": list(changed)}

    def _meeting_save(self, db, r, meeting):
        db.execute(
            "INSERT INTO meetings VALUES (?,?,?) ON CONFLICT(project,meeting_id) DO UPDATE SET definition=excluded.definition",
            (r.project, meeting["id"], self.encode(meeting)),
        )

    def _meeting_task(self, db, r, now, meeting, member_id, stage, description, dependencies=()):
        group, member = next(
            (g, m) for g in meeting["groups"] for m in g["members"] if m["id"] == member_id
        )
        meeting["serial"] += 1
        task_id = f"{meeting['id']}/s{meeting['serial']}"
        role = self._office_role(
            db, r, now, group, member, role_id="office-task/" + self.digest(task_id)[:40]
        )
        task = self._task_create(
            db,
            TaskCreate(
                project=r.project,
                actor=r.actor,
                request_id=r.request_id,
                task_id=task_id,
                title=f"{meeting['title']} · {stage} · {member['name']}"[:240],
                description=description,
                role_id=role,
                review_required=stage == "成员执行" and meeting.get("review_required", False),
                depends_on=list(dependencies),
                inputs=[meeting["context"], *meeting.get("decision_inputs", [])]
                + [f"tasks/{t}/result" for t in dependencies],
            ),
            now,
        )
        db.execute(
            "INSERT INTO meeting_tasks VALUES (?,?,?,?,?,?)",
            (r.project, task_id, meeting["id"], member_id, group["id"], stage),
        )
        self.record_db(
            db,
            r.project,
            group["id"],
            "dispatch.created",
            f"已派发任务：{task['title']}",
            member_id=member_id,
            task_id=task_id,
            details={"meeting_id": meeting["id"], "stage": stage, "parallel": stage == "成员执行"},
            now=now,
        )
        return task

    @staticmethod
    def _structured_instruction(schema):
        return (
            "\n本阶段只分析和规划，不修改项目文件。阅读所有输入中的用户目标、成员职责、前序结果。"
            "必须遵守任务阶段要求；岗位 Prompt 不改变本阶段的分析范围。"
            "在标准结果的 details 字段中返回以下结构的纯 JSON 字符串，不加 Markdown；summary 用自然语言解释判断。"
            "正常提交分析结果（outcome=completed），可行性异议通过结构化字段表达。\n" + schema
        )

    def _meeting_plan(self, db, r, now, meeting, dependencies=()):
        meeting["stage"] = "planning"
        task = self._meeting_task(
            db,
            r,
            now,
            meeting,
            meeting["leader"],
            "主导者拆解",
            "深度拆解用户目标：识别交付物、验收标准、依赖、风险和项目现状。根据配置成员的职责与能力为每个参会小组分配具体工作。"
            "必须包含每个参会小组一次；吸收组长的异议并说明调整理由，不能忽略风险。"
            + self._structured_instruction(
                '{"assignments":[{"group_id":"配置中的小组ID","goal":"具体工作和验收标准"}]}'
            ),
            dependencies,
        )
        meeting["waiting"] = [task["task_id"]]
        return task

    def _meeting_start(self, db, r, now, config):
        selected = set(r.member_ids)
        selected.add(r.leader_id)
        groups = []
        for group in config["groups"]:
            members = [m for m in group["members"] if m["id"] in selected]
            if members:
                lead = next(m for m in group["members"] if m["relationship"] == "leader")
                if lead["id"] not in {m["id"] for m in members}:
                    members.insert(0, lead)
                for member in members:
                    self._office_binding(db, r.project, group, member)
                groups.append({**group, "members": members})
        if r.leader_id not in {m["id"] for g in groups for m in g["members"]}:
            raise CoordinationError("invalid_leader", "请选择项目中已接入的主导者")
        if db.execute(
            "SELECT 1 FROM meetings WHERE project=? AND meeting_id=?", (r.project, r.briefing_id)
        ).fetchone():
            raise CoordinationError("already_exists", "会议编号已存在")
        if len(groups) > 16:
            raise CoordinationError("group_limit", "一次会议最多 16 个小组")
        meeting = dict(
            id=r.briefing_id,
            title=r.title,
            goal=r.description,
            mode=r.mode,
            review_required=getattr(r, "review_required", False),
            leader=r.leader_id,
            groups=groups,
            stage="planning",
            serial=0,
            round=1,
            waiting=[],
            error=None,
            created_at=now,
            context=f"meetings/{r.briefing_id}/context",
        )
        self._write_item(
            db,
            r,
            now,
            meeting["context"],
            "state",
            1,
            {"goal": r.description, "groups": groups},
            [],
        )
        task = self._meeting_plan(db, r, now, meeting)
        self._meeting_save(db, r, meeting)
        self._event(
            db, r, now, "meeting.started", meeting["id"], {"mode": r.mode, "leader": r.leader_id}
        )
        return {"tasks": [task], "meeting": meeting}

    def _meeting_result(self, db, project, task_id):
        row = self._task_row(db, project, task_id)
        for ref in json.loads(row["outputs"]):
            if ref["key"] != f"tasks/{task_id}/result":
                continue
            item = self._item(db, project, ref["key"])
            if not item["fresh"] or item["version"] != ref["version"]:
                raise ValueError("前序结果已变化，请检查后重试")
            value = item["value"]
            if isinstance(value, dict) and isinstance(value.get("details"), str):
                parsed = json.loads(value["details"])
                if not isinstance(parsed, dict):
                    raise ValueError("阶段结果必须是 JSON 对象")
                if self._changed_task_dependencies(db, project, row):
                    raise ValueError("前置任务已变化，请重新生成阶段结果")
                return parsed
        raise ValueError("成员未返回可验证的结构化 details 结果")

    def _meeting_groups(self, db, r, now, meeting, plan_task):
        plan = self._meeting_result(db, r.project, plan_task)
        assignments = plan.get("assignments")
        if not isinstance(assignments, list) or len(assignments) != len(meeting["groups"]):
            raise ValueError("主导者需要为每个小组返回一项任务")
        mapping = {}
        for assignment in assignments:
            if not isinstance(assignment, dict):
                raise ValueError("小组分配必须是对象")
            gid, goal = assignment["group_id"], assignment["goal"]
            if gid in mapping or not isinstance(goal, str) or not 1 <= len(goal) <= 5000:
                raise ValueError("小组任务重复、为空或超长")
            mapping[gid] = goal
        if set(mapping) != {g["id"] for g in meeting["groups"]}:
            raise ValueError("主导者分配了未参会的小组")
        meeting["assignments"] = mapping
        self._meeting_leaders(db, r, now, meeting, [plan_task])

    def _meeting_leaders(self, db, r, now, meeting, dependencies):
        discussion = meeting["mode"] == "discussion"
        meeting["stage"] = "reviewing" if discussion else "delegating"
        meeting["waiting"] = []
        for group in meeting["groups"]:
            lead = next(m for m in group["members"] if m["relationship"] == "leader")
            instruction = (
                "分析本组可用成员的特性、能力和任务目标，判断能否完成。不能完成时明确原因、缺少的能力、建议交给哪个小组。"
                if discussion
                else "把本组目标拆解为成员可执行的工作，写出具体交付物和验收标准。每项任务提供本组唯一 id 和 depends_on 数组；确实独立时用空数组。有先后关系必须引用前置任务 id；跨组依赖用 小组ID/任务ID。只分配给本组参会成员，每组最多 12 项。"
            )
            schema = (
                '{"can_complete":true,"reason":"能力判断、风险及调整建议"}'
                if discussion
                else '{"tasks":[{"id":"implement","depends_on":[],"member_id":"本组成员ID","title":"任务名称","description":"具体工作与验收标准"}]}'
            )
            task = self._meeting_task(
                db,
                r,
                now,
                meeting,
                lead["id"],
                "组长评估" if discussion else "组长分工",
                instruction
                + "\n本组ID："
                + group["id"]
                + "\n本组目标："
                + meeting["assignments"][group["id"]]
                + self._structured_instruction(schema),
                dependencies,
            )
            meeting["waiting"].append(task["task_id"])

    def _meeting_advance(self, db, r, now, meeting):
        rows = [self._task_row(db, r.project, tid) for tid in meeting["waiting"]]
        if not rows or any(not self._delivery_accepted(db, r.project, t) for t in rows):
            return
        stage = meeting["stage"]
        if stage == "planning":
            self._meeting_groups(db, r, now, meeting, meeting["waiting"][0])
        elif stage == "reviewing":
            results = [self._meeting_result(db, r.project, t["task_id"]) for t in rows]
            if any(
                type(v.get("can_complete")) is not bool
                or not isinstance(v.get("reason"), str)
                or not v["reason"].strip()
                for v in results
            ):
                raise ValueError("组长需要返回 can_complete 布尔值及具体 reason")
            if all(v["can_complete"] for v in results):
                meeting["stage"] = "ready_for_execution"
                self._decision_create(
                    db,
                    r,
                    now,
                    meeting["id"],
                    None,
                    "execution_gate",
                    {
                        "question": "讨论已达成一致，下一步如何处理？",
                        "context": "各组长已评估方案。可保存方案结束讨论，或确认后进入开发。",
                        "options": ["仅保存方案", "确认并执行", "补充要求后重新讨论"],
                    },
                )
            elif meeting["round"] >= 3:
                meeting["stage"] = "needs_attention"
                meeting["error"] = "已完成三轮讨论，仍有组长提出异议。请调整成员或目标后重新开会。"
            else:
                meeting["round"] += 1
                self._meeting_plan(db, r, now, meeting, meeting["waiting"])
        elif stage == "delegating":
            work = []
            for group, row in zip(meeting["groups"], rows, strict=True):
                result = self._meeting_result(db, r.project, row["task_id"])
                entries = result.get("tasks")
                if not isinstance(entries, list) or not 1 <= len(entries) <= 12:
                    raise ValueError("组长需要返回 1–12 项成员任务")
                for index, entry in enumerate(entries):
                    if not isinstance(entry, dict):
                        raise ValueError("成员任务必须是对象")
                    if entry["member_id"] not in {m["id"] for m in group["members"]}:
                        raise ValueError("组长分配了本组以外的成员")
                    if (
                        not isinstance(entry["description"], str)
                        or not 1 <= len(entry["description"]) <= 6000
                        or not isinstance(entry["title"], str)
                        or not 1 <= len(entry["title"]) <= 160
                    ):
                        raise ValueError("成员任务名称或说明不合法")
                    local_id = entry.get("id", f"step-{index + 1}")
                    if not isinstance(local_id, str) or not re.fullmatch(
                        r"[a-zA-Z0-9][a-zA-Z0-9_.:@-]{0,63}", local_id
                    ):
                        raise ValueError("成员任务 id 无效")
                    key = group["id"] + "/" + local_id
                    if any(node["key"] == key for node in work):
                        raise ValueError("成员任务 id 重复")
                    depends = entry.get("depends_on")
                    if depends is None:
                        # Old completed plans had only an ordered list. Preserve
                        # that order rather than assuming their work is independent.
                        dependencies = [work[-1]["key"]] if work else []
                    else:
                        if (
                            not isinstance(depends, list)
                            or len(depends) > 24
                            or any(not isinstance(dep, str) or not dep for dep in depends)
                        ):
                            raise ValueError("depends_on 必须是最多 24 项任务 ID 数组")
                        dependencies = [
                            dep if "/" in dep else group["id"] + "/" + dep for dep in depends
                        ]
                        if len(set(dependencies)) != len(dependencies):
                            raise ValueError("前置任务不能重复")
                    work.append(
                        dict(key=key, entry=entry, parent=row["task_id"], dependencies=dependencies)
                    )
            keys = {node["key"] for node in work}
            if any(dep not in keys for node in work for dep in node["dependencies"]):
                raise ValueError("成员任务引用了不存在的前置任务")
            pending, ordered, resolved = list(work), [], set()
            while pending:
                ready = [node for node in pending if set(node["dependencies"]) <= resolved]
                if not ready:
                    raise ValueError("成员任务存在循环依赖")
                for node in ready:
                    ordered.append(node)
                    resolved.add(node["key"])
                    pending.remove(node)
            created = {}
            for node in ordered:
                entry = node["entry"]
                deps = [node["parent"], *[created[key] for key in node["dependencies"]]]
                task = self._meeting_task(
                    db,
                    r,
                    now,
                    meeting,
                    entry["member_id"],
                    "成员执行",
                    entry["title"]
                    + "\n"
                    + entry["description"]
                    + "\n前置步骤完成后，先阅读其实际发布结果再执行。保留现有文件成果，如实报告修改、验证和未解决问题。",
                    deps,
                )
                created[node["key"]] = task["task_id"]
            meeting["stage"], meeting["waiting"] = "executing", list(created.values())
        elif stage == "executing":
            meeting["stage"] = "done"
        self._event(
            db,
            r,
            now,
            "meeting.advanced",
            meeting["id"],
            {"stage": meeting["stage"], "round": meeting["round"]},
        )

    def advance_meetings(self, project):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute(
                "SELECT definition FROM meetings WHERE project=?", (project,)
            ).fetchall():
                meeting = json.loads(row["definition"])
                if (
                    meeting.get("paused")
                    or db.execute(
                        "SELECT 1 FROM decisions WHERE project=? AND meeting_id=? AND status='pending'",
                        (project, meeting["id"]),
                    ).fetchone()
                ):
                    continue
                if meeting["stage"] not in (
                    "planning",
                    "reviewing",
                    "delegating",
                    "executing",
                ) or meeting.get("error"):
                    continue
                r = SimpleNamespace(
                    project=project, actor="office-coordinator", request_id=meeting["id"]
                )
                db.execute("SAVEPOINT meeting_step")
                try:
                    self._meeting_advance(db, r, self.clock(), meeting)
                except (ValueError, KeyError, TypeError, CoordinationError) as exc:
                    db.execute("ROLLBACK TO meeting_step")
                    meeting = json.loads(row["definition"])
                    meeting["error"] = "阶段结果需要检查：" + str(exc)[:500]
                db.execute("RELEASE meeting_step")
                self._meeting_save(db, r, meeting)
            db.commit()

    def _meetings_list(self, db, r, now):
        assignments = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM meeting_tasks WHERE project=? ORDER BY rowid LIMIT 1001",
                (r.project,),
            )
        ]
        return {
            "meetings": [
                json.loads(row["definition"])
                for row in db.execute(
                    "SELECT definition FROM meetings WHERE project=? ORDER BY rowid DESC LIMIT 100",
                    (r.project,),
                )
            ],
            "assignments": assignments[:1000],
            "assignments_truncated": len(assignments) > 1000,
        }

    def _meeting_execute(self, db, r, now):
        row = db.execute(
            "SELECT definition FROM meetings WHERE project=? AND meeting_id=?",
            (r.project, r.meeting_id),
        ).fetchone()
        if not row:
            raise CoordinationError("not_found", "会议不存在", status=404)
        meeting = json.loads(row["definition"])
        if meeting.get("paused") or meeting["stage"] != "ready_for_execution":
            raise CoordinationError(
                "invalid_transition", "需要所有组长完成讨论并认可方案后才能执行"
            )
        for task_id in meeting["waiting"]:
            if (
                self._task_row(db, r.project, task_id)["status"] != "done"
                or self._meeting_result(db, r.project, task_id).get("can_complete") is not True
            ):
                raise CoordinationError("stale_discussion", "组长评估已变化，请重新讨论")
        db.execute(
            "UPDATE decisions SET status='answered',answer='确认执行',revision=revision+1,updated_at=? WHERE project=? AND meeting_id=? AND kind='execution_gate' AND status='pending'",
            (now, r.project, r.meeting_id),
        )
        meeting["mode"] = "execution"
        self._meeting_leaders(db, r, now, meeting, meeting["waiting"])
        self._meeting_save(db, r, meeting)
        self._event(db, r, now, "meeting.execution_requested", meeting["id"], {})
        return meeting

    def _meeting_retry(self, db, r, now):
        row = db.execute(
            "SELECT definition FROM meetings WHERE project=? AND meeting_id=?",
            (r.project, r.meeting_id),
        ).fetchone()
        if not row:
            raise CoordinationError("not_found", "会议不存在", status=404)
        meeting = json.loads(row["definition"])
        if meeting.get("paused"):
            raise CoordinationError(
                "meeting_paused", "Resume the meeting before retrying its stage"
            )
        if not meeting.get("error") or meeting["stage"] not in (
            "planning",
            "reviewing",
            "delegating",
        ):
            raise CoordinationError(
                "invalid_transition", "只有阶段结果格式错误时可重新生成；请先处理失败或暂停的任务"
            )
        for task_id in meeting["waiting"]:
            task = self._task_row(db, r.project, task_id)
            self._task_reopen(
                db,
                SimpleNamespace(
                    project=r.project,
                    actor=r.actor,
                    request_id=r.request_id,
                    task_id=task_id,
                    expected_revision=task["revision"],
                ),
                now,
            )
        meeting["error"] = None
        self._meeting_save(db, r, meeting)
        return meeting
