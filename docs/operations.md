# Operations and recovery

## Files and service lifecycle

`relayfence init --directory <directory>` creates an admin access token and a schema-v2 SQLite ledger. Re-running init preserves existing files and applies supported migrations. `serve` reads `RELAYFENCE_TOKEN` first if that environment variable exists, otherwise the configured token file. An empty environment value is an error, not an instruction to disable authentication.

The default directory is `.relayfence` relative to the current terminal, so use an absolute `--directory` in scripts/services. Windows, macOS and Linux are supported targets. Only loopback listeners (`127.0.0.1` or `::1`) are exposed by the CLI.

~~~bash
relayfence serve --directory /absolute/private/relayfence --port 7433
~~~

On Windows, use a quoted Windows path in place of the example. Stop the foreground server with Ctrl+C. No daemon, startup service, browser tab, client configuration or scheduled task is installed automatically.

The ledger uses `ledger.sqlite3` plus SQLite's `-wal` and `-shm` files while running. Do not delete or move individual files while the service is live. Keep the directory outside network/cloud-synced storage.

## Backup

~~~bash
relayfence backup /absolute/private/backups/ledger-2026-10-10.sqlite3 --directory /absolute/private/relayfence
~~~

The command uses SQLite's online backup API, so committed WAL changes are included in a consistent snapshot. It refuses to overwrite an existing destination or the live database. Keep backups private: saved claim receipts include lease tokens, and artifact/state values may contain sensitive project data.

## Restore into a new instance

1. Stop clients and the old service. Do not run two independent writable copies as if they were one coordinator.
2. Create a **new private directory** and copy the backup there as `ledger.sqlite3`.
3. Run `relayfence init --directory <new-directory>`. It preserves the restored database and creates a new access token if missing.
4. Start `serve --directory <new-directory>` and reconnect clients using the new token.
5. Inspect tasks and freshness. Existing lease deadlines are preserved, so old leases may already be expired. Review any work performed after the backup timestamp.

Restoring an older backup also restores older fence counters and receipts. Treat it as a new coordination epoch: retire old clients/endpoints, rotate the access token, and never rely on old fencing numbers to authorize an external system after rollback. Lost post-backup writes cannot be reconstructed automatically.

An unknown database schema version fails at startup. v0.2 migrates schema 1 to 2 inside one write transaction. Back up with v0.1 first, stop old service processes, then start v0.2. Migration preserves fences and receipt hashes, including JSON null values, and adds empty registries and nullable assignment fields. Do not run v0.1 against an upgraded database. Rollback requires the pre-upgrade backup and the new-epoch precautions described above; there is no downgrade migration.

## Token rotation and trust

For the admin token, stop the service, replace it with a new high-entropy value (at least 32 ASCII characters without whitespace), update clients, and restart. Instance credentials support online `agent_token_rotate` with revision CAS. Rotation or disabling immediately denies old credentials, including receipt replays. A replacement credential for the same instance can renew its existing lease. Replayed rotation receipts return historical tokens, which may already be revoked by a subsequent rotation; use a new request ID/current revision for an intentional new rotation.

On POSIX, init creates the token as mode `0600` and a newly created instance directory as `0700`. Windows uses inherited ACLs: choose a directory accessible only to the intended user. Existing directory permissions are not silently rewritten. Secure the ledger and backups as carefully as the token.

The admin token accesses the **whole service**. Instance tokens authenticate one registered actor in one project. Instances can read project data, mutate shared state, create tasks and hold leases, but cannot edit profiles, roles, credentials or assignments. Admin calls cannot impersonate registered actors. Legacy unregistered actors remain supported with the admin token. This is for cooperating agents under the same trust boundary, not hostile multi-tenant isolation. Separate trust groups need separate service instances.

## Other machines

v0.2 does not expose a public listener. If you need a remote client, keep the service on loopback and use an SSH local port forward to it. Point clients to the forwarded loopback URL and supply the RelayFence token through a private channel. A bridge can use an HTTPS URL provided by your infrastructure, but configuring and securing an external reverse proxy is outside this release's tested deployment.

Do not disable Host/Origin validation to make an arbitrary hostname work. Both REST and MCP require a bearer token; browser requests also need an allowed same origin. No CORS wildcard is enabled.

## Monitoring and bounds

- `GET /healthz` is unauthenticated process liveness/version metadata, not a storage write-readiness check.
- Successful mutations produce durable events. Failed requests return error envelopes; unexpected failures go to server stderr.
- The console polls every 5 seconds and displays 50 items/tasks per page plus the latest 50 loaded events. Its three counters describe the current pages, not global totals.
- A request may wait up to 10 seconds for SQLite's write lock. Use bounded retries with the same request ID on an unknown outcome.
- Input is capped at 128 KiB; dependency validation at 256 revisions. Store large reports/files externally and put references/digests in artifact JSON.
- Revisions, events and receipts are retained indefinitely. Monitor disk growth. Archive whole inactive ledgers rather than deleting individual receipt rows.

Do not store passwords, model API keys or other secrets in shared item values. Values, titles and descriptions are untrusted project data, not privileged instructions for an agent.
