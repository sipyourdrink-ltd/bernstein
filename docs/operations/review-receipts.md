# Review and autofix receipts

A worker that reviews a pull request leaves behind a single signed artefact
binding the issue, the plan, every tool call, and the resulting diff — so a
reviewer can prove offline that the PR was generated from the ticket without
operator override. A related, narrower receipt links an autofix commit back
to the finding it addresses.

## PR review receipts

```
bernstein review-receipt emit   --pr <url> --repo <owner/repo> \
    --issue <issue.md> --plan <plan.md|-> --diff <pr.diff> \
    --journal-head <head> --verdict <verdict> [--task-id <id>] [--workdir DIR]

bernstein review-receipt verify --pr <url> --issue <issue.md> --diff <pr.diff> [--workdir DIR]
```

`emit` binds `{issue_hash, plan_hash, journal_head, diff_hash, verdict}` into
one record, signs it with the install's Ed25519 identity, and anchors it in
the review lineage spine. `--plan` accepts a file path or `-` to read the
plan from stdin.

`verify` recomputes `issue_hash` and `diff_hash` from the presented `--issue`
and `--diff` files, checks the Ed25519 signature against the receipt's
embedded public key, and confirms the record's spine anchor. Exit codes:
`0` verified, `1` no receipt found (or bad input), `2` mismatch (tamper).

The tracker comment posted on a PR is a *projection* of the receipt — a
short verdict plus the `verify` invocation a reviewer can run — never the
receipt body itself.

## Autofix receipts

An `AutofixReceipt` links a reviewer *finding* to the commit that fixed it
and the gate result, produced when a fix is applied in an isolated
`git worktree` (never the primary working tree) and then torn down. The
receipt binds `{finding_hash, fix_commit_hash, gate_passed, gate_summary,
task_id, timestamp}`, is Ed25519-signed, and is anchored in its own spine
(run id `review-autofix`) alongside the review receipts.

Records land at `.sdd/reviews/autofix/<finding_hash>.json`.

Unlike PR review receipts, autofix receipts have no dedicated top-level CLI
verb as of this writing — `run_autofix_in_worktree` and
`verify_autofix_receipt` are library functions
(`src/bernstein/core/review/receipt.py`) called by the code that drives an
autofix attempt, not something an operator invokes directly.

## Related receipt types

The same "signed, journal-anchored, offline-verifiable" pattern covers
several other receipt families documented separately:

| Receipt | Command | Doc |
|---|---|---|
| Stall escalation | `bernstein escalation show / verify` | [Stall escalation receipts](stall-escalation.md) |
| Mandate consent | `bernstein mandate emit / verify / revoke` | [Spending mandates](spending-mandates.md) |
| Webhook node | `bernstein webhook verify` | [Audited webhook node](webhook-node.md) |

## Source

`src/bernstein/core/review/receipt.py` (`ReviewReceipt`, `AutofixReceipt`,
emit/verify logic), `src/bernstein/cli/commands/review_receipt_cmd.py`
(`bernstein review-receipt`).
