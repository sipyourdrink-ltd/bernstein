# Pull request review routing

`.github/workflows/core-reviewer-review.yml` requests one core reviewer when
a non-draft pull request is opened, reopened, or marked ready for review,
including fork pull requests. It reads `core_reviewers` from
`.github/quorum-roster.toml` at the event's base SHA, excludes the author
(case-insensitively) and expired roster terms, then chooses the person least
recently requested in the repository's issue-event history. A person absent
from that history goes first; ties sort by login. The run log records each
candidate's latest request and the choice. API failures fail the run rather
than selecting from incomplete history.

An existing pending core request is preserved on reopening. Drafts and a
roster with no eligible reviewer receive no new request. The router has no
checkout, executes no pull request code, and grants only `pull-requests: write`.
Concurrent pull requests can observe the same history and select the same
person; this is recency routing, without a shared reservation queue.

CODEOWNERS has no catch-all; its protected-path entries remain. The area
steward router and the hourly sweep for missing approvals continue separately,
so they may request additional reviewers. Anyone may review any pull request.
Routing does not change approval eligibility or quorum: `quorum.yml` and
`scripts/quorum_check.py` still enforce the
[review charter](../governance/review-charter.md).

Reviewers should use `needs-committer-review`, oldest first, as their work
list rather than relying on notifications. The charter's sixty-day lapse
rule still applies even when a reviewer has received no requests.
