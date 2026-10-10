"""Agent/role registry, credential checks and immutable execution snapshots."""

import json
import secrets

from .errors import CoordinationError
from .models import Mutation


class Registry:
    # Store supplies encode/digest as static methods to keep this mixin acyclic.
    @staticmethod
    def _registry_row(db, table, project, ident):
        column = "agent_id" if table == "agents" else "role_id"
        row = db.execute(
            f"SELECT * FROM {table} WHERE project=? AND {column}=?", (project, ident)
        ).fetchone()
        if not row:
            raise CoordinationError("not_found", f"{column} does not exist", {column: ident}, 404)
        return row

    @staticmethod
    def _registry_view(row):
        return {
            **json.loads(row["definition"]),
            "revision": row["revision"],
            "updated_at": row["updated_at"],
        }

    def _authorize(self, db, operation, request, principal):
        if principal.admin:
            if (
                isinstance(request, Mutation)
                and db.execute(
                    "SELECT 1 FROM agents WHERE project=? AND agent_id=?",
                    (request.project, request.actor),
                ).fetchone()
            ):
                raise CoordinationError(
                    "agent_credential_required",
                    "Use the instance credential for this actor",
                    status=403,
                )
            return
        row = db.execute(
            "SELECT * FROM agents WHERE token_hash=?", (principal.token_hash,)
        ).fetchone()
        if not row or not json.loads(row["definition"])["enabled"]:
            raise CoordinationError(
                "unauthorized", "Agent credential is invalid or disabled", status=401
            )
        if row["project"] != request.project:
            raise CoordinationError(
                "forbidden", "Credential belongs to another project", status=403
            )
        if isinstance(request, Mutation):
            if request.actor != row["agent_id"]:
                raise CoordinationError(
                    "actor_mismatch", "Actor must match the authenticated instance", status=403
                )
            if operation not in {
                "state_put",
                "task_create",
                "task_claim",
                "task_heartbeat",
                "task_release",
                "task_complete",
            }:
                raise CoordinationError(
                    "admin_required", "This operation needs the admin token", status=403
                )

    def credential_valid(self, token_hash):
        with self.connection() as db:
            row = db.execute(
                "SELECT definition FROM agents WHERE token_hash=?", (token_hash,)
            ).fetchone()
            return bool(row and json.loads(row["definition"])["enabled"])

    @staticmethod
    def _check_revision(row, expected):
        current = row["revision"] if row else 0
        if current != expected:
            raise CoordinationError(
                "revision_conflict",
                "Read the current revision before updating",
                {"current_revision": current},
            )
        return current + 1

    def _registry_put(self, db, r, now, table, ident):
        column = "agent_id" if table == "agents" else "role_id"
        row = db.execute(
            f"SELECT * FROM {table} WHERE project=? AND {column}=?", (r.project, ident)
        ).fetchone()
        revision = self._check_revision(row, r.expected_revision)
        definition = r.model_dump(
            mode="json", exclude={"project", "actor", "request_id", "expected_revision"}
        )
        if (
            not row
            and table == "agents"
            and db.execute(
                "SELECT 1 FROM receipts WHERE project=? AND actor=?", (r.project, ident)
            ).fetchone()
        ):
            raise CoordinationError(
                "actor_in_use", "Choose a new instance ID; this actor already has legacy receipts"
            )
        if table == "agents" and r.actor == ident:
            raise CoordinationError(
                "actor_in_use", "Use a separate admin actor to register an instance"
            )
        db.execute(
            f"INSERT INTO {table}(project,{column},revision,definition,updated_at) VALUES (?,?,?,?,?) "
            f"ON CONFLICT(project,{column}) DO UPDATE SET revision=excluded.revision,definition=excluded.definition,updated_at=excluded.updated_at",
            (r.project, ident, revision, self.encode(definition), now),
        )
        self._event(
            db, r, now, column.removesuffix("_id") + ".updated", ident, {"revision": revision}
        )
        return self._registry_view(self._registry_row(db, table, r.project, ident))

    def _agent_put(self, db, r, now):
        return self._registry_put(db, r, now, "agents", r.agent_id)

    def _agent_get(self, db, r, now):
        return self._registry_view(self._registry_row(db, "agents", r.project, r.agent_id))

    def _registry_list(self, db, r, table):
        column = "agent_id" if table == "agents" else "role_id"
        rows = db.execute(
            f"SELECT * FROM {table} WHERE project=? AND {column}>? ORDER BY {column} LIMIT ?",
            (r.project, r.after, r.limit + 1),
        ).fetchall()
        return {
            table: [self._registry_view(row) for row in rows[: r.limit]],
            "next_cursor": rows[r.limit - 1][column] if len(rows) > r.limit else None,
        }

    def _agents_list(self, db, r, now):
        return self._registry_list(db, r, "agents")

    def _roles_list(self, db, r, now):
        return self._registry_list(db, r, "roles")

    def _role_get(self, db, r, now):
        return self._registry_view(self._registry_row(db, "roles", r.project, r.role_id))

    def _agent_token_rotate(self, db, r, now):
        row = self._registry_row(db, "agents", r.project, r.agent_id)
        revision = self._check_revision(row, r.expected_revision)
        token = secrets.token_urlsafe(32)
        db.execute(
            "UPDATE agents SET token_hash=?,revision=?,updated_at=? WHERE project=? AND agent_id=?",
            (self.digest(token), revision, now, r.project, r.agent_id),
        )
        self._event(db, r, now, "agent.credential_rotated", r.agent_id, {"revision": revision})
        return {"agent": self._agent_get(db, r, now), "token": token}

    @staticmethod
    def _validate_binding(agent, selection, capabilities):
        if not agent["enabled"]:
            raise CoordinationError(
                "agent_disabled", "Enable the selected agent before assigning work"
            )
        missing = sorted(set(capabilities) - set(agent["capabilities"]))
        if missing:
            raise CoordinationError(
                "capability_mismatch", "Agent lacks required capabilities", {"missing": missing}
            )
        model = selection["model"]
        parameters = selection["parameters"]
        if model is None:
            if agent["mode"] == "command" or parameters:
                raise CoordinationError(
                    "model_required",
                    "Choose a model before configuring parameters or command execution",
                )
            return
        option = next((m for m in agent["models"] if m["id"] == model), None)
        if option is None:
            raise CoordinationError(
                "unsupported_model", "Model is not in the selected agent's configured catalogue"
            )
        if any(value not in option["parameters"].get(key, []) for key, value in parameters.items()):
            raise CoordinationError(
                "unsupported_parameters",
                "Parameter is not supported by this model's configured catalogue",
            )

    def _role_put(self, db, r, now):
        agent = self._registry_view(self._registry_row(db, "agents", r.project, r.agent_id))
        self._validate_binding(agent, r.selection.model_dump(), r.required_capabilities)
        return self._registry_put(db, r, now, "roles", r.role_id)

    def _claim_execution(self, db, r, row):
        if not row["role_id"]:
            if r.selection is not None:
                raise CoordinationError(
                    "role_required", "Assign a role before submitting a model selection"
                )
            return None
        role = self._registry_view(self._registry_row(db, "roles", r.project, row["role_id"]))
        agent = self._registry_view(self._registry_row(db, "agents", r.project, role["agent_id"]))
        if r.actor != role["agent_id"]:
            raise CoordinationError(
                "assignment_mismatch",
                "Only the role's assigned instance may claim this task",
                {"agent_id": role["agent_id"]},
                403,
            )
        self._validate_binding(agent, role["selection"], role["required_capabilities"])
        if r.selection is None or r.selection.model_dump() != role["selection"]:
            raise CoordinationError(
                "selection_mismatch",
                "Acknowledge the role's exact model and parameters before claiming",
                {"selection": role["selection"]},
            )
        return {
            "role": role,
            "agent": {k: agent[k] for k in ("agent_id", "revision", "runtime", "mode")},
            "requested": role["selection"],
            "reported_model": None,
            "model_evidence": "unverified",
        }

    def _task_assign(self, db, r, now):
        row = self._task_row(db, r.project, r.task_id)
        self._check_revision(row, r.expected_revision)
        if row["status"] == "done" or (row["status"] == "running" and row["expires_at"] > now):
            raise CoordinationError(
                "invalid_transition",
                "Release live work or reopen completed work before reassigning",
            )
        if r.role_id:
            self._registry_row(db, "roles", r.project, r.role_id)
        db.execute(
            "UPDATE tasks SET role_id=?,status='ready',revision=revision+1,owner=NULL,lease_hash=NULL,expires_at=NULL,execution=NULL,updated_at=? WHERE project=? AND task_id=?",
            (r.role_id, now, r.project, r.task_id),
        )
        self._event(db, r, now, "task.assigned", r.task_id, {"role_id": r.role_id})
        return self._task_get(db, r, now)
