"""Versioned public request contract shared by HTTP, MCP and the Python client."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Name = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_./:@-]*$")
]
MAX_INTEGER = 2**63 - 1
Version = Annotated[int, Field(ge=0, le=MAX_INTEGER, strict=True)]
ModelId = Annotated[
    str, Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:/@+\-]*$")
]
ParameterValue = Annotated[str, Field(min_length=1, max_length=128)]


class ModelOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: ModelId
    parameters: dict[Name, list[ParameterValue]] = Field(default_factory=dict, max_length=16)

    @model_validator(mode="after")
    def bounded_options(self):
        if any(not v or len(v) > 32 or len(v) != len(set(v)) for v in self.parameters.values()):
            raise ValueError("Each parameter needs 1-32 distinct allowed values")
        return self


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: ModelId | None = None
    parameters: dict[Name, ParameterValue] = Field(default_factory=dict, max_length=16)
    # Skill names resolved against the local installation; never paths or commands.
    mcp_servers: list[Name] | None = Field(default=None, max_length=32)
    skills: list[Name] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def distinct_skills(self):
        if self.mcp_servers is not None and len(set(self.mcp_servers)) != len(self.mcp_servers):
            raise ValueError("MCP servers must be unique")
        if len(set(self.skills)) != len(self.skills):
            raise ValueError("Skills must be unique")
        return self


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: Name = "default"


class Mutation(Request):
    actor: Name
    request_id: Name


class TaskActivityEntry(BaseModel):
    """One observable step inside a run, forwarded by the executing dispatcher.

    The dispatcher owns the only process that can see a vendor's stream, so it
    reports what happened; the ledger never reads a client's filesystem.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["note", "command", "output", "result", "error"]
    text: str = Field(min_length=1, max_length=6000)
    # A short label such as the command that ran, kept separate from its output.
    title: str = Field(default="", max_length=200)
    status: Literal["running", "completed", "failed"] = "completed"
    at: float | None = None
    source_agent: str | None = Field(default=None, min_length=1, max_length=128)
    parent_agent: str | None = Field(default=None, min_length=1, max_length=128)


class TaskActivity(Mutation):
    """Record observed run activity for one task so operators can follow it live."""

    task_id: Name
    fence: int | None = Field(default=None, ge=1, le=MAX_INTEGER, strict=True)
    lease_token: str | None = Field(default=None, min_length=20, max_length=128)
    member_id: Name | None = None
    entries: list[TaskActivityEntry] = Field(min_length=1, max_length=40)


class QuestionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    context: str = Field(default="", max_length=4000)
    options: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def valid_options(self):
        if not self.question.strip() or any(not v.strip() or len(v) > 300 for v in self.options):
            raise ValueError("Question and options must be nonblank and bounded")
        return self


class StatePut(Mutation):
    key: Name
    expected_version: Version
    value: JsonValue


class ItemGet(Request):
    key: Name
    version: Annotated[int, Field(ge=1, le=MAX_INTEGER, strict=True)] | None = None


class Page(Request):
    after: str = Field(default="", max_length=128)
    limit: int = Field(default=50, ge=1, le=200, strict=True)


class ItemsList(Page):
    pass


class TaskActivityList(Page):
    task_id: Name | None = None
    after_id: int = 0


class SnapshotList(Request):
    limit: int = Field(default=50, ge=1, le=200, strict=True)


class AgentPut(Mutation):
    agent_id: Name
    expected_revision: Version
    label: str = Field(min_length=1, max_length=160)
    runtime: Name
    mode: Literal["external", "command"] = "external"
    enabled: bool = True
    capabilities: list[Name] = Field(default_factory=list, max_length=32)
    models: list[ModelOption] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def distinct_options(self):
        if len({m.id for m in self.models}) != len(self.models):
            raise ValueError("Model IDs must be unique")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("Capabilities must be unique")
        if self.mode == "command" and not self.models:
            raise ValueError("Command agents need at least one explicitly configured model")
        return self


class AgentGet(Request):
    agent_id: Name


class AgentsList(Page):
    pass


class AgentTokenRotate(Mutation):
    agent_id: Name
    expected_revision: Version


class RolePut(Mutation):
    role_id: Name
    expected_revision: Version
    label: str = Field(min_length=1, max_length=160)
    instructions: str = Field(default="", max_length=8000)
    deliverables: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        default_factory=list, max_length=16
    )
    required_capabilities: list[Name] = Field(default_factory=list, max_length=32)
    agent_id: Name
    selection: Selection = Field(default_factory=Selection)


class RoleGet(Request):
    role_id: Name


class SkillList(Request):
    pass


class RolesList(Page):
    pass


class TaskCreate(Mutation):
    review_required: bool = False
    task_id: Name
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=8000)
    inputs: list[Name] = Field(default_factory=list, max_length=32)
    role_id: Name | None = None
    priority: int = Field(default=1, ge=0, le=3, strict=True)
    due_at: float | None = Field(default=None, ge=0, allow_inf_nan=False, strict=True)
    depends_on: list[Name] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def distinct_inputs(self):
        if len(set(self.inputs)) != len(self.inputs):
            raise ValueError("input keys must be unique")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("dependency tasks must be unique")
        return self


class TaskGet(Request):
    task_id: Name


class TaskInspect(TaskGet):
    before: int = Field(default=0, ge=0, le=MAX_INTEGER, strict=True)
    limit: int = Field(default=30, ge=1, le=100, strict=True)


class TasksList(Page):
    available_only: bool = False
    status: (
        Literal[
            "ready",
            "running",
            "done",
            "paused",
            "cancelled",
            "failed",
            "blocked",
            "awaiting_review",
            "waiting_user",
            "lease_expired",
        ]
        | None
    ) = None
    query: str = Field(default="", max_length=240)
    role_id: Name | None = None


class TaskClaim(Mutation):
    task_id: Name
    lease_seconds: int = Field(default=300, ge=1, le=3600, strict=True)
    selection: Selection | None = None


class LeaseMutation(Mutation):
    task_id: Name
    fence: int = Field(ge=1, le=MAX_INTEGER, strict=True)
    lease_token: str = Field(min_length=20, max_length=128)


class TaskHeartbeat(LeaseMutation):
    lease_seconds: int = Field(default=300, ge=1, le=3600, strict=True)


class TaskRelease(LeaseMutation):
    pass


class TaskQuestion(LeaseMutation):
    question: QuestionPayload


class DecisionRespond(Mutation):
    decision_id: Name
    expected_revision: int = Field(ge=1, strict=True)
    action: Literal["continue", "revise", "finish_discussion", "cancel"]
    answer: str = Field(default="", max_length=4000)


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Name
    expected_version: Version
    value: JsonValue


class TaskComplete(LeaseMutation):
    outputs: list[Output] = Field(min_length=1, max_length=16)
    reported_model: ModelId | None = None

    @model_validator(mode="after")
    def distinct_outputs(self):
        if len({output.key for output in self.outputs}) != len(self.outputs):
            raise ValueError("output keys must be unique")
        return self


class TaskReopen(Mutation):
    task_id: Name
    expected_revision: int = Field(ge=1, le=MAX_INTEGER, strict=True)


class TaskReview(TaskReopen):
    action: Literal["approve", "reject"]
    note: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def valid_evidence(self):
        if not self.note.strip() or any(not v.strip() or len(v) > 500 for v in self.evidence):
            raise ValueError("Review note and evidence must be nonblank and bounded")
        return self


class TaskAssign(TaskReopen):
    role_id: Name | None = None


class TaskUpdate(TaskCreate):
    review_required: bool | None = None
    expected_revision: int = Field(ge=1, le=MAX_INTEGER, strict=True)


class TaskControl(TaskReopen):
    action: Literal["pause", "resume", "cancel", "retry"]
    reason: str = Field(default="", max_length=2000)


class TaskDelete(TaskReopen):
    """Revision-CAS deletion; the task row and its child rows are removed."""


class TaskFail(LeaseMutation):
    error_code: Name
    message: str = Field(min_length=1, max_length=2000)


class RunnerHeartbeat(Mutation):
    runner_id: Name
    active_task: Name | None = None


class OfficeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=128)
    model: ModelId
    parameters: dict[Name, ParameterValue] = Field(default_factory=dict, max_length=16)


class OfficeMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Name
    name: str = Field(min_length=1, max_length=60)
    position: Literal[
        "lead",
        "product",
        "architect",
        "frontend",
        "backend",
        "qa",
        "security",
        "reviewer",
        "custom",
    ]
    custom_position: str | None = Field(default=None, min_length=1, max_length=60)
    prompt: str | None = Field(default=None, min_length=1, max_length=8000)
    avatar: int = Field(default=0, ge=0, le=23, strict=True)
    gender: Literal["female", "male", "neutral"] = "neutral"
    agent_id: Name | None = None
    provider: str = Field(min_length=1, max_length=128)
    model: ModelId
    parameters: dict[Name, ParameterValue] = Field(default_factory=dict, max_length=16)
    relationship: Literal["leader", "native_subagent", "independent_session"] = (
        "independent_session"
    )
    session_id: str | None = Field(default=None, max_length=200)
    fallback_models: list[OfficeModel] = Field(default_factory=list, max_length=4)
    # Installed skill names enabled for this member; validated against the runtime.
    mcp_servers: list[Name] | None = Field(default=None, max_length=32)
    skills: list[Name] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def distinct_member_skills(self):
        if self.mcp_servers is not None and len(set(self.mcp_servers)) != len(self.mcp_servers):
            raise ValueError("MCP servers must be unique")
        if len(set(self.skills)) != len(self.skills):
            raise ValueError("Skills must be unique")
        return self

    @model_validator(mode="after")
    def member_instructions(self):
        if self.position == "custom" and (
            self.custom_position is None or not self.custom_position.strip()
        ):
            raise ValueError("A custom position needs a nonempty title")
        if self.prompt is not None and not self.prompt.strip():
            raise ValueError("Member prompt must contain instructions or be null for defaults")
        return self


class OfficeGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Name
    name: str = Field(min_length=1, max_length=60)
    software: Literal["codex", "opencode", "zcode", "dsh"]
    members: list[OfficeMember] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def leader(self):
        if sum(m.relationship == "leader" for m in self.members) != 1:
            raise ValueError("Each group needs exactly one leader")
        return self


class OfficeSnapshotSave(Mutation):
    """Freeze the current office configuration under a human-chosen label."""

    label: str = Field(min_length=1, max_length=120)
    expected_revision: Version


class OfficeSnapshotRestore(Mutation):
    """Overwrite the live configuration with a saved snapshot.

    ``expected_revision`` guards the live configuration, so a concurrent edit is
    reported instead of silently discarded.
    """

    snapshot_id: Name
    expected_revision: Version


class OfficeSnapshotDelete(Mutation):
    snapshot_id: Name


class OfficePut(Mutation):
    expected_revision: Version
    groups: list[OfficeGroup] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_people(self):
        ids = [m.id for g in self.groups for m in g.members]
        if len(ids) != len(set(ids)) or len({g.id for g in self.groups}) != len(self.groups):
            raise ValueError("Group and member IDs must be unique")
        if len(ids) > 48:
            raise ValueError("An office supports up to 48 people")
        route_ids = [
            route
            for g in self.groups
            for m in g.members
            for route in [
                m.id,
                *[f"{m.id}/fallback/{i + 1}" for i in range(len(m.fallback_models))],
            ]
        ]
        if len(route_ids) != len(set(route_ids)) or any(len(r) > 128 for r in route_ids):
            raise ValueError(
                "Member and fallback route IDs must be unique and at most 128 characters"
            )
        return self


class MeetingExecute(Mutation):
    meeting_id: Name


class MeetingControl(MeetingExecute):
    action: Literal["pause", "resume"]


class MeetingRevision(MeetingExecute):
    briefing_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_./:@-]*$"
    )
    changes: str = Field(min_length=1, max_length=4000)


class OfficeTransfer(TaskReopen):
    member_id: Name
    note: str = Field(min_length=1, max_length=2000)


class MemberCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skills: list[Name] | None = Field(default=None, max_length=32)
    mcp_servers: list[Name] | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def distinct(self):
        for value in (self.skills, self.mcp_servers):
            if value is not None and len(value) != len(set(value)):
                raise ValueError("Capability selections must be unique")
        return self


class OfficeDispatch(Mutation):
    member_overrides: dict[Name, MemberCapabilities] = Field(default_factory=dict, max_length=32)
    review_required: bool = False
    leader_id: Name | None = None
    mode: Literal["discussion", "execution"] = "execution"
    expected_revision: Version
    briefing_id: Annotated[
        str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_./:@-]*$")
    ]
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=6000)
    member_ids: list[Name] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_members(self):
        if len(self.member_ids) != len(set(self.member_ids)):
            raise ValueError("Members must be unique")
        return self


class QuotaObserve(Mutation):
    route_id: Name
    status: Literal["available", "exhausted", "rate_limited", "unknown"]
    remaining: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    unit: Literal["tokens", "requests", "credits", "percent"] | None = None
    observed_at: float = Field(ge=0, allow_inf_nan=False)
    expires_at: float = Field(gt=0, allow_inf_nan=False)
    reset_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    source: Literal["provider_api", "runtime_error", "operator"]
    detail: str = Field(default="", max_length=1000)


class OfficeRoutePreview(Request):
    current_route_id: Name
    allow_unknown: bool = False


class OfficeHandoff(Mutation):
    task_id: Name
    expected_revision: Version
    current_route_id: Name
    checkpoint_key: Name
    stopped_confirmed: Literal[True]


class EventsList(Request):
    after: int = Field(default=0, ge=0, le=MAX_INTEGER, strict=True)
    limit: int = Field(default=50, ge=1, le=200, strict=True)
    subject: Name | None = None


class GroupLogsList(Request):
    group_id: Name | None = None
    before: int = Field(default=0, ge=0, le=MAX_INTEGER, strict=True)
    limit: int = Field(default=50, ge=1, le=200, strict=True)
    level: Literal["debug", "info", "warning", "error"] | None = None
    query: str = Field(default="", max_length=200)


class TeamGroupsList(Request):
    project_filter: Name | None = None
    query: str = Field(default="", max_length=200)


OPERATIONS: dict[str, tuple[type[Request], str]] = {
    "task_inspect": (TaskInspect, "Inspect paginated execution attempts, reviews and handoffs."),
    "task_review": (TaskReview, "Admin: accept a fresh delivery or return it for revision."),
    "task_question": (
        TaskQuestion,
        "Pause the current attempt and request a durable user decision.",
    ),
    "decisions_list": (Request, "Read pending and recent user decisions for this project."),
    "decision_respond": (DecisionRespond, "Admin: answer a pending decision with revision CAS."),
    "meeting_revision": (
        MeetingRevision,
        "Admin: create a new iteration of a completed objective, retaining prior results.",
    ),
    "office_activity": (
        Request,
        "Read bounded member progress and published conversation excerpts.",
    ),
    "meeting_control": (
        MeetingControl,
        "Admin: pause or resume the entire briefing, preserving manually paused tasks.",
    ),
    "office_transfer": (
        OfficeTransfer,
        "Admin: transfer inactive task with handoff notes and revision CAS.",
    ),
    "meetings_list": (Request, "Read durable meeting stages and member assignments."),
    "meeting_execute": (MeetingExecute, "Admin: execute an agreed discussion plan."),
    "meeting_retry": (MeetingExecute, "Admin: retry invalid completed stage outputs."),
    "office_get": (Request, "Read office groups, positions and current configuration revision."),
    "project_overview": (
        Request,
        "Read consistent progress and recovery attention; cancelled work and stale results do not count as completed progress.",
    ),
    "capabilities": (
        Request,
        "List the operations this running process supports, newest browser assets first.",
    ),
    "task_activity": (
        TaskActivity,
        "Record observed run activity for a task so operators can follow it live.",
    ),
    "task_activity_list": (
        TaskActivityList,
        "Page through recorded run activity, oldest first.",
    ),
    "office_put": (OfficePut, "Admin: save office groups and people using revision CAS."),
    "office_snapshot_list": (
        SnapshotList,
        "List saved office configuration snapshots, newest first.",
    ),
    "office_snapshot_save": (
        OfficeSnapshotSave,
        "Admin: archive the current office groups under a named snapshot.",
    ),
    "office_snapshot_restore": (
        OfficeSnapshotRestore,
        "Admin: replace the live office groups with a saved snapshot using revision CAS.",
    ),
    "office_snapshot_delete": (
        OfficeSnapshotDelete,
        "Admin: remove a saved office snapshot; the live configuration is untouched.",
    ),
    "team_groups_list": (
        TeamGroupsList,
        "Read groups saved by every local project office, with project attribution.",
    ),
    "office_dispatch": (
        OfficeDispatch,
        "Admin: turn a briefing into parallel, role-bound tasks; meeting briefings keep their staged flow.",
    ),
    "quota_observe": (
        QuotaObserve,
        "Admin: record a sourced, expiring quota observation. Never submit credentials.",
    ),
    "quota_list": (Request, "Read latest quota observations with freshness flags."),
    "office_route_preview": (
        OfficeRoutePreview,
        "Choose a route, preferring models inside the current software.",
    ),
    "office_handoff": (
        OfficeHandoff,
        "Admin: hand off a stopped quota-failed task with a durable checkpoint and revision CAS.",
    ),
    "task_update": (
        TaskUpdate,
        "Admin: edit inactive work with revision CAS, including priority, due date, role and prerequisite tasks.",
    ),
    "task_control": (
        TaskControl,
        "Admin: pause, resume, cancel or retry work with revision CAS. Pause/cancel revoke a live lease.",
    ),
    "task_delete": (
        TaskDelete,
        "Admin: permanently delete a stopped task and its child rows; dependents must be removed first.",
    ),
    "task_fail": (
        TaskFail,
        "Record a failed attempt using its live lease. A retry requires an explicit operator action.",
    ),
    "runner_heartbeat": (
        RunnerHeartbeat,
        "Publish a local dispatcher's availability for the authenticated agent instance.",
    ),
    "runners_list": (Request, "List registered local runners and their recent heartbeat status."),
    "agent_put": (
        AgentPut,
        "Admin: create/update an agent's model catalogue and capabilities using revision CAS.",
    ),
    "agent_get": (
        AgentGet,
        "Read an agent profile. Model options are operator-configured, not provider discovery.",
    ),
    "agents_list": (
        AgentsList,
        "Page through registered agent instances; credentials are never listed.",
    ),
    "agent_token_rotate": (
        AgentTokenRotate,
        "Admin: issue/rotate an instance credential. Keep the returned token private; old credentials stop working.",
    ),
    "role_put": (
        RolePut,
        "Admin: bind a role to an agent and supported model/parameters using revision CAS.",
    ),
    "role_get": (RoleGet, "Read a role's instructions, deliverables and agent/model selection."),
    "roles_list": (RolesList, "Page through role bindings."),
    "mcp_list": (SkillList, "List local MCP metadata without connecting or exposing credentials."),
    "skill_list": (
        SkillList,
        "List installed Agent Skills grouped by runtime, with the roots that were scanned.",
    ),
    "task_assign": (
        TaskAssign,
        "Admin: assign or clear a role on ready/expired work. Uses revision CAS; live work must be released first.",
    ),
    "state_put": (
        StatePut,
        "Create/update a shared state key with compare-and-swap. Use version 0 to create.",
    ),
    "item_get": (
        ItemGet,
        "Read a state/artifact revision, its current version and dependency freshness.",
    ),
    "items_list": (ItemsList, "Page through current state and artifact heads in key order."),
    "task_create": (
        TaskCreate,
        "Create work. Declare every shared input key; claiming captures their versions.",
    ),
    "task_get": (
        TaskGet,
        "Read task status, revision, lease deadline and result freshness. Never exposes lease tokens.",
    ),
    "tasks_list": (
        TasksList,
        "Page through tasks. Available means ready or an expired running lease.",
    ),
    "task_claim": (
        TaskClaim,
        "Atomically claim ready/expired work. Save fence and secret lease_token; inputs are a consistent snapshot.",
    ),
    "task_heartbeat": (
        TaskHeartbeat,
        "Extend a live lease. An expired or replaced lease cannot be renewed.",
    ),
    "task_release": (
        TaskRelease,
        "Release your live lease to ready, allowing a fresh claim and input snapshot.",
    ),
    "task_complete": (
        TaskComplete,
        "Atomically publish artifacts and finish work only if lease and all captured inputs are still valid.",
    ),
    "task_reopen": (
        TaskReopen,
        "Explicitly reopen a completed task using its current revision. The next claim captures fresh inputs.",
    ),
    "group_logs_list": (
        GroupLogsList,
        "Page through durable per-group logs, newest first, with bounded analysis.",
    ),
    "events_list": (
        EventsList,
        "Poll durable project events after a cursor. Events contain metadata, not values or lease tokens.",
    ),
}
