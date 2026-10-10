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
2. Look up the receipt under `(project, actor, request_id)`.
3. If it exists, compare the canonical operation/payload digest. Return the original success if identical; otherwise reject.
4. Check current versions, task status, lease and declared dependencies.
5. Write new immutable revisions and advance heads/task status.
6. Write a compact event and a success receipt.
7. Commit together. Any exception rolls back the transaction.

The receipt includes normalized defaults, so omitting a default and explicitly providing its value have the same identity. JSON object order is ignored; array order is significant. Only successful operations consume an idempotency key. Rejected operations write no ledger event or receipt.

State updates are compare-and-swap operations. A missing key has expected version `0`; each successful update creates the next positive version, even if the value happens to be unchanged. A key's state/artifact kind cannot change.

SQLite runs in WAL mode with foreign keys, a 10-second busy timeout and `synchronous=FULL` on each connection. Durability still depends on the local filesystem and storage honoring SQLite's synchronization requests. Read operations use one transaction, so a response is assembled from one database snapshot.

## Ownership

Each task has one status: `ready`, `running`, or `done`. A running task with an expired deadline can be claimed again. Expiration is evaluated on access; it does not require a sweeper or mutate task status by itself.

~~~mermaid
stateDiagram-v2
  [*] --> ready: create
  ready --> running: claim / fence + 1
  running --> running: renew live lease
  running --> running: expired takeover / fence + 1
  running --> ready: release live lease
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

Artifact values are JSON metadata, text, or external references. RelayFence does not fetch URLs, resolve Git SHAs, execute commands, evaluate model output, infer dependencies, or store binary files.

## Events and growth

Committed mutations append project-scoped metadata to a globally increasing SQLite event sequence. Gaps within one project are normal because other projects share the sequence. Poll `events_list(after=cursor)`, process the returned events, then save `next_cursor`. Delivery is at least once from the consumer's perspective; consumers should deduplicate by sequence.

Only successful mutations appear in this ledger; failed attempts are not a security audit stream. Events omit input values and lease tokens, but subject/actor names and dependency keys may themselves be sensitive.

v0.1 retains revisions, receipts and events indefinitely. Deleting receipts would weaken retry guarantees, so there is intentionally no automatic retention job. Monitor disk use, keep bounded project scopes, and archive whole inactive ledgers. There is no published throughput or scale guarantee.

## Why these limits exist

The initial boundary makes the concurrency guarantees inspectable and keeps client adapters small. Untrusted tenants need authenticated identities and project authorization. Independent service replicas need a shared transactional backend and carefully designed fencing. External writes need an enforcement point that checks ownership. These are separate design changes, not flags that make a local ledger distributed.
