# Contributing

Start with a reproducible coordination failure, a missing client integration, or a documentation correction. Small, inspectable changes are easier to review than new orchestration layers.

~~~bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=relayfence --cov-report=term-missing
uv build
~~~

Keep requests in models.py, domain decisions in store.py, and transport behavior in the adapters. A core rule must behave identically over MCP and HTTP. Do not add model/provider dependencies to the ledger.

For concurrency changes, include a race or failure test that would fail before the fix. Do not replace process-race tests with mocks. For persistence changes, cover receipts, events and task ownership together. Include migrations before changing the database schema.

Use a new request_id for a new operation and preserve it for an exact retry. Never log tokens, payload values or full credential-bearing requests.

The console is dependency-free HTML/CSS/JavaScript packaged with the Python wheel. If editing it, check desktop and mobile layout, keyboard access, hostile item text and failed-auth behavior in a browser. Prettier can format the file but is not a runtime dependency.

For client integrations, record client/SDK versions, transport, how you reproduced the connection, and what was actually verified. An SDK protocol test is not a vendor model-session test. New evidence belongs in docs/clients.md.

Open an issue before adding distributed storage, a scheduler, RBAC or automatic external execution: these change the trust and consistency model.
