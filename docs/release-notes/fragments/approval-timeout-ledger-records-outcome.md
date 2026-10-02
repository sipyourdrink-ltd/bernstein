## The approval ledger records an expiry-approved task as approved

When a review expired with no decision and `approve_on_timeout=True` let the
task proceed, the approval gate still wrote a signed `grant_refused` record to
the audit chain, so the chain said "refused" for a task that went ahead. The
ledger now records what happened: a `grant_issued` entry whose signed reason is
`approved_by_timeout_policy`, making clear that the policy approved it and no
person did. The fail-closed default is unchanged: an expiry without the opt-in
is rejected and recorded as a refusal, as before. `GrantLedger.issue_grant`
gained an optional `reason` argument for this.
