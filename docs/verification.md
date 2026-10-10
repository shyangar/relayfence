# Release verification

Initial verification date: 2026-10-10. This file records actual checks and their scope, not a reliability SLA.

## v0.2 local verification

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

The protocol demo uses official SDK clients, including modern and legacy connections. It does not launch Codex, ZCode or DSH model sessions. No model generation, paid API, cross-machine deployment, high-load benchmark, power-loss hardware test or third-party security audit was performed.

The process-loss test verifies SQLite rollback/durable commit boundaries. Storage hardware and filesystem behavior remain part of the deployment assumptions.
