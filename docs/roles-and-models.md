# Roles, agent instances and model selection

v0.2 adds configurable **role → agent instance → model/parameters** bindings. A role also has instructions, required capabilities and deliverables. Multiple instances may use the same agent software with different models. No vendor or model is hardcoded to a role.

## Configure in the console

1. Start `relayfence serve` and connect at `http://127.0.0.1:7433/` with the admin `access.token`.
2. Expand **Agent instances & model catalogues**. Register a unique instance ID, software name, connection mode, capabilities and model IDs. Add allowed parameter values under each model when needed. Save the instance.
3. Click **Issue / rotate credential** and download its `.token` file to a private directory. Rotation immediately invalidates the previous credential. Use this token for this instance; keep the admin token with the operator.
4. Under **Role bindings**, enter a role ID, choose the instance, then choose one of its models. Supported parameters appear as dropdowns. Add instructions and deliverables, then save.
5. Under **Assign work**, create a task with that role and all shared input keys. Existing ready or expired tasks have a **Reassign** button. Live work must be released first; completed work must be explicitly reopened first.

Model catalogues and capabilities are **operator-configured**, not remotely discovered or certified. Add only options available to your account and adapter. Unsupported combinations are rejected at role save and again at claim, so a later catalogue change cannot silently fall back to another model. Capabilities are declared metadata, not tested permissions. Instructions and deliverables describe the job; RelayFence does not judge semantic compliance.

The console uses revision checks to avoid overwriting another editor's changes. On `revision_conflict`, click Edit to reload the current object before making a new change. For a lost response, retry the unchanged form in the same tab: its request ID is retained. The browser does not persist drafts, credentials or pending requests across reloads.

## External sessions: Codex, ZCode, DSH and other MCP clients

Use mode **External session / MCP** for an independently running client. RelayFence can assign work to its credential, but it cannot change that external conversation's model through its coordination tools. Either choose **Use external session settings** (unknown model) or select a required model and configure that model in the client before claiming.

Generate the existing connector snippet with the **instance** token path, for example:

~~~bash
relayfence config codex --token-file /absolute/private/codex-reviewer.token
~~~

Use a quoted Windows path on Windows. For ZCode/DSH substitute the client name as documented in [clients](clients.md). Do not share the admin token with role workers. If `RELAYFENCE_TOKEN` is set, it overrides `--token-file`; unset it or set it to the correct instance credential.

Give the client its project and instance ID. Its mutation `actor` must equal the authenticated instance ID. To claim, read `task_get`, then `role_get`, configure the external session and send the role's exact selection:

~~~json
{
  "request": {
    "project": "shop",
    "actor": "codex-reviewer",
    "request_id": "claim-review-001",
    "task_id": "review-login",
    "selection": {"model": "your-supported-model-id", "parameters": {"reasoning_effort": "high"}}
  }
}
~~~

The example model ID is a placeholder, not a product recommendation. If the role uses external settings, send `"selection": {"model": null, "parameters": {}}`. Acknowledgment must match exactly, including parameters. It proves that the client accepted the assignment, **not which model actually performed the work**.

## Local command adapter: apply the selection to execution

Mode **Local command adapter** supports explicit local execution through `relayfence worker`. The operator creates a private local JSON configuration, outside shared state:

~~~json
{
  "project": "shop",
  "agent_id": "my-command-worker",
  "command": ["/absolute/path/to/python", "/absolute/path/to/my_adapter.py", "{model}", "{parameters_json}"],
  "cwd": "/absolute/path/to/isolated-worktree",
  "timeout_seconds": 600,
  "lease_seconds": 120
}
~~~

~~~bash
relayfence worker --config worker.json --task implement-login --token-file /absolute/private/worker.token --run-directory /absolute/private/run-001
~~~

`--run-directory` must be new. Paths in `cwd` are relative to the configuration file. Use absolute executable/script paths. This is an explicit one-task command, not a background dispatcher; the HTTP/MCP server never executes a command stored in the registry. There are no bundled vendor launchers or automatic model-list queries in this release.

The worker:

1. Reads the role and validates its mode/instance. Claims using the instance credential and exact model selection.
2. Takes the atomic claim snapshot as the authoritative instructions, input values and configuration.
3. Launches the operator's command with `shell=False`. An entire argument `{model}` is required; `{parameters_json}` and `{parameter:name}` are optional. Windows batch files are rejected; use the executable interpreter and script directly.
4. Sends one JSON object on stdin containing `task`, `inputs` and `selection`. The adapter must use the parameters or reject unsupported ones. No lease token or RelayFence bearer token is passed to the child. Existing vendor authentication remains the responsibility of the local adapter.
5. Renews the lease during execution, stops on renewal failure or timeout, validates stdout and publishes through the normal lease/dependency checks.

The adapter must emit **only** one JSON result on stdout. Send diagnostics to stderr:

~~~json
{
  "reported_model": "your-supported-model-id",
  "outputs": [{"key": "build/result", "expected_version": 0, "value": {"commit": "...", "test_report": "..."}}]
}
~~~

`reported_model` may be omitted when unknown. If supplied and different from the requested model, completion is rejected. The task records `requested`, `reported_model`, and `model_evidence` (`unverified` or `worker_reported`). A wrapper must not infer the actual model solely from an argument it received; use real runtime metadata or leave the report unknown. This is not provider-attested model verification, and parameters are not independently verified.

The worker is **not a sandbox**. Run trusted adapter code with the appropriate OS permissions and an isolated worktree. Do not detach subprocesses: stopping a wrapper is not a universal guarantee about detached descendants or external side effects. Command stdout is capped at 128 KiB and stderr is monitored up to 1 MiB; these are application bounds, not OS resource quotas.

## Try a complete model-selection handoff without API keys

~~~bash
uv run python examples/role_models.py
~~~

This starts a real HTTP service, registers two credentialed instances, binds two roles to different **demo model labels**, launches real adapter subprocesses, and publishes dependent artifacts. It saves a persistent ledger and recovery files in a unique `.relayfence/roles-demo-*` directory. The fixture reports the arguments it received; it does not call an LLM or certify a vendor client. See [the adapter contract example](../examples/role_command.py).

## Changing bindings and recovery

- Role edits affect **future claims**, including a reclaim after release/expiry. Running work retains its role revision, instructions, deliverables, agent revision and requested selection. An old owner may finish its live lease even if the role now points elsewhere.
- `task_assign` uses task revision CAS, accepts a role ID or `null`, and resets only ready/expired work. Old lease tokens cannot submit after reassignment.
- Disabling an instance or rotating its token blocks old credentials, including replay requests. Give a replacement credential only to the intended instance. The admin token cannot impersonate registered instance actors to retrieve their claim receipts.
- On process failure before completion, the worker attempts to release its lease. It does not automatically rerun the command.
- Recovery records are written before requests. If completion's response is lost, retry `task_complete` with the saved `complete-request.json`, the same instance token and the same request ID. Inspect current task state before any new execution. A recovered claim receipt is historical and requires a live-lease check/renewal before work.
- Once a completion request is saved, the worker retains the lease for inspection even if publication is rejected. Resolve the returned error and explicitly release/reclaim as needed. Never rerun an adapter solely because a network reply was lost.

Keep run directories and backups private. Claim recovery files contain lease tokens; logs and input/output files can contain project data. File creation uses restricted POSIX permissions; Windows inherits directory ACLs. Unknown model or parameter mismatches never trigger silent fallback.
