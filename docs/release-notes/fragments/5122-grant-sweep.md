## Grant sweep assertion on every reconcile (Issue #5122)

Every reconcile run now asserts the revoked grant set is absent from the active
set by deriving both sets from `GrantChainResult.lifecycles()` via
`compute_grant_sets()`. A revoked grant still present is a `measured, failed`
audit finding (category `grant-sweep`, severity `critical`), journaled with
the decision record that authorized the reconcile run. The revoked set is
generated from `GRANT_REVOKED` decision records, never hand-curated, so it
cannot drift from the chain. (`#5122`)
