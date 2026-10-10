# Changelog

## 0.2.0 — Unreleased

- Role → agent instance → model/parameter selection in the console, HTTP and MCP, with instructions and deliverables.
- Instance model catalogues/capabilities, project-scoped credentials, actor checks and admin-only configuration changes.
- Atomic execution snapshots and revision-checked reassignment of ready/expired work.
- Separate requested and worker-reported models, with no silent mismatch/fallback on completion.
- Explicit local command worker with model delivery, lease renewal, timeouts and recovery records.
- Transactional schema 1 → 2 migration retaining fences and old receipt hashes.
- No-key HTTP/subprocess example and identity/assignment/migration/adapter tests.

Limits: manually configured model catalogues; external MCP sessions set models in their own client. No bundled vendor launcher, automatic dispatcher, model discovery or provider-attested model verification.

## 0.1.0 — 2026-10-10

First public alpha of an independent coordination service for agents across tools.

- Versioned shared state with compare-and-swap and immutable revision history.
- Atomic task claims, expiring leases, renewal, release, fencing and explicit reopening.
- Input snapshots, transactional multi-artifact publication and transitive freshness checks.
- Durable idempotency receipts, cursor-based events and SQLite online backups.
- Authenticated loopback HTTP/MCP, a shared-service stdio bridge and Python client.
- Read-only coordination console, English/Chinese quickstarts and client configuration generator.
- No-key real-protocol demo covering races, stale input, retries, takeover and process restart.
- Tests for independent process races, loss before commit and loss of a response after commit.

Known limits: one trusted service on local storage, shared service-wide token, no automatic agent launcher, no external side-effect fencing, no history/receipt garbage collection, and no claimed live Codex/ZCode/DSH model-session certification.
