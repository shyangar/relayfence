# Public protocol, v1

HTTP operations are `POST /v1/<operation>` with a JSON body and `Authorization: Bearer <token>`. MCP exposes the same operation names; put the body inside `{"request": {...}}`. The service also provides authenticated `GET /v1/schema` for the complete generated JSON schemas.

Every request has `project` (default `default`). Every mutation requires `actor` and `request_id`. Names are 1–128 characters, begin with an ASCII letter/digit, and then allow letters/digits and `_./:@-`. Do not put secrets in names.

## Operations

| Operation | Additional request fields | Main result |
| --- | --- | --- |
| `state_put` | `key, expected_version, value` | New immutable state revision |
| `item_get` | `key`; optional positive `version` | Value, version, current head and freshness |
| `items_list` | Optional `after` key, `limit` | Current heads, next cursor |
| `task_create` | `task_id, title`; optional `description, inputs` | Ready task |
| `task_get` | `task_id` | Current status, revision, lease deadline and result freshness |
| `tasks_list` | Optional `after` task ID, `limit, available_only` | Tasks, next cursor |
| `task_claim` | `task_id`; optional `lease_seconds` | Task, secret lease token, input values and versions |
| `task_heartbeat` | Lease fields; optional `lease_seconds` | Extended current task |
| `task_release` | Lease fields | Task returned to ready |
| `task_complete` | Lease fields; `outputs` | Completed task and published artifacts |
| `task_reopen` | `task_id, expected_revision` | Completed task returned to ready |
| `events_list` | Optional integer `after`, `limit` | Events, next cursor, has_more |

Lease fields are `task_id, fence, lease_token` in addition to the mutation fields. Leases default to 300 seconds and accept 1–3,600 seconds. Use sensible renewal margin, such as renewing a 300-second lease every 60 seconds.

Each output is `{"key": "...", "expected_version": 0, "value": ...}`. Completion requires 1–16 distinct outputs; task creation accepts up to 32 distinct inputs. Page limits are 1–200 (default 50). Payloads are limited to 128 KiB of canonical JSON; raw HTTP bodies also have a 128 KiB limit. MCP has additional framing allowance. Floats must be finite; unknown fields are rejected.

`available_only` includes ready tasks and running tasks with expired leases. Claiming can still fail if declared inputs are missing/stale or another client wins the race.

## Example MCP exchange

Create the input:

~~~json
{
  "request": {
    "project": "shop",
    "actor": "planner",
    "request_id": "spec-001",
    "key": "api/login",
    "expected_version": 0,
    "value": {"auth": "passkey"}
  }
}
~~~

Call `task_create` with `task_id="implement-login"` and `inputs=["api/login"]`. Then call `task_claim` as the worker:

~~~json
{
  "request": {
    "project": "shop",
    "actor": "implementation-worker",
    "request_id": "claim-001",
    "task_id": "implement-login",
    "lease_seconds": 300
  }
}
~~~

Keep the returned `data.lease_token` and `data.task.fence`. Use `data.inputs` as the input snapshot. When calling `task_complete`, supply those lease fields and an output such as:

~~~json
{
  "key": "result/login",
  "expected_version": 0,
  "value": {"commit": "<your actual commit SHA>", "report": "<your actual test report>"}
}
~~~

No field verifies the truth of a commit or report. Verification remains a worker/reviewer responsibility.

## Envelopes and errors

~~~json
{"ok": true, "data": {"...": "operation result"}, "replayed": false}
~~~

~~~json
{
  "ok": false,
  "error": {
    "code": "version_conflict",
    "message": "Read the current version before writing",
    "details": {"key": "api/login", "expected_version": 1, "current_version": 2}
  }
}
~~~

MCP returns the envelope in structured content and JSON text, with `isError=true` for domain failures. Schema failures may be emitted by the MCP SDK before the adapter runs; these still carry `isError=true` but are not guaranteed to contain the domain envelope. HTTP uses 200 for success; 409 for coordination conflicts; 400/422 for malformed requests; 401/403/421 for access/origin/host rejection; 404 for missing objects; 413 for oversized input; and 500/503 for internal or storage/transport failures.

| Error | Recovery |
| --- | --- |
| `version_conflict` / `revision_conflict` | Read the current object. Reconcile changes before a new write with a new request ID. |
| `task_busy` | Choose other work or wait for the owner to release/expire; do not force takeover. |
| `stale_input` during claim | Rebuild stale upstream artifacts first. |
| `stale_input` during completion | Release the live lease, claim a new snapshot, and recompute affected work. |
| `lease_lost` / `lease_expired` | Stop using that lease. Inspect task status; a fresh claim is a new ownership attempt. |
| `idempotency_conflict` | The key already identifies a different request. Recover the original request or use a new key for genuinely new work. |
| `dependency_cycle` / `dependency_limit` | Change the task boundaries/dependencies; do not retry unchanged forever. |
| `kind_conflict` | Choose a separate key for state vs artifact. |
| `busy` / `storage_error` / `transport_error` | Outcome may be unknown. Retry the exact original ID and payload with bounded backoff; inspect service health. |

Persist mutation request IDs and payloads before sending when you need recovery across **client** restarts. RelayFence persists the server receipt; it cannot recover a caller's forgotten request ID. A new UUID on every network retry defeats idempotency.

The service has no automatic retry loop or lease keeper. The Python client intentionally returns transport failures instead of inventing a new request. Use new IDs for separate heartbeat operations; an exact heartbeat retry returns the old deadline.

## Polling and freshness

`items_list` and `tasks_list` use lexicographic cursors and return `next_cursor: null` at the end. `events_list` uses monotonically increasing numeric cursors and returns the last delivered cursor even at the end.

Artifacts include `fresh` and `stale_dependencies` with expected/current versions. A historical revision is stale if it has been superseded. A done task retains its historical completion status; `result_freshness` tells you whether its published output references remain current. Reopen and recompute explicitly when needed.
