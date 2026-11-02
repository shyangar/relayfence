"""Operational read model assembled in a single ledger transaction."""

from collections import Counter


class ProjectOverview:
    def _project_overview(self, db, r, now):
        counts = Counter()
        attention = []
        completed = stale = cancelled = total = 0
        for row in db.execute(
            "SELECT * FROM tasks WHERE project=? ORDER BY updated_at DESC,task_id", (r.project,)
        ):
            task = self._task_view(db, r.project, row, now)
            status = task["display_status"]
            counts[status] += 1
            total += 1
            is_stale = bool(task["result_freshness"] and not task["result_freshness"]["fresh"])
            cancelled += status == "cancelled"
            completed += status == "done" and not is_stale
            stale += status == "done" and is_stale
            reason = "stale_result" if status == "done" and is_stale else status
            if reason in {
                "failed",
                "lease_expired",
                "waiting_user",
                "awaiting_review",
                "stale_result",
                "paused",
            }:
                if len(attention) < 20:
                    attention.append(
                        {
                            "task_id": task["task_id"],
                            "title": task["title"],
                            "reason": reason,
                            "revision": task["revision"],
                            "failure": task["failure"],
                        }
                    )
        decisions = db.execute(
            "SELECT count(*) FROM decisions WHERE project=? AND status='pending'", (r.project,)
        ).fetchone()[0]
        eligible = total - cancelled
        return {
            "project": r.project,
            "observed_at": now,
            "counts": dict(counts),
            "progress": {
                "total": total,
                "eligible": eligible,
                "completed": completed,
                "cancelled": cancelled,
                "stale": stale,
                "percent": round(100 * completed / eligible) if eligible else 0,
            },
            "pending_decisions": decisions,
            "attention": attention,
            "attention_total": sum(
                counts[s]
                for s in ("failed", "lease_expired", "waiting_user", "awaiting_review", "paused")
            )
            + stale,
        }
