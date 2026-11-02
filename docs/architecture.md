# Architecture and invariants

## Deployment model

One authoritative service owns a SQLite ledger on local disk. HTTP and MCP call the same validated service layer. A stdio MCP bridge forwards operations to that service; it does not open a database or create a second coordinator.

~~~text
MCP HTTP ──────────────────────┐
MCP stdio → bridge → HTTP ──────┼→ Service → Store → local SQLite
Python/HTTP client ────────────┘               └→ receipts + events
~~~

The package has no model provider, agent framework, or FlowTrial dependency. Do not place the SQLite database on NFS, SMB, cloud-synced storage, or a shared network volume. The supported deployment is one service, one host, one trust group.

## Write transaction

Every mutation opens its own connection and executes `BEGIN IMMEDIATE`. There is no process-local ownership map.

1. Acquire SQLite's write lock; only then sample service time.
2. Recheck authenticated instance/project permissions, then look up the receipt under `(project, actor, request_id)`.
3. If it exists, compare the canonical operation/payload digest. Return the original success if identical; otherwise reject.
4. Check current versions, task status, lease and declared dependencies.
5. Write new immutable revisions and advance heads/task status.
6. Write a compact event and a success receipt.
7. Commit together. Any exception rolls back the transaction.

The receipt includes normalized defaults, so omitting a default and explicitly providing its value have the same identity. JSON object order is ignored; array order is significant. Only successful operations consume an idempotency key. Rejected operations write no ledger event or receipt.

State updates are compare-and-swap operations. A missing key has expected version `0`; each successful update creates the next positive version, even if the value happens to be unchanged. A key's state/artifact kind cannot change.

SQLite runs in WAL mode with foreign keys, a 10-second busy timeout and `synchronous=FULL` on each connection. Durability still depends on the local filesystem and storage honoring SQLite's synchronization requests. Read operations use one transaction, so a response is assembled from one database snapshot.

## Ownership

Stored task statuses are `ready`, `running`, `done`, `paused`, `failed`, and `cancelled`. Read models also distinguish blocked dependencies, pending user decisions, pending reviews, and expired leases. A running task with an expired deadline can be claimed again explicitly. Expiration is evaluated on access; it does not require a sweeper or mutate task status by itself.

~~~mermaid
stateDiagram-v2
  [*] --> ready: create
  ready --> running: claim / fence + 1
  running --> running: renew live lease
  running --> running: expired takeover / fence + 1
  running --> ready: release live lease
  ready --> paused: operator pause
  running --> paused: revoke lease
  paused --> ready: resume with new execution
  running --> failed: failed attempt
  failed --> ready: explicit retry
  ready --> cancelled: operator cancel
  running --> cancelled: revoke lease
  cancelled --> ready: explicit retry
  running --> done: validate + publish atomically
  done --> ready: explicit revision-checked reopen
~~~

Ownership requires all of: running status, matching actor, matching fencing number, matching secret lease token, and a deadline strictly later than the service's sampled time. Heartbeats cannot revive expired leases or shorten a valid deadline.

Fence numbers increase per task and survive reopening/restarts. A replacement's higher number makes any old completion invalid. A token is randomly generated for each claim. Public task/event reads never expose tokens or their hashes. The database's saved claim receipt necessarily retains the original token so lost claim responses can be recovered; the database and backups are sensitive.

Time is server wall-clock time. Clock jumps can delay or accelerate expiry, so run on a host with a stable, synchronized clock. Fencing still rejects an old generation after a takeover. A long-running write transaction is serialized ahead of any later claim; there is no external I/O inside the transaction.

**Replayed receipts describe the past.** A replayed claim can contain an expired/replaced lease, and a replayed completion can contain an artifact that has since become stale. Read current status and renew with a new request ID before resuming work. Never reinterpret a replay as a new lease grant.

## Dependencies

Task creation declares input **keys**, not values. Claiming resolves all declared keys to current revisions and captures their values in the returned input snapshot. The stored claim retains those revision references. Inputs may be states or artifacts; an artifact must itself be fresh before a task can claim it.

Completion rechecks the entire transitive dependency closure under the same write lock. Every output automatically depends on every captured input. Callers cannot omit a captured dependency during completion. This deliberately conservative rule may invalidate more work than necessary; split independent work into separate tasks to reduce that.

Revisions are immutable. Freshness is computed when reading or claiming; there is no mass rewrite of downstream records and no automatic rebuild. A revision is stale when it or any required revision is no longer the current head. Traversal deduplicates references, stops at 256 revisions, and fails closed when the bound is exceeded.

Outputs cannot overwrite any direct or transitive input key. This also prevents the common self-invalidating dependency cycle. Multiple output writes are atomic; a conflict in one output rejects the entire completion.

Artifact values are JSON metadata, text, or external references. The service does not fetch URLs, resolve Git SHAs, execute commands, evaluate model output, infer dependencies, or store binary files. The worker executes an operator-configured adapter (built-in Codex, the OpenCode command adapter, or a custom command). The optional local dispatcher may run alongside the server, but executable settings come only from private local files.

Task prerequisites form an acyclic graph. Claim captures prerequisite fence generations and incorporates their outputs into the artifact input snapshot. Reopening/re-executing a prerequisite invalidates the captured generation. The dispatcher polls ready work and sorts by priority/due date. All automatically connected project dispatchers share an `ExecutionCapacity` admission controller (default two active executions per service). A resolved working directory admits one execution at a time, matching the worker's filesystem lock. Different directories can execute concurrently. Queued projects receive a turn before a busy project starts more work; abandoned queue entries expire after 30 seconds. Task IDs are scoped by project when deduplicating candidates.

Admission happens before thread-pool submission and is released after worker cleanup, including submission failure and cancellation. It is process-local scheduling, not an OS CPU/memory quota or a cross-host lock. The filesystem lock, Windows Job Object/POSIX process group, and ledger lease each retain their distinct responsibilities. Independent `dispatch` processes have their own capacity controllers; shared-directory exclusion still comes from the filesystem lock. Failed tasks and expired attempts require explicit recovery.

Each provider identity is polled once per round even when multiple member slots share it. Project dispatchers use explicit pre-poll and admission hooks rather than replacing their `tick` methods; the manager's scheduling lock covers admission/stop transitions, not network polling. A slow provider in one project therefore does not hold the scheduling lock while other projects poll.

`project_overview` assembles progress and recovery attention inside one ledger read transaction. Cancelled steps are excluded from the denominator; pending reviews and stale outputs are excluded from completed progress. The percentage covers all steps in the project, not overall business-goal acceptance. `/local/runtime` separately reports live scheduler health and capacity, which are observations rather than durable task state. Browser refreshes are coalesced with a trailing refresh so a mutation or project switch does not lose its update.

## Role/model snapshots and identity

Agent instances declare software, external/command mode, capabilities and model catalogues. Roles bind one instance to a model/parameter selection with revision CAS. Claim validates the binding under the lease transaction's write lock and snapshots instructions, deliverables and execution configuration. Role edits affect future claims; live owners may finish against their saved configuration. Reassignment is limited to ready/expired work and revokes the old lease.

The admin service token manages configuration. Instance token hashes identify one project/actor and are rechecked before receipt replay. MCP obtains this identity through the [SDK's injected request context](https://py.sdk.modelcontextprotocol.io/handlers/context/), never a tool argument. Admin calls cannot impersonate registered actors. Credential-rotation receipts contain raw secrets and must be protected alongside claim receipts.

Selection acknowledgment is not evidence of the actual model. Completion may report a model; a mismatch against an explicit selection rejects publication. Missing evidence remains `unverified`; `worker_reported` is not independently attested. Capabilities and deliverables are declarative, not semantic verification.

## Events and growth

Except for ephemeral runner heartbeats, committed mutations append project-scoped metadata to a globally increasing SQLite event sequence. Gaps within one project are normal because other projects share the sequence. Poll `events_list(after=cursor)`, process the returned events, then save `next_cursor`. Delivery is at least once from the consumer's perspective; consumers should deduplicate by sequence.

Only successful mutations appear in this ledger; failed attempts are not a security audit stream. Events omit input values and lease/instance tokens. Claim/completion events include the execution snapshot, including role instructions and model selection; this can contain sensitive project information.

v0.3 retains revisions, receipts and events indefinitely. Deleting receipts would weaken retry guarantees, so there is intentionally no automatic retention job. Monitor disk use, keep bounded project scopes, and archive whole inactive ledgers. There is no published throughput or scale guarantee.

## Why these limits exist

The boundary keeps concurrency guarantees inspectable and adapters small. Instance identities reduce accidental cross-role access but do not create a hostile multi-tenant sandbox. Independent replicas need a shared transactional backend; external writes need an enforcement point. These require separate design work.


## Per-run capabilities

`mcp_catalogue.py` projects safe metadata from local Codex/OpenCode/ZCode/DSH configuration. Discovery never launches a configured executable or connects to an endpoint. Native definitions remain private; the ledger stores names only. Selected names are validated at save/dispatch and again at adapter execution so missing or unreadable configurations fail rather than silently changing the tool set. Plugin cache manifests are declared candidates only and cannot be selected as verified runtime connections.

`Selection` and `OfficeMember` accept optional `mcp_servers`; the default preserves runtime configuration. `OfficeDispatch.member_overrides` applies Skill/MCP overrides to a deep-copied roster before creating tasks, leaving saved team configuration unchanged. Request hashing distinguishes omitted MCP overrides from explicit null. Registry acknowledgement strips default nulls to preserve legacy clients; nondefault selections participate in the immutable claim snapshot.
