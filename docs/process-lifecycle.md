# Process ownership and control traffic

RelayFence keeps model execution separate from control traffic. A slow coordinator must not prevent a local timeout or stop request from terminating an adapter, and an exited adapter must not leave its ordinary descendants running in the workspace.

## Process lifetime

The worker owns the adapter until its process tree has been stopped and its final output has been checked. It keeps the workspace lock throughout that cleanup, then submits through the existing lease/fencing checks.

- **Windows:** a private Job Object uses `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, without breakaway permissions. A small Python bootstrap waits on an anonymous pipe. The worker assigns the bootstrap to the job **before** allowing it to create the adapter. Assignment failure aborts execution. This avoids the race between creating a fast wrapper and assigning it after it has already spawned children. The bootstrap uses the base Python interpreter because a Windows virtual-environment redirector can itself spawn a child early.
- Closing the Windows job terminates its remaining members even after the adapter exits. The worker waits for the job's active-process count to reach zero before releasing the workspace. The kernel also closes the job handle when the worker process crashes or is forcibly terminated.
- **Linux/macOS:** the adapter starts in a new session/process group. Cleanup signals that group even when its leader has exited. `SIGTERM` for the standalone worker/dispatcher and Ctrl+C use normal cleanup. An uncatchable runner `SIGKILL` does not have the Windows job's owner-death guarantee.

These are lifecycle controls for trusted local adapters, not a sandbox. POSIX descendants that deliberately create a new session, programs launched through an unrelated service, remote actions, and changes already made to files are outside this cleanup guarantee. Windows sandbox implementations that use external services may also launch processes outside the job. Keep isolated worktrees and perform external acceptance checks.

Implementation: [`processes.py`](../src/relayfence/processes.py), [`_windows_job.py`](../src/relayfence/_windows_job.py), and [`_process_gate.py`](../src/relayfence/_process_gate.py). Windows semantics follow Microsoft's [Job Objects documentation](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

## Lease and execution deadlines

The adapter monitor checks local stop requests, execution deadlines and output limits approximately every 50 ms while running. Lease renewal uses a separate asynchronous HTTP connection pool. Its requests can be cancelled without abandoning a thread that might later renew a lease.

`heartbeat_timeout_seconds` in `worker.json` defaults to **5 seconds** (allowed range 0.2–30). It bounds the complete renewal request, including connection establishment and a response that keeps sending small chunks. A failed/timed-out renewal stops the adapter; execution does not continue speculatively while ownership is uncertain. The existing renewal interval is at most 3 seconds.

The request is cancelled and its transport closed when execution ends. Local stop/timeout checks do not wait for that request's server response. A cancellation already sent over the network cannot undo a server-side heartbeat transaction; fencing still prevents a revoked lease from publishing a result. Execution and heartbeat deadlines are separate from the bounded time needed for OS process cleanup.

## Dispatcher connections and recovery

Each configured identity has one reusable asynchronous polling client. A polling round runs identities concurrently, then orders the available candidates by priority/deadline/creation time as before. Ordinary workspace locking and `max_parallel` still apply. Task execution has separate clients, so rotating or closing a polling connection does not close an active task's transport.

`request_timeout_seconds` in `dispatcher.json` defaults to **3 seconds** (allowed range 0.2–10). It bounds the complete per-identity polling sequence, including paginated role/task reads. It also sets the per-I/O timeout for the task's synchronous preflight/publication requests. A round can wait for a slow identity up to its polling deadline; waits are concurrent rather than added once per identity. Very large catalogues or slow storage may require increasing this setting.

Connection/authentication failures back off independently: `poll_seconds`, twice that interval, and so on, capped at 30 seconds, with attempts made on subsequent polling rounds. A successful probe resets the delay. Token files are reread on probes; replacing a token retires its old polling client. Existing executions keep their original credential and stop if it is revoked. No adapter is rerun to recover a lost publication response.

HTTP and asynchronous control clients validate success/error envelopes and HTTP status consistency. They bound decoded replies at 64 MiB to allow lists/claims containing multiple 128 KiB values. Invalid, oversized or interrupted replies report `transport_error`; mutation requests are not automatically repeated. Preserve the saved request ID and payload for an exact replay.

## Logs and health snapshots

Stdout (128 KiB), stderr (1 MiB), and Codex event logs (8 MiB) are checked during execution and **again after process cleanup**. They remain file-backed, so a burst can exceed a limit between checks. These are rejection/termination thresholds, not hard filesystem quotas.

`runs/health.json` is written to a private temporary file and replaced atomically, so a successful read sees a complete old or new snapshot. Windows can briefly deny a read or replacement while another handle is open. Readers should retry transient file-open errors; the dispatcher preserves the previous snapshot and retries a failed write next round. Health-file failure alone does not abort running tasks. A crashed dispatcher can leave a stale snapshot: the service's runner-heartbeat timestamp is the source for online/offline presence.

## Reproduce the failure tests

```sh
uv run pytest tests/test_process_lifecycle.py tests/test_dispatcher.py tests/test_cli_client.py -q
```

The lifecycle suite launches real wrappers and grandchildren, tests early parent exit, failure, timeout, lease loss and local stop, kills a Windows owner without Python cleanup, checks the startup gate, and exercises HTTP servers that withhold responses or trickle bytes. Platform-specific checks are skipped on the other operating systems. No model account or API key is used.
