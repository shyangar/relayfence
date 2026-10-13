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


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: Name = "default"


class Mutation(Request):
    actor: Name
    request_id: Name


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


class RolesList(Page):
    pass


class TaskCreate(Mutation):
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


class TasksList(Page):
    available_only: bool = False
    status: (
        Literal["ready", "running", "done", "paused", "cancelled", "failed", "blocked"] | None
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


class TaskAssign(TaskReopen):
    role_id: Name | None = None


class TaskUpdate(TaskCreate):
    expected_revision: int = Field(ge=1, le=MAX_INTEGER, strict=True)


class TaskControl(TaskReopen):
    action: Literal["pause", "resume", "cancel", "retry"]
    reason: str = Field(default="", max_length=2000)


class TaskFail(LeaseMutation):
    error_code: Name
    message: str = Field(min_length=1, max_length=2000)


class RunnerHeartbeat(Mutation):
    runner_id: Name
    active_task: Name | None = None


class EventsList(Request):
    after: int = Field(default=0, ge=0, le=MAX_INTEGER, strict=True)
    limit: int = Field(default=50, ge=1, le=200, strict=True)
    subject: Name | None = None


OPERATIONS: dict[str, tuple[type[Request], str]] = {
    "task_update": (
        TaskUpdate,
        "Admin: edit inactive work with revision CAS, including priority, due date, role and prerequisite tasks.",
    ),
    "task_control": (
        TaskControl,
        "Admin: pause, resume, cancel or retry work with revision CAS. Pause/cancel revoke a live lease.",
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
    "events_list": (
        EventsList,
        "Poll durable project events after a cursor. Events contain metadata, not values or lease tokens.",
    ),
}
