"""Versioned public request contract shared by HTTP, MCP and the Python client."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Name = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_./:@-]*$")
]
MAX_INTEGER = 2**63 - 1
Version = Annotated[int, Field(ge=0, le=MAX_INTEGER, strict=True)]


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


class TaskCreate(Mutation):
    task_id: Name
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=8000)
    inputs: list[Name] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def distinct_inputs(self):
        if len(set(self.inputs)) != len(self.inputs):
            raise ValueError("input keys must be unique")
        return self


class TaskGet(Request):
    task_id: Name


class TasksList(Page):
    available_only: bool = False


class TaskClaim(Mutation):
    task_id: Name
    lease_seconds: int = Field(default=300, ge=1, le=3600, strict=True)


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

    @model_validator(mode="after")
    def distinct_outputs(self):
        if len({output.key for output in self.outputs}) != len(self.outputs):
            raise ValueError("output keys must be unique")
        return self


class TaskReopen(Mutation):
    task_id: Name
    expected_revision: int = Field(ge=1, le=MAX_INTEGER, strict=True)


class EventsList(Request):
    after: int = Field(default=0, ge=0, le=MAX_INTEGER, strict=True)
    limit: int = Field(default=50, ge=1, le=200, strict=True)


OPERATIONS: dict[str, tuple[type[Request], str]] = {
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
