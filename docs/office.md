# AI office (development preview)

RelayFence's `/` opens a game-style office. The existing administrative console remains at `/console`. Both use the same authenticated HTTP service and SQLite ledger; the scene does not run a second scheduler.

## Team and meeting flow

The project manager lists saved local directories with group and task counts. Adding a directory does not start a model or switch the active project. Entering a project connects its runner and opens its office; the office, meeting and quota views then use that project's ledger namespace. The list is stored in the private service data directory and survives restarts. Previously connected directories are recovered from existing private worker configurations when no list has been saved yet. A missing directory is shown rather than silently removed. Connected projects retain independent background dispatchers; switching the selected page does not stop another project. Tasks sharing a checkout execute under an exclusive process lock.

Run `relayfence serve` and open the local URL it prints, then select an existing project directory. The server discovers installed software and configures scoped local runners automatically. No manual admin token or executor selection is needed in the office. Create a group for a detected software, then click a desk to configure its person:

- Name, one of 24 appearances, gender expression and position.
- Group leader, independent cooperating session, or a planned native subagent.
- Detected provider, exact model ID and supported reasoning setting; the runner is bound automatically.
- Up to four ordered fallback models for that same person; these do not create extra desks.

The **团队管理** view aggregates the groups saved by every local project. It can rename or delete a group and create a new group in the current project. **选择已有小组** in the office lists groups from other projects; importing one creates a fresh copy in the current office, reconnects members against the current project's discovered models where possible, and leaves the source project unchanged. Deleting a group removes its office configuration but does not cancel tasks that were already dispatched. Saving a desk does not start a model call; dispatching a task does. The server validates software, model and parameter bindings. Missing software or failed discovery is shown explicitly; discovery does not prove paid balance or real-time model availability. A stable local project ID is derived from the selected directory, so reconnecting that directory preserves its tasks and team. Previously connected projects keep their background dispatchers when the selected project changes. Tasks in different workspaces can run concurrently. Tasks sharing a checkout hold an exclusive worker lock for their entire process lifetime, including cleanup.

The local project picker, office configuration, tasks, events, member guidance and per-group logs are stored in the project SQLite ledger. The log view reads durable entries newest-first with cursor pagination, filters by group/level/text, and shows counts, task outcomes, recent alerts and average completed-task time. Private worker configuration remains a separate, permission-restricted dispatcher input. Provider keys configured in **智能体管理** are encrypted before they are written to SQLite; plaintext is decrypted only in the local worker process at execution time.

The local office opens directly without a token or expiring link. Local UI mode trusts the local operating-system account; a loopback peer, the UI request marker and an explicit same-origin Fetch Metadata header are required. Requests without browser metadata still need API credentials. The loopback peer, Host, Origin, JSON content-type and browser fetch-site checks still prevent remote or cross-site writes, and CORS is not enabled. The in-page directory browser lists folders without native windows. Advanced API credentials remain private server-side. The server never accepts executable commands or credential values from this form. Advanced bearer-token clients and `/console` remain available.

The meeting room creates actual ledger tasks from the validated group plan. Group leaders return task IDs and explicit `depends_on` arrays. Dependencies may refer to a task in the same group or `group-id/task-id` in another group. The coordinator validates the entire graph (including unknown IDs and cycles) before creating any member tasks. Claims wait for prerequisites and include their published, versioned results. Older ordered plans without dependency fields retain their sequential order. Each task carries the selected position's instructions and deliverables. A running local dispatcher claims eligible work; an offline runner remains visibly offline. Planning and revision stages still follow their staged order. Use separate workspaces or worktrees for actual parallel file editing.

The wall screen, bubbles and task board reflect ledger states. They do not invent model dialogue or movement. All provider names, task descriptions and results are rendered with text nodes. A lost mutation response leaves a replay button that resends the original request ID rather than making a second task.

## Positions

| Position | Responsibility | Typical deliverable |
| --- | --- | --- |
| Project lead | Split goals, assign owners and coordinate handoffs | Task plan and acceptance checklist |
| Product manager | Define observable behavior and scope | Requirements and acceptance criteria |
| Architect | Specify module boundaries, interfaces and failure behavior | Design and interface contracts |
| Frontend engineer | Implement interfaces, interactions and accessibility | UI code and reproducible checks |
| Backend engineer | Implement state, APIs and error handling | Service code and tests |
| QA engineer | Test normal, failure and boundary behavior | Test results and reproducible defects |
| Security engineer | Review trust boundaries and sensitive data handling | Findings with severity and remediation |
| Reviewer | Check changes against requirements and evidence | Review conclusions and unresolved issues |

The complete Chinese instructions and deliverables live in `src/relayfence/office_roles.py`.

## Quota observations and handoff policy

`quota_observe` stores an observation with a route, source, timestamp and expiry. It accepts tokens, requests, credits or percent, without comparing values across different units. `quota_list` exposes stale observations as stale; no balance is calculated from token consumption. Changing a route's provider, model, parameters or runner invalidates its earlier quota observations.

Sources are `provider_api`, `runtime_error` and `operator`. Only a fresh provider observation can establish availability by default. A manual observation remains visibly manual. A reset timestamp means **recheck**, not automatically replenished balance. A rate limit is distinct from exhausted credits. Observations expire in at most 24 hours.

`office_route_preview` prefers models inside the current software, in configured order, before other software. It never invokes a model. Unknown routes require an explicitly requested probe; the normal handoff does not select them.

`office_handoff` is an **explicit, transactional handoff**. It requires all of the following:

1. The task failed specifically with `quota_exhausted`.
2. The operator confirms the old executor has stopped.
3. A durable, fresh checkpoint exists in the ledger.
4. The task revision and failed execution's model still match.
5. An alternate route has fresh availability evidence.

The operation retains the original role instructions, deliverables and capability requirements, adds the checkpoint to task inputs, and records the route change. It refuses running tasks, ambiguous failures and stale checkpoints. A maximum of three handoffs prevents loops. Completion with an old lease remains rejected.

**Current boundary:** this preview provides quota storage, provenance, routing decisions and safe explicit handoff. It does not yet install continuous vendor quota probes or an unattended cross-vendor retry supervisor. Vendor account quota APIs differ; where an adapter cannot obtain authoritative balance data, the screen shows unknown. Do not describe this preview as automatic quota failover.

## Native subagents versus independent sessions

A native subagent belongs to a real parent session inside the selected software. A separate CLI process is an independent session even if it is drawn beside a leader. Native-child configurations may be drafted, but binding/dispatch is rejected with `native_binding_required` until a runtime adapter can verify and control that relationship. The current command adapters execute independent sessions. The UI does not pretend these are native children.

## Skills (per software)

The **技能中心** tab lists installed [Agent Skills](https://github.com/anthropics/skills) grouped by software, and the member dialog offers a multi-select picker restricted to that member's software. Discovery is read-only: it never edits `~/.codex/config.toml`, `~/.config/opencode`, `~/.zcode` or `~/.dsh`.

Scanned roots per runtime (`skill_list` returns each root with an `exists` flag):

| Software | User root | Project root |
| --- | --- | --- |
| Codex | `$CODEX_HOME/skills` (default `~/.codex/skills`) | `<workspace>/.codex/skills`, `<workspace>/.agents/skills` |
| OpenCode | `$XDG_CONFIG_HOME/opencode/skills` (default `~/.config/opencode/skills`) | `<workspace>/.opencode/skills`, `<workspace>/.agents/skills` |
| ZCode | `$ZCODE_HOME/skills`, `$ZCODE_HOME/plugins/skills` | `<workspace>/.agents/skills` |
| DeepSeek Harness | `$DSH_HOME/skills` | `<workspace>/.agents/skills` |

A skill is a directory whose `SKILL.md` starts with YAML frontmatter (`name`, `description`). Symlinks, nested container directories and manifests with a UTF-8 byte-order mark are **not** reported, because the Codex loader ignores them. Codex's discovery behaviour was verified against the installed binary with `codex debug prompt-input`; OpenCode's user/project roots match its V2 [skills documentation](https://opencode.ai/v2/docs/skills/). The ZCode and DSH roots follow the same convention but were not end-to-end verified with those vendors' binaries. The picker uses the frontmatter `name`, so keep it equal to the directory name: OpenCode derives the skill ID from that directory. The scanner mirrors Codex's layout (one directory level per root), so OpenCode's optional flat `skills/<name>.md` files and deeper nested `SKILL.md` sources are not listed; use `<root>/<skill>/SKILL.md` to make a skill selectable.

A member's selection is stored on the office member and projected into the role's `selection.skills`. `role_put`, `office_put` and `office_dispatch` resolve every name against the local installation and reject unknown names with `skill_unknown`, so a stale selection fails explicitly instead of silently shrinking. Up to 32 unique names are allowed.

At execution time the runner copies the selection into the runtime's own project root: `<workspace>/.codex/skills/relayfence-selected/<name>` for Codex, `<workspace>/.opencode/skills/relayfence-selected/<name>` for OpenCode, and the shared `<workspace>/.agents/skills/relayfence-selected/<name>` for ZCode and DSH. The injected folder uses the source directory name, never the untrusted frontmatter name. That directory is marker-owned: a previous run is replaced, and a foreign directory is never deleted. Because Codex discovers the workspace skill root automatically, the adapter no longer passes `--ignore-user-config` (which disables skill discovery entirely); it passes `-c 'skills.config=[{name="…",enabled=false},…]'` for every skill the member did **not** select. The value is a separate argv element built from validated names, never a shell string. OpenCode has no equivalent per-run switch that RelayFence can pass through the command adapter, so its injected selection is authoritative for which skills RelayFence delivers, but the member's other installed OpenCode skills stay visible to that runtime.

Skills that a runtime's own configuration disables are still listed, marked **本机配置已禁用**, so the screen does not promise something the vendor hides. Copying files is local work: it does not invoke a model and proves nothing about whether a model will follow a skill.

## Agent management and encrypted local keys

**智能体管理** is the first tab. The service scans the local machine for Codex, OpenCode, ZCode and DeepSeek Harness at startup and refreshes the inventory in the background. Each agent card shows whether the runtime is installed, which providers and models were discovered, and whether a usable account or key was found. Selecting a card lists every provider, its model IDs and supported parameters, plus a login/key prompt when it cannot be used yet.

Authentication is read from vendor-native locations without printing credential values: Codex's local login, OpenCode's `auth list`, ZCode's local account credentials, and DSH's launch environment or `.env` files. DSH headless runs intentionally disable the desktop account platform, so `deepseek-account` is shown as unsupported and requires an API-key provider instead. A DSH key must be visible to the headless process through its launch environment, `<invocation cwd>/.env`, `$DSH_HOME/.env`, or RelayFence's encrypted store.

When a provider needs a key, **配置密钥** writes the value through the local admin endpoint. RelayFence encrypts it with Fernet, stores only ciphertext and a short mask such as `••••••••3456`, and returns only that mask to the browser. The encryption key is a private file in the service's local data directory (`local/agent-credentials.key`); it is not stored in SQLite. Saving or deleting a key triggers a rescan so the provider becomes selectable only after authentication is actually present. Decrypted keys are injected into the worker environment for the matching runtime/provider and are never written into a task envelope, office configuration, log or published result. Deleting a key removes the stored ciphertext; it does not change the vendor's own login or environment variables.

## Model catalogue and prices

The software selector is enabled only for runtimes with at least one discovered model. On Windows, OpenCode discovery also checks the headless CLI bundled with OpenCode Desktop (`%LOCALAPPDATA%\Programs\@opencodedesktop\resources\opencode-cli.exe` and versioned copies under `%APPDATA%\ai.opencode.desktop\cli`). If OpenCode is installed but the selector stays disabled, click **重新检测** after installing or signing in, then reopen the project. RelayFence does not invent a fallback model list for an unavailable runtime.

For OpenCode, discovery keeps `opencode models` as the authoritative reference list and makes a best-effort, read-only call to `opencode api --standalone GET /api/model` for labels and prices. The member model selector marks a model **免费** only when every reported input, output and cache rate is zero. Other models show the base USD price per million tokens, with the note that the provider's bill is authoritative. If the local API is unavailable or omits a price, no free marker is added and the screen says the price is unknown rather than guessing. This is catalogue metadata, not remaining free-tier quota; quota observations remain a separate, explicit mechanism.

## Runtime permissions

The built-in Codex worker supports `read-only`, `workspace-write` and `danger-full-access`. Its default remains `read-only`; full access is an explicit local worker setting. The vendor adapter similarly accepts `--permission-mode`. Permissions are local runner configuration, never task-controlled shell arguments.

The OpenCode adapter maps `--permission-mode` onto OpenCode's own agents: `read-only` uses the `plan` agent, while `workspace-write` and `danger-full-access` use `build`. RelayFence does not pass a permission-bypass flag or edit OpenCode's provider/auth configuration; OpenCode's saved settings still apply. These are agent policies, not an OS sandbox: the `build` agent still follows OpenCode's permission rules, whose defaults allow external directories, so `workspace-write` alone does not confine shell or file writes to the workspace. Use a separate worktree or container when you need hard isolation. Each run uses a private `--standalone` server so killing the worker also ends the run instead of leaving a shared-service session behind.

The 2026-10-16 development run was explicitly authorized to use full access. Codex used `gpt-6.1-sol` with `medium`. ZCode's discovered Start Plan catalogue exposes `GLM-5.3` and `GLM-5.3-Flash` with `low`, `high`, `max`; after a runtime rejection of `medium`, the user selected `high`. DSH uses standard mode without a fabricated reasoning parameter. These are local verification facts, not universal account availability claims.

## Storage, packaging and checks

### Recorded vendor development verification (2026-10-16)

These records describe completed local runs, not a promise about current model capacity:

- Codex: `gpt-6.1-sol`, `medium`, full access. Created office regression tests and completed a subsequent verification task with a published result.
- ZCode: Start Plan `GLM-5.3-Flash`, `high`, `yolo`. Created `tests/test_office_zcode_review.py` and ran its three tests and Ruff. The first `GLM-5.3` authenticated request hit a concurrency limit, not exhausted daily quota. Flash completed. Its structured check records initially failed the string-only result schema; an operator reran validation and published the preserved result with an explicit recovery note. The adapter now accepts exact `{command, result}` string records without inferring their success.
- DeepSeek Harness: A configured provider produced the role catalogue; a separate configured provider subsequently reviewed it. The requested `deepseekv41-flash` ID was rejected by that runtime, so it must not be advertised as supported. Initial publication errors were separate from model execution.

ZCode's optional existing-login callback is restricted to the selected Start Plan account family and model. It does not create or refresh accounts. Its installed credential format is version-dependent; missing or incompatible credentials fail closed. Explicit provider/model/reasoning/permission acknowledgements are required before dispatch. Protocol lines, notification queues and event logs are bounded. Credentials are never part of a task envelope or a published result.

Schema migrations add office configuration, quota observations, local projects, group logs, encrypted agent credentials and office configuration snapshots (schema 9), plus supporting event, meeting-assignment and member-message indexes (schema 10). They run under the existing write lock and preserve task fences and idempotency receipts. Back up a live ledger before downgrading: older versions do not support the current schema.

Saving or deleting a provider key triggers a rescan so the provider becomes selectable only once authentication is confirmed. While a project is connected, the refreshed catalogue is staged until running tasks finish; the agents tab applies it immediately, but an open "new group" or member dialog may need one further refresh to list a newly saved key.

No frontend build service or third-party asset CDN is required. `office.html`, `office.css` and `office.js` are packaged with the Python distribution. The browser test `scripts/check_office.cjs` takes `RELAYFENCE_PLAYWRIGHT` (module path) and `RELAYFENCE_BROWSER` (browser executable). It starts an isolated mock service to verify direct local access, directory selection, discovered model choices, automatic binding, existing-group import, the cross-project team library, group deletion, the per-group chat feed and long-member role mapping without real model requests. Optional `RELAYFENCE_SCREENSHOT_DIR` saves a desktop screenshot. Mock tests do not imply model execution; live discovery and connection are validated separately.

## Member configuration and task guidance

Members support custom position names and editable role prompts. Saved prompts apply to subsequent dispatches. Native children are saved as unbound drafts until a verified native session adapter is available.

Click a member with a task to open the right-side conversation panel. It shows persisted guidance, lifecycle events and published result summaries, rather than a vendor-native streaming transcript. Running-task guidance pauses and fences the task, interrupts its local process, waits for cleanup and local-lock release, then resumes with the instruction. File edits are retained without rollback. Ready tasks receive guidance before dispatch; paused tasks stay paused. Failed cleanup leaves the task paused with a visible error. Guidance text is also stored in the durable group log, so deleting the task does not erase the retained conversation; after a restart, unfinished guidance is marked failed and the group feed records the reason.

Schema migration 5 persists messages and idempotency keys. After restart, unfinished guidance is marked failed and tasks remain paused for review. Schema 7 persists the local project index and group logs; schema 8 adds encrypted agent credentials; schema 9 adds office configuration snapshots; schema 10 adds supporting indexes. Older releases cannot open the current schema; back up before upgrading.

## Office configuration snapshots

The studio floor workspace provides directory buttons for saving the current office configuration (groups, members, models and position prompts) under a label, opening the archive, and restoring it. Snapshots are stored per project in the ledger with a creation time and group/member counts. Restoring a snapshot replaces the live groups and members, records a normal configuration edit, and only affects future dispatches; running tasks continue with their claimed selection. Snapshots can be deleted individually, and the currently saved snapshots are shown in the archive dialog.

## Task command center and hierarchical meetings

The task command center is a per-group chat console. Each group channel shows real guidance, task lifecycle events and published deliverables as a conversation; it polls the durable ledger every two seconds, pages older history without rebuilding the scroll position, and keeps the guidance composer attached to a selected member task. The **会议阶段** and **任务与操作** columns expose the same briefing controls, dependencies and command buttons without crowding the conversation. The feed is a projection of committed ledger rows, not a simulated model transcript. A completed delivery resolves only the revision references recorded by that completion (falling back to the matching event), so reopening or revising a task cannot rewrite earlier chat. Deleted tasks retain the title from the deletion event and the full guidance stored in the durable group log; independent office tasks also mirror lifecycle events into their group. Long member ids use the server-provided role mapping instead of duplicating the digest in the browser. An unresolved guidance send keeps its retry state and asks for confirmation before leaving the group, and startup recovery writes a visible failure event with its reason. Progress is completed steps / generated steps, not an elapsed-time estimate. Generated step counts can increase during planning. Whole-briefing pause also blocks new stage generation and claims; whole-briefing resume preserves independently paused or subsequently changed tasks. Individual pause/cancel revoke the lease and signal the local process; resume and handoff wait for teardown. Handoff preserves original role requirements, prerequisites and written files, records operator notes, and leaves the receiving task paused until explicitly resumed.

Select a meeting leader and participants. Each participating group's leader joins automatically. The leader produces a structured group assignment from the goal and the roster snapshot. Discussion mode asks group leaders to assess feasibility and report objections; the leader revises the plan with these results, for at most three rounds. Agreement waits for the operator to start execution. Execution mode goes directly from planning to group leaders' member-level assignments. Member tasks can run concurrently inside the shared project checkout. Planning stages are instructed to analyze only, while retaining the operator's configured runtime permissions.

Only validated, published structured results create downstream tasks. Unknown groups/members, malformed results and stale dependencies block progression with an explanation; the command center can regenerate invalid stage outputs. This is an orchestration protocol over actual model calls, not a simulated conversation. Vendor model output quality and account availability still determine whether a real meeting completes.

Switching projects changes the selected view and retains previously connected project dispatchers. Different projects can continue independently; closing the server stops all owned dispatchers. After a server restart, enter a project to reconnect its runner. Configuration and meeting state remain in the ledger; unfinished work is not marked complete by restart.

Schema 7 adds the SQLite local project index and per-group logs. Existing task events with a member assignment are projected into the log table once during upgrade. Schema 8 adds encrypted agent credentials; schema 9 adds office configuration snapshots; schema 10 adds supporting indexes. Schema 6 added durable meetings and member assignment records. Back up before upgrading; older releases cannot open the current schema.

## Objective iterations and office live wall

Group headers provide Rename and Delete. Rename preserves the group ID, members, models and existing task assignments. Delete removes the group configuration and does not cancel tasks already dispatched; historical meeting roster snapshots keep the name recorded at dispatch.

A completed meeting can start a new modification iteration of the same objective. The command center groups these by root objective and filters by iteration. Each new iteration retains the original goal, accumulated modification requests and bounded published result references from the prior iteration, and plans against the current configuration of the same members. Existing meetings are treated as iteration 1 without a database migration. Only the latest completed iteration may be extended, preventing accidental parallel branches. Running work continues to use member guidance. Removed roster members must be restored before reusing that roster.

The office live wall polls real member task status, recent persisted user guidance and published summaries. It excludes cancelled tasks and offers direct access to member history/guidance. The member conversation panel keeps date separators, incremental status pills, safe fenced-code blocks, collapsible long results and older-record pagination; it preserves the reader's scroll position unless they were already at the bottom. It does not display private chain of thought or fabricate vendor-native streaming chat. Planning/worker stages remain actual tasks with durable output records.

## Task configuration and authenticated activity

Office dispatch creates a distinct role per task, fixing the selected prompt, model, reasoning and skills independently of later meetings. Generic registry roles remain editable; deliberate task transfer creates its own receiving role.

Project skill roots are registered by ledger project ID, so a background meeting always validates skills against its own workspace. Switching the selected page does not replace another project's roots.

Worker activity uses its Agent credential and includes the current fence and lease token. The ledger validates ownership and lease freshness, derives the member from task assignment, and rejects stale or forged reports. Failed report batches retain their exact idempotency key and payload until acknowledged. Member history reads task state and guidance in one database snapshot.

## Human checkpoints and live workspace

Schema 11 stores durable human decisions. A worker can return `question` instead of outputs or failure; RelayFence fences and pauses its task and meeting. The task command center shows the pending question, context and choices. A reply is stored as a task input before work resumes; the local control endpoint waits for the previous process to exit. Declining cancels the associated work. Revision checks reject stale replies and pending decisions survive restarts.

Discussion meetings stop after group assessments. Users can save the discussion without development, request a new planning round, or explicitly start execution. Question and answer records appear in the conversation history.

The office places the team floor beside a bounded live activity rail, with project metrics and a direct link to pending decisions. Worker event files are read incrementally, including partial lines and long streams, with a final bounded flush before publishing completion. Agent and parent-session identifiers are shown only when emitted by the runtime. Configuring a native child member does not establish a native session; unsupported sessions remain visibly unconnected. Live activity is a projection of reported events, not an assertion that every vendor desktop message is available.

## Delivery acceptance and execution history

New office meetings default to **成员交付需人工验收**. You can disable it before dispatch. Planning and discussion are unchanged; execution steps pause their delivery progression at `awaiting_review`. Open a step in the delivery board, read its exact published output and approve or reject it. Approval unlocks eligible dependencies. Rejection records feedback and leaves work paused until explicit resume. Previous projects and API callers keep the old opt-in behavior.

The task command center includes five delivery lanes and an execution archive. Each claim is a separate attempt identified by fence; retries and transfers do not create a new business objective. The archive retains model selection, completion/failure/interruption, takeover, review evidence and handoffs, with older-attempt pagination. A task can have many attempts and several iteration siblings under its original objective.

The team library now shows member workload from each project's real ledger. Manual transfers publish a structured handoff input, preserving prior output references, known failure, recipient and the user's next-step note. The receiving claim reads that input. See [design references and boundaries](open-source-patterns.md).

Schema 12 adds delivery policies/reviews and a task-history index. Back up the ledger before upgrading; older releases cannot open the upgraded database.

## Project-owned native conversations

Automatically generated local workers now bind **one project + one runtime** to one persistent native conversation. Provider slots, groups and members using the same software share that identity; distinct project directories have distinct bindings. First actual execution creates the conversation, then later tasks, retries, guidance restarts and modification rounds resume its exact ID. There is no `--last` heuristic. Older manual worker configurations without `session_file` keep their previous independent execution behavior.

Bindings are stored privately beside each project's local worker configuration, under `local/<project>/sessions/<runtime>.json`. They record the project/workspace identity, native ID, last task, last model and completed-call count. Native history stays in the corresponding software's own storage. Atomic binding writes capture the ID from startup events before a model turn finishes; a failed turn therefore does not silently discard the conversation. If creation was interrupted before an ID arrived, the next task stops for inspection rather than creating another conversation automatically. Missing native history is treated as a resume failure, not a new session.

- Codex: persistent `exec`, then `exec resume <saved-id>`, with the requested model, reasoning, output contract and permission settings supplied for each task. See [OpenAI's non-interactive resume documentation](https://learn.chatgpt.com/docs/non-interactive-mode).
- ZCode: `session/create` once; later protocol processes use `session/resume`, update model/reasoning/mode and verify the acknowledged configuration before sending the task.
- DSH: headless JSON events supply the created ID; later calls pass `--session-id`. The current provider/model selection is still supplied on each call.
- OpenCode: capture its emitted `sessionID`; subsequent calls use `--session`.

The existing workspace process lock serializes calls, so two tasks cannot send overlapping turns into the project's native conversation. Different projects remain independent. A new prompt identifies the current task and role; previous conversation is context, not authorization to repeat previous work. Model context-window limits and native compaction still apply; a stable ID does not guarantee verbatim recall of unlimited history.

The office shows each configured runtime's binding, native ID, last-call state and completed-call count. **重建绑定** explicitly archives the old binding, keeps the vendor's old conversation, and creates a new binding on the next execution. It checks the displayed ID/update version and refuses while the workspace is in use. An invalid binding file requires local inspection; it is not silently overwritten.

This controls actual runtime conversations. It does not automatically launch or focus each vendor's desktop window, import a pre-existing arbitrary desktop chat, or claim that a headless conversation is visible as an open desktop tab. Desktop visibility and direct native navigation require their own verified integrations. Restart `serve` and reconnect existing projects to generate the new session-enabled worker configurations.


## Project workbench design (2026-10-30)

The default office entry is now a project workbench with one labelled sidebar and a project switcher. It shows actual ledger progress, the current meeting/iteration, pending decisions, recent events and team cards. Team cards open member conversations; **Manage members** opens the retained office scene. The **Team view / Office** switch changes presentation without changing the team configuration or task state.

**View task flow** opens an iteration-specific dependency list with member conversation, execution history, pause/resume and transfer controls. Root/iteration filters update the focused flow. Completed discussion steps can reach 100% while the meeting still waits for an execution decision; the project-wide percentage is a separate aggregate of all eligible steps.

The member drawer shows the configured role, provider and model. **Conversation** shows guidance and published messages; **Execution records** shows captured activity and system events. Both use the existing paginated history. Finished tasks with no draft or pending send hide the inactive composer so history has more space. The drawer is nonmodal; desktop content makes room for it, while mobile uses a full-width panel. Sending guidance keeps the existing stop-and-resume behavior; the page does not imply unsupported live injection.

White surfaces, blue emphasis, subtle borders and semantic status colours are shared across views. Mobile navigation scrolls horizontally and keeps project switching available. Focus indicators, keyboard record tabs and reduced-motion preferences are supported.


## Skills and MCP capability centre

The capability centre uses a filterable table (name, type, runtime, origin, state and expandable details). Long skill descriptions and paths no longer stretch runtime cards. Folded YAML descriptions are parsed as text. MCP inventory reads bounded local configuration and plugin manifests without starting servers, authenticating or returning commands, environment values, URLs or headers. A plugin cache declaration does not establish that its plugin is enabled. Configured does not mean connected.

Member configuration has independent Skill and MCP selections. MCP defaults to the runtime's configuration. Codex and OpenCode may explicitly select configured MCP names; an empty list disables the detected configuration entries. This is not a block on plugin-supplied, remote-organisation or managed tools. ZCode and DSH currently expose discovered declarations but retain runtime-managed MCP loading; explicit per-run MCP selection is rejected rather than silently ignored.

The meeting form has **本次任务的 Skill / MCP**. Select **读取并配置能力**, then enable **本次单独配置** for a participant (including automatically added leaders). The override is frozen into the meeting/role/claim snapshots and does not overwrite the team. Omitting an override uses the member defaults. Within an override, omitted `mcp_servers` retains the member choice, `null` inherits the runtime configuration and `[]` disables detected configured entries. Skill `[]` requests no injected skills; globally installed skills remain runtime-controlled outside Codex.

The adapters use Codex CLI configuration overrides and OpenCode's inline configuration. OpenCode V1 uses `mcp.<name>.enabled`; V2 uses `mcp.servers.<name>.disabled`. V2 overrides preserve the full private server definition because server objects are replaced. Selection changes apply on the next execution, not by mutating a running process. No new database migration is needed.

Configuration references: [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp), [OpenCode V2 MCP](https://dev.opencode.ai/v2/docs/mcp-servers/), [OpenCode configuration precedence](https://dev.opencode.ai/docs/config/).
