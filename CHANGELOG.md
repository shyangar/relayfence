# Changelog

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
