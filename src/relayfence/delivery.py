"""Delivery acceptance is independent from process completion and execution attempts."""

import json

from .errors import CoordinationError


class Delivery:
    @staticmethod
    def _review_info(db, project, row):
        policy = db.execute(
            "SELECT review_required FROM task_policies WHERE project=? AND task_id=?",
            (project, row["task_id"]),
        ).fetchone()
        review = db.execute(
            "SELECT * FROM task_reviews WHERE project=? AND task_id=? AND fence=?",
            (project, row["task_id"], row["fence"]),
        ).fetchone()
        result = dict(review) if review else None
        if result:
            result["evidence"] = json.loads(result["evidence"])
        return bool(policy and policy[0]), result

    def _delivery_accepted(self, db, project, row):
        required, review = self._review_info(db, project, row)
        return row["status"] == "done" and (
            not required or bool(review and review["status"] == "approved")
        )

    def _task_review(self, db, r, now):
        row = self._task_row(db, r.project, r.task_id)
        self._check_revision(row, r.expected_revision)
        required, review = self._review_info(db, r.project, row)
        if not required or not review or review["status"] != "pending" or row["status"] != "done":
            raise CoordinationError("invalid_transition", "只有待验收的交付可以审核")
        # The worker who produced this attempt cannot approve its own delivery.
        if r.action == "approve" and row["owner"] == r.actor:
            raise CoordinationError("independent_review_required", "请由其他审核人或用户验收")
        view = self._task_view(db, r.project, row, now)
        if r.action == "approve" and not view["result_freshness"]["fresh"]:
            raise CoordinationError("stale_input", "交付已过期，请驳回重做后再验收")
        status = "approved" if r.action == "approve" else "rejected"
        db.execute(
            "UPDATE task_reviews SET status=?,reviewer=?,note=?,evidence=?,updated_at=? "
            "WHERE project=? AND task_id=? AND fence=?",
            (
                status,
                r.actor,
                r.note,
                self.encode(r.evidence),
                now,
                r.project,
                r.task_id,
                row["fence"],
            ),
        )
        if r.action == "reject":
            description = row["description"] + "\n\n验收返工要求：\n" + r.note
            if len(description) > 8000:
                raise CoordinationError("description_limit", "返工要求导致说明超长，请精简")
            self._task_reopen(db, r, now)
            # Remain paused until a deliberate resume. Rejecting is not consent
            # to spend another execution attempt immediately.
            db.execute(
                "UPDATE tasks SET description=?,status='paused' WHERE project=? AND task_id=?",
                (description, r.project, r.task_id),
            )
        else:
            db.execute(
                "UPDATE tasks SET revision=revision+1,updated_at=? WHERE project=? AND task_id=?",
                (now, r.project, r.task_id),
            )
        self._event(
            db,
            r,
            now,
            "task.reviewed",
            r.task_id,
            {"fence": row["fence"], "status": status, "note": r.note, "evidence": r.evidence},
        )
        return self._task_get(db, r, now)

    def _task_inspect(self, db, r, now):
        task = self._task_get(db, r, now)
        # Page by immutable claim event sequence. Heartbeats do not crowd out
        # attempts; earlier failures and takeovers remain available after restart.
        claims = db.execute(
            "SELECT * FROM events WHERE project=? AND subject=? AND kind='task.claimed' "
            "AND (?=0 OR seq<?) ORDER BY seq DESC LIMIT ?",
            (r.project, r.task_id, r.before, r.before, r.limit + 1),
        ).fetchall()
        attempts = []
        endings = ("task.completed", "task.failed", "task.pause", "task.cancel", "task.released")
        for claim in claims[: r.limit]:
            details = json.loads(claim["details"])
            next_claim = db.execute(
                "SELECT seq,created_at FROM events WHERE project=? AND subject=? AND kind='task.claimed' AND seq>? ORDER BY seq LIMIT 1",
                (r.project, r.task_id, claim["seq"]),
            ).fetchone()
            end_seq = next_claim["seq"] if next_claim else 2**63 - 1
            events = db.execute(
                "SELECT kind,details,created_at FROM events WHERE project=? AND subject=? AND seq>? AND seq<? AND kind IN (?,?,?,?,?) ORDER BY seq LIMIT 1",
                (r.project, r.task_id, claim["seq"], end_seq, *endings),
            ).fetchone()
            status = (
                events["kind"].removeprefix("task.")
                if events
                else (
                    "superseded"
                    if next_claim
                    else "lease_expired"
                    if task["lease_expired"]
                    else "running"
                )
            )
            attempts.append(
                {
                    "sequence": claim["seq"],
                    "fence": details.get("fence"),
                    "owner": claim["actor"],
                    "started_at": claim["created_at"],
                    "ended_at": events["created_at"]
                    if events
                    else next_claim["created_at"]
                    if next_claim
                    else None,
                    "status": status,
                    "execution": details.get("execution"),
                    "takeover": details.get("takeover", False),
                    "outcome": json.loads(events["details"]) if events else {},
                }
            )
        reviews = [
            dict(v)
            for v in db.execute(
                "SELECT * FROM task_reviews WHERE project=? AND task_id=? ORDER BY fence DESC LIMIT 100",
                (r.project, r.task_id),
            )
        ]
        for review in reviews:
            review["evidence"] = json.loads(review["evidence"])
        handoffs = [
            {**dict(v), "details": json.loads(v["details"])}
            for v in db.execute(
                "SELECT seq,kind,actor,details,created_at FROM events WHERE project=? AND subject=? AND kind IN ('office.transferred','office.handoff') ORDER BY seq DESC LIMIT 50",
                (r.project, r.task_id),
            )
        ]
        return {
            "task": task,
            "attempts": attempts,
            "reviews": reviews,
            "handoffs": handoffs,
            "next_before": claims[r.limit - 1]["seq"] if len(claims) > r.limit else None,
        }

    def _group_workload(self, db, project, group_id, members, now):
        rows = db.execute(
            "SELECT t.*,a.member_id FROM tasks t JOIN meeting_tasks a ON t.project=a.project AND t.task_id=a.task_id WHERE a.project=? AND a.group_id=?",
            (project, group_id),
        ).fetchall()
        by_member = {
            m["id"]: {
                "member_id": m["id"],
                "running": 0,
                "queued": 0,
                "blocked": 0,
                "awaiting_review": 0,
                "paused": 0,
                "waiting_user": 0,
                "failed": 0,
                "lease_expired": 0,
            }
            for m in members
            if isinstance(m, dict)
        }
        for row in rows:
            target = by_member.get(row["member_id"])
            if target is None:
                continue
            task = self._task_view(db, project, row, now)
            status = task["display_status"]
            key = "queued" if status == "ready" else status
            if key in target:
                target[key] += 1
        return list(by_member.values())
