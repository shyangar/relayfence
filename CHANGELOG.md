# Changelog

## Unreleased

- Rebuild meeting-stage progress as a full-width agenda/detail workspace with a numbered timeline, concise next-step states, expandable blockers and stable selection during refresh. Keep terminal meetings free of inactive pause controls.

- Fix task-room column allocation and long meeting descriptions; replace skill cards with a searchable Skill/MCP table. Discover local MCP declarations without exposing connection secrets, support member defaults and per-meeting capability overrides, and apply verified Codex/OpenCode configuration switches at execution.

- Apply the approved white/blue project workbench: one labelled sidebar and project switcher, ledger-backed task/decision/team cards, retained office view, focused task timeline, and a nonmodal member drawer with provider/model context and separate conversation/activity tabs. Mobile navigation and project switching remain available.

- Open the local office URL directly, including after service restart or expired legacy cookies; require explicit same-origin Fetch Metadata, keep API/MCP authentication, and avoid mistaking an authorization failure for an outdated server.

- Bound automatically connected project execution with a shared service capacity (`serve --max-parallel`, default 2), exclusive directory admission and fair waiting turns. Release permits after teardown and on failed/cancelled submission; retain filesystem locking and task fencing.
- Poll duplicate provider slots once per round, scope candidate deduplication by project, and replace the manager's monkey-patched tick with explicit scheduler hooks so network polling does not hold the cross-project admission lock.
- Add a transactional `project_overview` read model and live `/local/runtime` status. The office shows queue reasons, provider connection errors, recovery attention and reconnect actions; cancelled, expired, unreviewed and stale work no longer inflate completion/running indicators.
- Coalesce overlapping browser refreshes with a trailing update and mark interrupted synchronization as stale.

- Add OpenCode as a first-class agent software: installed-CLI discovery via `opencode models`, exact provider/model pass-through, `plan`/`build` permission mapping, private `--standalone` runs, and structured-result validation.
- Register OpenCode office groups and skills (`~/.config/opencode/skills`, `<workspace>/.opencode/skills`) and print an OpenCode V2 `mcp.servers` connector snippet with `relayfence config opencode`.
- Add the **任务指挥台** shortcut to the office side rail so the task view is reachable without using the top tabs.
- Rebuild **任务指挥台** as a per-group chat console: real guidance, lifecycle events and published deliverables stream into the conversation every two seconds, with older-history paging, an inline guidance composer, group channels and a separate task-control column.
- Deep-review the **任务指挥台** projection: each delivery is pinned to the revisions recorded by that completion (with the event-ledger fallback), deleted tasks keep their title and full retained guidance, independent office tasks mirror lifecycle events into their group, long member ids expose the exact role mapping used by the backend, and an unresolved guidance send requires confirmation before leaving the group. Startup recovery records a visible `chat.failed` entry, and task deletion writes its audit event before removing the assignment so it lands in the owning group.
- Detect OpenCode Desktop's bundled headless CLI, and show OpenCode models marked **免费** when the local model API reports zero input/output/cache prices; paid models show their base per-million-token price.
- Move the local project picker into SQLite (`Schema 7`) and add a `group_logs` table with cursor pagination, group/level/text filters and bounded analysis. Legacy `projects.json` is imported and removed.
- Run office member tasks in parallel: direct briefings no longer create a dependency chain, meeting member tasks depend only on the shared plan, and the local dispatcher creates bounded slots per provider.
- Add a **日志分析** office view with level/kind/member/group summaries, task outcomes, recent alerts and older-log paging.
- Improve member conversations with incremental rendering, date separators, status pills, safe code blocks, collapsible long results and older-record pagination that preserves scroll position.
- Add a **团队管理** office view that aggregates groups saved by every local project, supports create/rename/delete, and imports existing groups from other projects into the current office. Office group headers now include delete; dispatched tasks are not cancelled.
- Add the first-position **智能体管理** office view with startup discovery of installed agents, provider/model and login/key status, and encrypted provider-key storage (`Schema 8`). Keys are encrypted with a private local key file, only masked hints are returned to the browser, and plaintext is injected into the matching worker environment at execution time.
- Add office configuration snapshots (`Schema 9`): save the current groups/members setup under a label, restore it over the live configuration, and delete individual snapshots from the archive dialog.
- Keep vendor-authored run summaries out of shared failure records: agent-reported blocked/failed outcomes publish only fixed sentences, while the original text stays in the private run directory. Discovered provider labels are sanitized before entering the ledger, and saved keys are restricted to the env names the current catalogue advertises.
- Harden local isolation and durability: state now defaults to platform application data and a connected project may not contain it; the office uses one-time pairing instead of a public marker; skill staging is path-safe, serialized per workspace and removed after the run; task deletion checks claim snapshots and meeting waits; reserved internal keys, duplicate revisions and version downgrades are rejected; request/list sizes and meeting/snapshot pagination are bounded (`Schema 10` indexes). Also fixes handoff with skills, partial legacy imports, unknown diagnosis echoing, vendor stream teardown, transient dispatcher backoff, member-chat cursor collisions, signal/discovery teardown, directory-picker races and credential-key recovery.
- Local HTTP hardening: mid-body client disconnects no longer flood uvicorn with tracebacks (`/local` and `/v1`), a busy ledger answers `/local` with `HTTP 503` instead of an unhandled 500, and `/local/status` no longer runs inline on the event loop. Worker-side fixes: `RELAYFENCE_*` environment passthrough is a strict allowlist, transient skill-lock contention releases the task lease for retry, and lease loss detected after a clean adapter exit is no longer overridden. The ZCode protocol queue is unbounded (its total volume is already capped by the event log), so a chatty vendor session between polls no longer kills the run.

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
