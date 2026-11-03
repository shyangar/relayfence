# Release verification

Initial verification date: 2026-10-10. This file records actual checks and their scope, not a reliability SLA.

## macOS fast-exit cleanup regression (2026-11-03)

- Main commit 9fd35f1 passed Ubuntu, Windows and wheel checks but exposed an intermittent macOS `EPERM` while cleaning up a fast-exiting adapter. That cleanup error masked the expected output-size rejection. The earlier PR run did not trigger this timing window.
- Group cleanup now uses a bounded macOS retry, polling/reaping on every attempt and continuing to signal descendants after leader exit. Deterministic cases cover a leader still reported alive, a group disappearing, a surviving group becoming signalable, persistent permission failures and unchanged Linux error handling. The real subprocess output-limit tests run five times per output type on every platform to exercise the fast-exit path repeatedly. No output limit, permission check or coverage threshold is relaxed.

## Cross-platform CI correction (2026-11-02)

- Commit 270a71e passed all functional tests on Ubuntu (730 passed, 5 platform skips), but failed the 90% coverage gate because Windows-only modules were counted as unexecuted there. The Windows matrix job and Linux wheel smoke test succeeded. macOS additionally exposed a test assertion that confused its temporary path's `private` component with the file-content marker.
- Directory-listing regression cases now exercise both ordinary and `private` parent directories and check a unique synthetic content marker and the excluded filename. The authenticated directory-only response must still match exactly.
- Each matrix job still runs the complete applicable test suite and publishes uniquely named, relative-path coverage data. A dependent job requires reports from all four OS/Python combinations, combines them and enforces the unchanged 90% threshold. A failed platform test prevents this job from succeeding; no platform-specific source files are omitted from coverage.

## Publication checks (2026-11-02)

- Release scope is the indexed source, tests, documentation and reviewed fixture images. Runtime ledgers, logs, local credentials, browser captures, temporary test directories and unrelated scratch projects are excluded. Developer-specific project names were removed from documentation; networking fixtures use reserved example addresses.
- The indexed files were checked for user-home paths, personal email addresses, private network addresses, credential patterns and exact matches to local credential values. A separate detect-secrets scan reported seven candidates, manually confirmed to be six synthetic security-test fixtures and one source checksum. Retained screenshots contain only demo/test content. Public repository links, generic configuration paths and loopback/documentation addresses are intentional examples.
- Full Python regression: **733 passed, 2 platform skips**, combined line/branch coverage **90.18%**. Ruff lint and all 100 Python files' formatting checks passed. These checks do not substitute for a third-party security audit.

## Meeting-stage workspace verification (2026-10-31)

- Task controls now show the latest two tasks by creation time (descending), with a More/Collapse history button and remaining count. The isolated browser regression verifies ordering independently of update time, expansion surviving refresh, collapsing and hiding the button for at most two tasks. Live port 7435 verification showed 2 of 14 tasks initially, all 14 after expansion, and returned to the collapsed view. Project/root/iteration changes reset the view; task state and controls are unchanged.
- Replaced the narrow stack of nested meeting cards with a full-width workspace: compact meeting selection, one detail panel, a numbered stage timeline, a next-step summary and grouped actions. Goals and failure details expand on demand. Finished/cancelled meetings do not expose pause/resume controls; cancelled stages are not labelled as future work.
- The complete isolated office browser regression passed, including new checks for switching between iteration records, retaining the selected record and an expanded goal during refresh, completed-stage indicators, and retaining the rule that only the latest completed iteration offers a new revision. Existing pause/resume, decisions, member guidance, capability selection and project/mobile checks also passed. Both JavaScript syntax checks passed.
- Live verification at port 7435 used existing connected-project records. The waiting-for-confirmation and blocked meetings displayed their respective summaries. The 390px viewport had no document horizontal overflow (document width 375px); its meeting selection stacks above the detail panel. The temporary viewport override was reset afterwards. No model task or execution decision was submitted, and no backend restart or schema change was necessary.

## Task layout and Skill/MCP capability verification (2026-10-30)

- Windows / CPython 3.12: **733 passed, 2 skipped**, combined line/branch coverage **90.20%**. Skips cover POSIX-only service-manager SIGTERM and unavailable Windows symlink privileges. This run used a writable checkout-local TEMP/TMP/SQLITE_TMPDIR for SQLite temporary journals; no global environment settings or production storage pragmas were changed.
- The isolated browser regression passed capability table filters/search, member MCP defaults, per-meeting overrides, task flow, member guidance, controls, project management, model/effort forms, runtime recovery and mobile document-width checks. Ruff source/tests/scripts lint and formatting checks and both JavaScript syntax checks passed.
- New backend tests cover redacted discovery, bounded JSON/TOML/YAML parsing, plugin declarations, unsupported and unknown selections, Codex argument delivery, OpenCode V1/V2 environment construction, immutable claim selections and meeting overrides that leave team defaults intact. Receipt tests distinguish an omitted override from an explicit null inheritance override.
- The production 7435 service was restarted after backing up its ledger and verifying that no tasks were ready or running. The existing connected-project project was reopened. Live browser inspection confirmed the two-column meeting/chat layout, collapsed goals, the capability table (2 Skills and 10 MCP configuration/plugin declarations), and the meeting's temporary capability controls. Live authenticated skill_list and mcp_list both returned capabilities_version 1. No new task was dispatched and the pending execution decision was left untouched. This supersedes the older restart limitation recorded below.
- Source and wheel builds passed. Archive inspection confirms the MCP module, office assets and JSON5/PyYAML dependency metadata, with private state excluded. The source archive now uses an explicit include list so unrelated local scratch directories and pytest temporary ledgers are not bundled.
- Codex/OpenCode support explicit selection of detected configuration entries; ZCode/DSH inherit their own runtime MCP settings. Plugin manifests are declarations, not proof of installation, enablement or connection, and cannot be selected as verified configured servers. This pass did not invoke supplier models or test MCP server authentication/connectivity. Existing runtime discovery failures remain separately visible; this verification does not certify those runtimes as available.

## Workbench UI verification (2026-10-30)

- The approved workbench and task/member-drawer concepts were implemented in the existing HTML/CSS/JavaScript surfaces. No database migration or model execution was required.
- **52 targeted Python cases passed** (office operations, local sessions and capabilities). JavaScript syntax checks and the isolated Edge browser regression passed, including the single labelled navigation, real-data workbench projection, team/office toggle, focused task flow, member drawer/model context, record tabs, guidance, task controls, decisions/review, project management and 390px layouts without document overflow.
- The real service at port 7435 displayed the existing connected-project project's 70% aggregate, three groups and one pending decision. Opening the task flow and member history used existing records and did not submit new instructions or resolve the pending decision.
- Ruff checks for RelayFence src/tests/scripts passed. Unrelated local scratch files were excluded from the release. The rebuilt wheel was checked for the new HTML, JavaScript and CSS markers.
- Browser regression uses fixtures; real-page verification is read-only. No supplier model call was made for this UI change.

## Product reliability verification (2026-10-28)

- Windows / CPython 3.12.13: **723 passed, 1 skipped**, combined line/branch coverage **90.11%**. The skip is the POSIX service-manager SIGTERM test. Ruff lint and formatting, JavaScript syntax checks, and source/wheel builds passed.
- Shared service admission tests cover concurrent callers, separate project fairness, matching workspace paths, identical task IDs in different projects, submission failures and cancelled futures. A real child-process regression verifies that a cancelled task retains capacity until process cleanup finishes and the next task cannot claim early.
- The office browser regression passed, including direct local access, roles/prompts, guidance, task pause/resume, meetings, model discovery, team management, delivery approval, fixed sessions, mobile layouts, runtime attention, cancelled/expired progress semantics, and sync failure recovery. It uses a mock service and does not invoke model software.
- A separate real service on port 7436 used an isolated SQLite snapshot with no runnable tasks and read-only agent permissions. The browser opened the plain URL without pairing and displayed healthy runtime status, capacity 0/2, 7/10 completed eligible steps (70%), four cancelled steps, one pending decision and three historical failures, matching the API snapshot. Original data and the pending execution decision were preserved.
- The existing port 7435 process was not restarted: automatic approval rejected the combined stop/restart command without a more specific reason. Restart that original service to load the final direct-access change. The port 7436 snapshot is for verification, not ongoing project work.
- No live vendor model request was made in this pass. The service admission limit is not an OS CPU/memory quota and is not shared between independent service/dispatch processes. Same-directory editing is serialized; separate directories or worktrees are required for concurrent editing.

## Process and IPC verification (2026-10-13)

- Windows / CPython 3.12.13: **180 passed, 1 platform-specific skip**, combined line/branch coverage **92.42%**. Ruff lint/format checks passed. The skipped case exercises POSIX `SIGTERM` cleanup and runs on the Linux/macOS CI jobs.
- Real subprocess regressions cover a wrapper exiting before its children/grandchildren, command failure, execution timeout, local stop, lease loss, final stderr/event-log limits, and cancelling an outstanding HTTP renewal.
- Windows tests forcibly kill the owner without running Python cleanup and verify the job's descendants stop. Startup-gate tests deliberately delay or fail job assignment and verify that the adapter cannot start early.
- Transport tests exercise a real HTTP server that never replies or trickles response bytes, polling connection reuse/credential replacement, bounded parallel polling, independent failure backoff, invalid envelopes and bounded response storage.
- A real service process, authenticated dispatcher and adapter tree verify normal publication and operator cancellation, including no publication after cancellation. The regression for an unknown completion outcome still replays the saved request exactly without a second artifact version.
- The 0.3.0 wheel was rebuilt and installed into a fresh environment outside the checkout. All six MCP protocol demo checks and the two-role subprocess handoff passed there, including the packaged Windows bootstrap.

See [process ownership and control traffic](process-lifecycle.md) for the limits of each operating system. This run used executable fixtures and local protocol services; the earlier native Codex acceptance below is historical evidence, not a new vendor-session run after these lifecycle changes.

## v0.3 initial local verification

- Windows / CPython 3.12.13: **150 pytest cases passed**, combined line/branch coverage **93.86%**; Ruff checks passed.

- Task management: lifecycle transitions, revocation of running leases, operator CAS races, dependency cycles, missing/stale prerequisites, upstream reopening, filtered pagination and task history.
- Background execution: real adapter subprocesses, priority ordering, parallel office slots (including multiple slots for one provider), prerequisite handoff, cancellation, explicit retries, failure isolation and shutdown. Configuration paths and credentials stay local.
- Browser acceptance: the existing 14 role/model checks and 9 new task-management checks pass against real HTTP services in headless Edge, including desktop/390px layouts and untrusted text handling. Run `scripts/check_tasks_console.py` with the same Playwright environment described below.
- Built-in Codex: installed CLI 0.130.0, configured `gpt-6-astra`/`max`, launched by the dispatcher without desktop interaction. It created `order_total.py` and published a structured artifact. An independent runner executed 54 checks, including 18 unique examples drawn from the previous Codex spec/Harness cases and 36 additional validations. See the [redacted native execution report](verification/native-dispatch.json).
- Native testing exposed a missing Windows sandbox setting and a false-success condition for blocked model responses. The adapter now preserves the user's configured sandbox implementation and maps blocked/failed responses to failed tasks.
- The 0.3.0 wheel includes migration 3 and installs into a fresh environment outside the checkout. The six-check real MCP protocol demo and two-role command example pass from that installed wheel.

The native run verifies one Codex model task. It does not certify every CLI version or model, and it does not establish native background execution for DSH/ZCode. The earlier DSH desktop MCP session read a Codex artifact and published test cases; that is separate evidence from the built-in Codex adapter.

## OpenCode local verification (2026-10-20)

- OpenCode Desktop 2.0.32 on Windows exposes a headless `opencode-cli.exe` under `%LOCALAPPDATA%\Programs\@opencodedesktop\resources`. Discovery found 30 models: 20 Anthropic models and 10 `opencode/*` models. The local `GET /api/model` response reported zero input/output/cache prices for all 10 `opencode/*` models and nonzero base prices for the Anthropic models.
- A live adapter smoke test used `opencode/deepseek-v4.1-flash-free`, the `plan` agent and the private `--standalone` JSON event stream. The model returned one valid structured `CodexResult` (`completed`, no files, no workspace changes). This verifies the CLI discovery, free-model catalogue, event parsing and result validation path for a single empty task; it is **not** a multi-file coding-quality or quota-exhaustion test.
- The suite at that point was **475 passed, 1 skipped** (the POSIX-only service-manager SIGTERM test on Windows); Ruff check and format checks pass.

## SQLite storage, group logs and parallel office verification (2026-10-21)

- Schema 7 migrated the live ledger in place. The legacy `projects.json` was imported into `local_projects` and removed; existing task events with member assignments were backfilled into `group_logs`.
- The live `/v1/group_logs_list` endpoint returned newest-first cursor pages, level filtering and the summary (level/group/task counts and average completed-task time) against the backfilled ledger. The office log view rendered the same data without changing task state.
- Dispatcher tests start two tasks through two slots for one provider in a shared checkout, verify distinct claims and finish both in less than the serial duration. Office dispatch tests verify member tasks have no sibling dependencies.
- The member conversation panel was exercised in the live browser with date separators, status pills, fenced code, collapsible long output and older-record paging; the server-side history endpoint returns `has_more`/`next_before`.
- `node --check` passes for `office.js` and `scripts/check_office.cjs`; the mock browser check now covers the log view and level filtering.

## Agent management and encrypted keys verification (2026-10-21)

- Schema 8 migrated the live ledger in place and added `agent_credentials` without changing existing task, office or group-log rows.
- Live discovery found Codex, OpenCode and ZCode ready with 47 models, and DSH installed with missing API keys. The DSH desktop-account provider was reported as unsupported in headless mode rather than offered as a runnable binding.
- A temporary local service exercised `/local/agents`, `/local/agent-credential` and `/local/agent-credential-delete`. The SQLite ciphertext, mask and private key file did not contain the submitted plaintext; the API returned only the mask, and deletion removed the stored row and restored the missing-key state.
- The mock browser check covers the agent inventory, missing-key prompt, encrypted save/mask display and deletion. The live browser check rendered four agent cards and the DSH provider/key details without page errors.
- The suite at that point was **490 passed, 1 skipped** (the POSIX-only service-manager SIGTERM test on Windows); Ruff, `compileall`, `node --check` and the mock browser check pass.

## Security and durability hardening verification (2026-10-23)

- **State isolation:** a connected project may not contain, or live inside, the RelayFence state directory. New installations default to platform application data; an existing `.relayfence` keeps working. This prevents `workspace-write` agents from reading `access.token` or the credential key and overwriting private agent files.
- **Office pairing:** the startup code is exchanged for an HttpOnly, SameSite cookie; the public `X-RelayFence-Local` marker grants administration only when no browser sessions are configured (`create_app(local_ui=True)` compatibility). Pairing is POST-only, same-origin JSON and loopback-only.
- **Skills:** Codex, OpenCode, ZCode and DSH now use their real project skill roots (`.codex/skills`, `.opencode/skills`, and the shared `.agents/skills`). Injection targets the source directory name, validates containment, holds the workspace skill lock for the whole skill-bearing run, removes its marked staging directory afterwards, and Codex explicitly disables every unselected discovered skill, including an empty selection.
- **Ledger integrity:** deleting a task is refused while another claim snapshot or a meeting `waiting` list references it; claims and retries consider meeting pauses and every blocker; agents cannot write reserved `tasks/` or `meetings/` keys; duplicate revisions return `409 state_conflict`; item heads never downgrade; list and snapshot responses are bounded and migration 10 adds the supporting indexes.
- **Runtime fixes:** handoff compares only model/parameters and preserves skills, legacy project imports are all-or-nothing, unknown diagnosis types no longer echo vendor text, vendor streams tear down their reader, transient execution failures back off, member-chat cursors are composite, Ctrl+C/SIGBREAK and discovery processes clean up, directory picking is single-flight, credential-key creation is race-safe and a missing key no longer leaves a provider looking authenticated.
- The suite is **554 passed, 1 skipped** (the POSIX-only test on Windows); Ruff, `compileall`, `node --check`, and the mock browser check pass. The browser check now pairs with `#pair=...`, then authorizes `/local` and `/v1` through the session cookie.

## Task command chat verification (2026-10-23)

- Added a read-only `/local/group-chat` projection over committed `group_logs`, `member_messages` and published `revisions`. It prioritizes real guidance text, shows lifecycle/failure events, and renders completed task summaries without generating simulated dialogue.
- Cursor tests cover incremental `after` polling, older `before` paging, unknown groups, invalid cursors and bounded limits. The command center polls every two seconds while visible and preserves scroll position when older history is prepended.
- The mock browser check now verifies the per-group chat panel, live event rows, composer visibility and at least one `/local/group-chat` request. The suite is **557 passed, 1 skipped**; Ruff, `node --check` and the browser check pass.

## Task command chat deep-review verification (2026-10-25)

- **Historical delivery integrity:** a task completed more than once shows the revision references recorded by each completion, with the matching event used when the group-log details were truncated. A later revision or reopened task cannot rewrite an earlier chat result. New regression tests cover per-completion outputs, event fallback and the no-current-output fallback.
- **Deletion retention:** task deletion writes its audit event before removing the meeting assignment, so the event lands in the group that owned the task. The deletion event retains the title, and guidance text remains fully readable after `member_messages` are deleted; deleted-task messages therefore keep attribution instead of an opaque id.
- **Independent office tasks:** lifecycle events for tasks with an `office/<member>` role but no `meeting_tasks` row are mirrored into the owning group. Long member ids use the same digest threshold as the backend, and `office_get` now exposes an exact `member_roles` map for the browser.
- **Guidance state:** an unresolved send keeps its retry state and asks before leaving the group; a same-group channel click no longer resets it. A successful send stays confirmed even if the following chat refresh fails. Startup recovery marks unfinished guidance failed **and** writes a visible `chat.failed` group-log entry with the reason.
- The mock browser check verifies the chat composer/feed and a long-member role mapping in addition to the earlier office flows. `node --check` passes for `office.js` and `scripts/check_office.cjs`.
- Final suite: **592 passed, 1 skipped** (the POSIX-only service-manager SIGTERM test on Windows); Ruff lint and `compileall` pass. This pass used deterministic ledger fixtures and the mock browser service; no live vendor model session was re-run (the available accounts remained quota- or credential-blocked).

## v0.2 historical verification

- Windows, CPython 3.12.13, MCP Python SDK 2.3.0: **107 pytest cases passed**, combined line/branch coverage **93.70%**.
- Added tests cover instance identity/project scoping, revoked credential replay, model/parameter/capability validation, immutable running snapshots, role rebinding, reassignment, concurrent revision updates and schema-v1 receipt compatibility.
- Real modern/legacy MCP and stdio bridge calls enforce the same instance identity; no host-context identity is accepted from tool arguments.
- Real subprocess tests verify delivery of selected model/parameters, lost completion recovery, timeouts, invalid output and lease-loss termination. The two-role example also passes over real HTTP.
- The built wheel installs into a new environment outside the checkout and includes both SQL schemas and HTML. Both the original six-check protocol demo and the new role/model handoff pass there.
- Headless Edge browser acceptance: register/edit instances and model options; issue/clear credentials; save role/model/parameter bindings; create/reassign tasks; external-session guidance; dependent selects; no silent model fallback after concurrent catalogue edits; untrusted labels rendered as text; safe project switching; no JavaScript errors; 390px mobile without horizontal overflow.

The actual [roles console screenshot](assets/roles-models.png) uses labelled demo models, not live vendor model sessions. Browser checks are reproducible with Node/Playwright installed: set `RELAYFENCE_PLAYWRIGHT` to the Playwright Node module path and optionally `RELAYFENCE_BROWSER` to an installed Chromium/Edge executable, then run `uv run python scripts/check_roles_console.py` from the checkout. The script creates a new private test ledger, stops its temporary service automatically and writes screenshots plus `.relayfence/roles-ui-report.json`.

## v0.1 historical baseline

- Windows, CPython 3.12.13, MCP Python SDK 2.3.0.
- 65 pytest cases passed, including independent process races and process loss before commit.
- Combined line/branch coverage: 93.76% (pytest-cov); core ledger reported 99%.
- Ruff lint and formatting checks passed.
- Both source distribution and wheel built successfully.
- The wheel was installed into a new virtual environment outside the checkout; packaged SQL/HTML were present and all six protocol acceptance checks passed.
- Browser console checked in headless Microsoft Edge at desktop and 390px mobile widths: authentication errors, current/stale data, token clearing, project switching, untrusted text, pagination, JavaScript errors and horizontal overflow.

The public [demo report](verification/demo-report.json) is from a real local MCP run. It contains no tokens. The README screenshot shows that ledger with additional example work created through the API.

The CI workflow repeats tests on Ubuntu (Python 3.11 and 3.13), Windows (3.13) and macOS (3.13), and separately installs/tests the wheel on Ubuntu. See the [actual workflow runs](https://github.com/shyangar/relayfence/actions/workflows/ci.yml) for current outcomes.

## What was not established

The protocol demo uses official SDK clients, including modern and legacy connections. It does not launch Codex, OpenCode, ZCode or DSH model sessions. The v0.3 Codex test used existing CLI authentication; OpenCode has a recorded single-turn local adapter smoke test above, while multi-file OpenCode task execution remains covered by fixtures. No separate model API integration, cross-machine deployment, high-load benchmark, power-loss hardware test or third-party security audit was performed.

The process-loss test verifies SQLite rollback/durable commit boundaries. Storage hardware and filesystem behavior remain part of the deployment assumptions.
