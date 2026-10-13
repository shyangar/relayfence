# Release verification

Initial verification date: 2026-10-10. This file records actual checks and their scope, not a reliability SLA.

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
- Background execution: real adapter subprocesses, global priority across agents, prerequisite handoff, workspace locking, cancellation, explicit retries, failure isolation and shutdown. Configuration paths and credentials stay local.
- Browser acceptance: the existing 14 role/model checks and 9 new task-management checks pass against real HTTP services in headless Edge, including desktop/390px layouts and untrusted text handling. Run `scripts/check_tasks_console.py` with the same Playwright environment described below.
- Built-in Codex: installed CLI 0.130.0, configured `gpt-6-astra`/`max`, launched by the dispatcher without desktop interaction. It created `order_total.py` and published a structured artifact. An independent runner executed 54 checks, including 18 unique examples drawn from the previous Codex spec/Harness cases and 36 additional validations. See the [redacted native execution report](verification/native-dispatch.json).
- Native testing exposed a missing Windows sandbox setting and a false-success condition for blocked model responses. The adapter now preserves the user's configured sandbox implementation and maps blocked/failed responses to failed tasks.
- The 0.3.0 wheel includes migration 3 and installs into a fresh environment outside the checkout. The six-check real MCP protocol demo and two-role command example pass from that installed wheel.

The native run verifies one Codex model task. It does not certify every CLI version or model, and it does not establish native background execution for DSH/ZCode. The earlier DSH desktop MCP session read a Codex artifact and published test cases; that is separate evidence from the built-in Codex adapter.

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

The protocol demo uses official SDK clients, including modern and legacy connections. It does not launch Codex, ZCode or DSH model sessions. The v0.3 Codex test used existing CLI authentication. No separate model API integration, cross-machine deployment, high-load benchmark, power-loss hardware test or third-party security audit was performed.

The process-loss test verifies SQLite rollback/durable commit boundaries. Storage hardware and filesystem behavior remain part of the deployment assumptions.
