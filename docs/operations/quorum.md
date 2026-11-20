# Review quorum operations

The [review charter](../governance/review-charter.md) remains the authority.
`scripts/quorum_check.py` evaluates current-head reviews against the base
branch's roster and ownership rules; a green checker is not a replacement for
the charter.

## Independent maintainer reviews

For protected-path automation PRs and every PR opened by the workflow account,
the required maintainer approval counts only when the maintainer is not a known
author, co-author or material committer/pusher on that change. The same
exclusion applies to the additional maintainer approval required for a large
ordinary PR. An otherwise current approval from a writer leaves the PR held;
it does not create a substitute reviewer or lower the required quorum.

Keep the required reviewer out of material commits on a future bot PR. The
bot or another independent contributor can make the fixes. If no eligible
required reviewer remains, leave the PR held for a separately authorized
governance decision. Do not use another account, proxy approval or a roster
edit to manufacture independence.

The existing exception for a base-only merge or rewrite applies only when the
checker proves the net diff unchanged. Unknown comparison provenance keeps
the contributor classified as a writer. Automatic checks use known provenance;
the charter still binds relationships the API cannot establish.

A Draft's neutral result does not establish readiness or authorize queue
arming. Current-base policy, standing objections, the 72-hour governance
window and the normal merge queue still apply. The maintainer's own-change
charter exception and ordinary quorum thresholds are unchanged.
