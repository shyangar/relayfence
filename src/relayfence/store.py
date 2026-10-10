"""SQLite ledger. All decisions and their receipts/events share one transaction.

One connection per operation, no process-local ownership or correctness cache.
SQLite files must live on a local disk, never a shared/network filesystem.
"""

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from .errors import CoordinationError
from .identity import ADMIN, Principal
from .models import Mutation, Request
from .registry import Registry

MAX_PAYLOAD_BYTES = 131_072
MAX_GRAPH_NODES = 256


def encode(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Store(Registry):
    encode = staticmethod(encode)
    digest = staticmethod(digest)

    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self.path = Path(path).resolve()
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            # Check the version under the write lock; concurrent starters cannot migrate twice.
            db.execute("BEGIN IMMEDIATE")
            try:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    raise RuntimeError(f"Unsupported database schema {version}; expected 2")
                scripts = (["schema.sql"] if version == 0 else []) + (
                    ["migration_2.sql"] if version < 2 else []
                )
                for name in scripts:
                    sql = files("relayfence").joinpath(name).read_text(encoding="utf-8")
                    for statement in sql.split(";"):
                        if statement.strip():
                            db.execute(statement)
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    def execute(self, operation: str, request: Request, principal: Principal = ADMIN) -> dict:
        payload = request.model_dump(mode="json")
        # Preserve v0.1 receipt hashes when the newly optional fields are absent.
        for field in {
            "task_create": ["role_id"],
            "task_claim": ["selection"],
            "task_complete": ["reported_model"],
        }.get(operation, []):
            if payload.get(field) is None:
                payload.pop(field, None)
        raw = encode({"operation": operation, "request": payload})
        if len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise CoordinationError("payload_too_large", "Request exceeds 128 KiB", status=413)
        mutation = isinstance(request, Mutation)
        with self.connection() as db:
            try:
                db.execute("BEGIN IMMEDIATE" if mutation else "BEGIN")
                now = self.clock()  # Time is sampled after acquiring the write lock.
                self._authorize(db, operation, request, principal)
                if mutation:
                    receipt = db.execute(
                        "SELECT digest,response FROM receipts WHERE project=? AND actor=? AND request_id=?",
                        (request.project, request.actor, request.request_id),
                    ).fetchone()
                    if receipt:
                        if receipt["digest"] != digest(raw):
                            raise CoordinationError(
                                "idempotency_conflict",
                                "This request_id was used for a different operation or payload",
                            )
                        result = json.loads(receipt["response"])
                        result["replayed"] = True
                        db.commit()
                        return result
                result = {
                    "ok": True,
                    "data": getattr(self, "_" + operation)(db, request, now),
                    "replayed": False,
                }
                if mutation:
                    db.execute(
                        "INSERT INTO receipts VALUES (?,?,?,?,?,?)",
                        (
                            request.project,
                            request.actor,
                            request.request_id,
                            digest(raw),
                            encode(result),
                            now,
                        ),
                    )
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _event(db, r, now, kind, subject, details):
        db.execute(
            "INSERT INTO events(project,actor,kind,subject,details,created_at) VALUES (?,?,?,?,?,?)",
            (
                r.project,
                r.actor,
                kind,
                subject,
                encode(details),
                now,
            ),
        )

    @staticmethod
    def _head(db, project, key):
        return db.execute(
            "SELECT * FROM items WHERE project=? AND key=?", (project, key)
        ).fetchone()

    def _check_version(self, db, project, key, expected, kind):
        head = self._head(db, project, key)
        current = head["version"] if head else 0
        if current != expected:
            raise CoordinationError(
                "version_conflict",
                "Read the current version before writing",
                {
                    "key": key,
                    "expected_version": expected,
                    "current_version": current,
                },
            )
        if head and head["kind"] != kind:
            raise CoordinationError(
                "kind_conflict", "A key cannot change between state and artifact", {"key": key}
            )
        return current + 1

    @staticmethod
    def _write_item(db, r, now, key, kind, version, value, dependencies):
        db.execute(
            "INSERT INTO items VALUES (?,?,?,?) ON CONFLICT(project,key) DO UPDATE SET version=excluded.version",
            (r.project, key, kind, version),
        )
        db.execute(
            "INSERT INTO revisions VALUES (?,?,?,?,?,?,?)",
            (
                r.project,
                key,
                version,
                encode(value),
                encode(dependencies),
                r.actor,
                now,
            ),
        )

    def _freshness(self, db, project, refs):
        pending = list(refs)
        seen = set()
        stale = []
        keys = set()
        while pending:
            ref = pending.pop()
            ident = (ref["key"], ref["version"])
            if ident in seen:
                continue
            seen.add(ident)
            keys.add(ref["key"])
            if len(seen) > MAX_GRAPH_NODES:
                raise CoordinationError(
                    "dependency_limit", "Dependency closure exceeds 256 revisions; split the work"
                )
            head = self._head(db, project, ref["key"])
            current = head["version"] if head else 0
            if current != ref["version"]:
                stale.append(
                    {
                        "key": ref["key"],
                        "expected_version": ref["version"],
                        "current_version": current,
                    }
                )
                continue
            row = db.execute(
                "SELECT dependencies FROM revisions WHERE project=? AND key=? AND version=?",
                (project, ref["key"], ref["version"]),
            ).fetchone()
            if row is None:
                raise RuntimeError("Missing revision in ledger")
            pending.extend(json.loads(row["dependencies"]))
        return {
            "fresh": not stale,
            "stale_dependencies": sorted(stale, key=lambda x: x["key"]),
        }, keys

    def _item(self, db, project, key, version=None):
        head = self._head(db, project, key)
        if not head:
            raise CoordinationError("not_found", "Item does not exist", {"key": key}, 404)
        version = head["version"] if version is None else version
        row = db.execute(
            "SELECT * FROM revisions WHERE project=? AND key=? AND version=?",
            (project, key, version),
        ).fetchone()
        if not row:
            raise CoordinationError(
                "not_found", "Revision does not exist", {"key": key, "version": version}, 404
            )
        freshness, _ = self._freshness(db, project, [{"key": key, "version": version}])
        return {
            "key": key,
            "kind": head["kind"],
            "version": version,
            "current_version": head["version"],
            "value": json.loads(row["value"]),
            "dependencies": json.loads(row["dependencies"]),
            "actor": row["actor"],
            "created_at": row["created_at"],
            **freshness,
        }

    def _state_put(self, db, r, now):
        version = self._check_version(db, r.project, r.key, r.expected_version, "state")
        self._write_item(db, r, now, r.key, "state", version, r.value, [])
        self._event(db, r, now, "state.updated", r.key, {"version": version})
        return self._item(db, r.project, r.key)

    def _item_get(self, db, r, now):
        return self._item(db, r.project, r.key, r.version)

    def _items_list(self, db, r, now):
        rows = db.execute(
            "SELECT key FROM items WHERE project=? AND key>? ORDER BY key LIMIT ?",
            (r.project, r.after, r.limit + 1),
        ).fetchall()
        return {
            "items": [self._item(db, r.project, row["key"]) for row in rows[: r.limit]],
            "next_cursor": rows[r.limit - 1]["key"] if len(rows) > r.limit else None,
        }

    @staticmethod
    def _task_row(db, project, task_id):
        row = db.execute(
            "SELECT * FROM tasks WHERE project=? AND task_id=?", (project, task_id)
        ).fetchone()
        if not row:
            raise CoordinationError("not_found", "Task does not exist", {"task_id": task_id}, 404)
        return row

    def _task_view(self, db, project, row, now):
        outputs = json.loads(row["outputs"])
        freshness = self._freshness(db, project, outputs)[0] if outputs else None
        return {
            "task_id": row["task_id"],
            "title": row["title"],
            "description": row["description"],
            "inputs": json.loads(row["inputs"]),
            "status": row["status"],
            "revision": row["revision"],
            "fence": row["fence"],
            "owner": row["owner"],
            "expires_at": row["expires_at"],
            "lease_expired": row["status"] == "running" and row["expires_at"] <= now,
            "input_versions": json.loads(row["snapshot"]),
            "outputs": outputs,
            "result_freshness": freshness,
            "updated_at": row["updated_at"],
            "role_id": row["role_id"],
            "execution": json.loads(row["execution"]) if row["execution"] else None,
        }

    def _task_create(self, db, r, now):
        if r.role_id:
            self._registry_row(db, "roles", r.project, r.role_id)
        if db.execute(
            "SELECT 1 FROM tasks WHERE project=? AND task_id=?", (r.project, r.task_id)
        ).fetchone():
            raise CoordinationError(
                "already_exists", "Task ID already exists", {"task_id": r.task_id}
            )
        db.execute(
            "INSERT INTO tasks(project,task_id,title,description,inputs,status,created_at,updated_at,role_id) VALUES (?,?,?,?,?,'ready',?,?,?)",
            (r.project, r.task_id, r.title, r.description, encode(r.inputs), now, now, r.role_id),
        )
        self._event(db, r, now, "task.created", r.task_id, {"inputs": r.inputs})
        return self._task_get(db, r, now)

    def _task_get(self, db, r, now):
        return self._task_view(db, r.project, self._task_row(db, r.project, r.task_id), now)

    def _tasks_list(self, db, r, now):
        available = (
            "AND (status='ready' OR (status='running' AND expires_at<=?))"
            if r.available_only
            else ""
        )
        params = (
            (r.project, r.after, now, r.limit + 1)
            if r.available_only
            else (r.project, r.after, r.limit + 1)
        )
        rows = db.execute(
            f"SELECT * FROM tasks WHERE project=? AND task_id>? {available} ORDER BY task_id LIMIT ?",
            params,
        ).fetchall()
        return {
            "tasks": [self._task_view(db, r.project, row, now) for row in rows[: r.limit]],
            "next_cursor": rows[r.limit - 1]["task_id"] if len(rows) > r.limit else None,
        }

    def _task_claim(self, db, r, now):
        row = self._task_row(db, r.project, r.task_id)
        execution = self._claim_execution(db, r, row)
        if row["status"] == "done":
            raise CoordinationError(
                "task_done", "Reopen the completed task before claiming it again"
            )
        if row["status"] == "running" and row["expires_at"] > now:
            raise CoordinationError(
                "task_busy",
                "Another live lease owns this task",
                {"expires_at": row["expires_at"], "fence": row["fence"]},
            )
        inputs = [self._item(db, r.project, key) for key in json.loads(row["inputs"])]
        refs = [{"key": item["key"], "version": item["version"]} for item in inputs]
        fresh, _ = self._freshness(db, r.project, refs)
        if not fresh["fresh"]:
            raise CoordinationError(
                "stale_input", "Rebuild stale upstream artifacts before claiming", fresh
            )
        token = secrets.token_urlsafe(32)
        fence = row["fence"] + 1
        db.execute(
            "UPDATE tasks SET status='running',revision=revision+1,fence=?,owner=?,lease_hash=?,expires_at=?,snapshot=?,updated_at=?,execution=? WHERE project=? AND task_id=?",
            (
                fence,
                r.actor,
                digest(token),
                now + r.lease_seconds,
                encode(refs),
                now,
                encode(execution) if execution else None,
                r.project,
                r.task_id,
            ),
        )
        self._event(
            db,
            r,
            now,
            "task.claimed",
            r.task_id,
            {
                "fence": fence,
                "takeover": row["status"] == "running",
                "inputs": refs,
                "execution": execution,
            },
        )
        return {"task": self._task_get(db, r, now), "lease_token": token, "inputs": inputs}

    def _assert_lease(self, db, r, now):
        row = self._task_row(db, r.project, r.task_id)
        if (
            row["status"] != "running"
            or row["fence"] != r.fence
            or row["owner"] != r.actor
            or not secrets.compare_digest(row["lease_hash"] or "", digest(r.lease_token))
        ):
            raise CoordinationError(
                "lease_lost",
                "This lease no longer owns the task. Stop and inspect the task",
                {"current_fence": row["fence"]},
            )
        if row["expires_at"] <= now:
            raise CoordinationError(
                "lease_expired", "Lease expired. Claim again before doing more work"
            )
        return row

    def _task_heartbeat(self, db, r, now):
        row = self._assert_lease(db, r, now)
        deadline = max(row["expires_at"], now + r.lease_seconds)
        db.execute(
            "UPDATE tasks SET expires_at=?,revision=revision+1,updated_at=? WHERE project=? AND task_id=?",
            (deadline, now, r.project, r.task_id),
        )
        self._event(
            db, r, now, "task.heartbeat", r.task_id, {"fence": r.fence, "expires_at": deadline}
        )
        return self._task_get(db, r, now)

    def _task_release(self, db, r, now):
        self._assert_lease(db, r, now)
        db.execute(
            "UPDATE tasks SET status='ready',revision=revision+1,owner=NULL,lease_hash=NULL,expires_at=NULL,updated_at=? WHERE project=? AND task_id=?",
            (now, r.project, r.task_id),
        )
        self._event(db, r, now, "task.released", r.task_id, {"fence": r.fence})
        return self._task_get(db, r, now)

    def _task_complete(self, db, r, now):
        row = self._assert_lease(db, r, now)
        execution = json.loads(row["execution"]) if row["execution"] else None
        if r.reported_model:
            if not execution:
                raise CoordinationError(
                    "role_required", "Model reports require a role execution snapshot"
                )
            requested = execution["requested"]["model"]
            if requested is not None and requested != r.reported_model:
                raise CoordinationError(
                    "model_mismatch", "Reported model differs from the requested model"
                )
            execution["reported_model"] = r.reported_model
            execution["model_evidence"] = "worker_reported"
        refs = json.loads(row["snapshot"])
        fresh, ancestors = self._freshness(db, r.project, refs)
        if not fresh["fresh"]:
            raise CoordinationError(
                "stale_input",
                "Inputs changed. Release, claim again, and recompute before publishing",
                fresh,
            )
        overlap = sorted({output.key for output in r.outputs} & ancestors)
        if overlap:
            raise CoordinationError(
                "dependency_cycle",
                "Outputs cannot overwrite direct or transitive inputs",
                {"keys": overlap},
            )
        # Preflight every output; the transaction also rolls back on any later failure.
        versions = [
            self._check_version(db, r.project, o.key, o.expected_version, "artifact")
            for o in r.outputs
        ]
        published = []
        for output, version in zip(r.outputs, versions, strict=True):
            self._write_item(db, r, now, output.key, "artifact", version, output.value, refs)
            published.append({"key": output.key, "version": version})
        db.execute(
            "UPDATE tasks SET status='done',revision=revision+1,lease_hash=NULL,expires_at=NULL,outputs=?,updated_at=?,execution=? WHERE project=? AND task_id=?",
            (
                encode(published),
                now,
                encode(execution) if execution else None,
                r.project,
                r.task_id,
            ),
        )
        self._event(
            db,
            r,
            now,
            "task.completed",
            r.task_id,
            {"fence": r.fence, "outputs": published, "execution": execution},
        )
        return {
            "task": self._task_get(db, r, now),
            "artifacts": [self._item(db, r.project, o["key"]) for o in published],
        }

    def _task_reopen(self, db, r, now):
        row = self._task_row(db, r.project, r.task_id)
        if row["revision"] != r.expected_revision:
            raise CoordinationError(
                "revision_conflict",
                "Task changed; read its current revision",
                {"current_revision": row["revision"]},
            )
        if row["status"] != "done":
            raise CoordinationError("invalid_transition", "Only completed tasks can be reopened")
        db.execute(
            "UPDATE tasks SET status='ready',revision=revision+1,owner=NULL,updated_at=? WHERE project=? AND task_id=?",
            (now, r.project, r.task_id),
        )
        self._event(db, r, now, "task.reopened", r.task_id, {"revision": r.expected_revision + 1})
        return self._task_get(db, r, now)

    @staticmethod
    def _events_list(db, r, now):
        rows = db.execute(
            "SELECT * FROM events WHERE project=? AND seq>? ORDER BY seq LIMIT ?",
            (r.project, r.after, r.limit + 1),
        ).fetchall()
        events = [{**dict(row), "details": json.loads(row["details"])} for row in rows[: r.limit]]
        return {
            "events": events,
            "next_cursor": events[-1]["seq"] if events else r.after,
            "has_more": len(rows) > r.limit,
        }

    def backup(self, destination: str | Path) -> None:
        """SQLite's online backup includes committed WAL pages in a consistent snapshot."""
        target = Path(destination).resolve()
        if target == self.path or target.exists():
            raise ValueError(
                "Backup destination must be a new file distinct from the live database"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents overwriting an existing backup.
        with target.open("xb"):
            pass
        with self.connection() as source:
            dest = sqlite3.connect(target)
            try:
                source.backup(dest)
            finally:
                dest.close()
