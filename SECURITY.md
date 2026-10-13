# Security policy

This alpha targets cooperating clients under one user's trust boundary. The admin bearer token grants access to every project. Instance credentials bind one project/actor and cannot edit registry or assignment settings. Instances still share all project data. Legacy unregistered actors remain audit labels. Do not deploy this as a public multi-tenant service.

The CLI binds only to loopback. Host and Origin checks cover HTTP/MCP routes. Data routes require a bearer token; the HTML shell and process-liveness endpoint contain no ledger data. Lease tokens are omitted from public task/event reads.

Protect instance/run directories and backups. Receipts preserve raw tokens from successful claim and credential-rotation responses. Public reads omit credentials and hashes. Authorization is checked inside the ledger transaction before receipt replay. POSIX creation modes are restricted; Windows inherits directory ACLs. Existing directory permissions are not changed.

The API does not execute stored content, fetch URLs or open caller-specified files. The optional worker CLI executes an operator-supplied local adapter with `shell=False`, outside the HTTP process. It is not a sandbox: trusted adapters have the operator's OS permissions/vendor authentication. The worker does not pass its bearer or lease token to the child. Treat stored content as untrusted data. External actions and detached descendants are not protected by fencing. Model catalogues and reports are declarations, not provider attestations.

Report a suspected vulnerability through [GitHub private vulnerability reporting](https://github.com/shyangar/relayfence/security/advisories/new) if available. If it is unavailable, open a minimal issue requesting a private contact channel without including exploit details or credentials. Never include access tokens, lease tokens or a real ledger database in a public report.

The current 0.3 series receives fixes as maintainer capacity permits; there is no response-time SLA or external security audit claim.
