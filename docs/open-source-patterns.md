# Open-source patterns applied to RelayFence

Reviewed on 2026-10-26. The implementation uses the projects' published workflow ideas; no source code from these projects was copied.

| Reference | Useful pattern | Applied here |
| --- | --- | --- |
| [Vibe Kanban](https://github.com/BloopAI/vibe-kanban) | Separate planning, execution and review; make work visible | Five-column delivery board with objective/iteration filters and direct task inspection |
| [Squad workflow](https://github.com/josemodena/squad/blob/main/docs/guides/workflow.md) | Independent review of a specific revision, durable execution ownership, explicit user pauses | Acceptance tied to a task fence; revision CAS; approval freshness checks; rejection pauses instead of automatically spending another attempt |
| [Claude Squad](https://github.com/smtg-ai/claude-squad) | Distinct task sessions and inspectable execution state | Paginated per-claim execution history retaining takeover, model selection, failure and interruption records |

Vibe Kanban's README currently announces sunsetting. It is a design reference, not a new runtime dependency. The projects' licensing and operating-system requirements are not inherited because this work does not copy or embed their implementations.

## Delivery contract

`TaskCreate.review_required` and `OfficeDispatch.review_required` default to false for API compatibility. New meetings created in the office enable the checkbox by default. Only member execution steps require acceptance; analysis and planning continue normally. Meeting revisions retain the policy.

The storage status remains `done` when outputs are atomically published. For opted-in deliveries, `display_status=awaiting_review` and a fence-specific review row distinguish published output from accepted delivery. Dependencies and meeting advancement require approval. The task list supports `awaiting_review`, `waiting_user` and `lease_expired` filters. Existing tasks are not retroactively gated.

`task_review` is an administrative mutation with task revision CAS, an explicit note and optional evidence references. The producing actor cannot approve its own delivery; all agent credentials remain unable to call this administrative endpoint. Approval refuses stale inputs/results. Evidence is a human record, not automatic proof that a command was run. Rejection retains the output/review history, appends actionable feedback and leaves the task paused; explicit resume is required to spend another attempt. It is not an automatic independent AI reviewer.

## Attempts and handoffs

`task_inspect` derives attempts from committed claim events, grouped by fence and paginated by immutable claim sequence. Heartbeats cannot push earlier attempts out of the page. A takeover is distinct from a successful completion. Lease tokens are never included. Review history and bounded handoff history accompany the inspection, while artifact values are loaded separately by exact version.

Manual member transfer creates a versioned handoff state item with the source and recipient, previous fence, published output references, known failure and operator's next-step instructions. Its key is appended to task inputs, so the receiving claim actually receives the handoff. Existing files remain in the checkout; the handoff does not assert that these files were diffed or tested. The task stays paused until the user resumes it.

## Team load and current limits

The team library computes per-member running, queued, blocked, paused, failed, waiting-user, awaiting-review and expired-lease counts from the same project ledger. These are workload counts, not provider capacity or quota estimates.

Shared checkouts still use the existing exclusive process lock. This change does not implement automatic Git worktree creation, merging, native child-session control, team template versioning or autonomous reviewer delegation. Real provider sessions still need per-adapter acceptance testing; the automated suite and browser fixtures do not establish vendor account availability.
