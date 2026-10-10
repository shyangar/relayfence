# Release verification

Initial verification date: 2026-10-10. This file records actual checks and their scope, not a reliability SLA.

## Local

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
