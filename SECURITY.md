# Security policy

This alpha targets cooperating clients under one user's trust boundary. The shared bearer token grants access to every project on its service. Actor names are audit labels, not authenticated identities. Do not deploy it as a public multi-tenant service.

The CLI binds only to loopback. Host and Origin checks cover HTTP/MCP routes. Data routes require a bearer token; the HTML shell and process-liveness endpoint contain no ledger data. Lease tokens are omitted from public task/event reads.

Protect the instance directory and all backups. Receipts preserve successful claim responses, including their lease tokens. POSIX creation modes are restricted for a new token/directory; Windows inherits the selected directory's ACL. An existing directory's permissions are not changed.

The API accepts JSON and external artifact references but does not execute stored content, fetch referenced URLs or open caller-specified files. Agents must treat stored content as untrusted data. External actions are not protected by RelayFence fencing.

Report a suspected vulnerability through [GitHub private vulnerability reporting](https://github.com/shyangar/relayfence/security/advisories/new) if available. If it is unavailable, open a minimal issue requesting a private contact channel without including exploit details or credentials. Never include access tokens, lease tokens or a real ledger database in a public report.

The current 0.1 series receives fixes as maintainer capacity permits; there is no response-time SLA or external security audit claim.
