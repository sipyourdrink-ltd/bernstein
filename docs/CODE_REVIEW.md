# Code review

Last reviewed: 2026-09-09.

The rules for how a change reaches `main` live in one place, the
[review charter](governance/review-charter.md). This page is the reviewer's
checklist and defers to the charter for everything else, so that the OpenSSF
Scorecard, operators evaluating the project, and new contributors all read the
same answer.

## The rules, in one paragraph

Every change goes through the merge queue; nobody pushes to `main` directly.
A pull request merges with two approving reviews from committers who are not
its author, at least one of them from a code owner (`.github/CODEOWNERS`),
green required checks on the queued state, and no unresolved *changes
requested*. A new push dismisses earlier approvals, and the person who pushed
last cannot supply the final one. A change over 400 lines, or one touching a
path with `sandbox`, `security`, or `audit` in it, needs a third approval from
a core reviewer. Protected paths (charter section 4: workflows, `core`,
`evolution`, `adapters`, dependency lockfiles, schemas, the security and
governance documents, agent configuration files) also need the maintainer's
approval. The project's own automation merges its own changes when CI is green;
dependency bots merge under their own policy. Neither counts toward a human
quorum.

## Reviewer checklist

An approval says "I read the whole diff and would defend it". Before approving:

1. Pull the branch for any non-trivial change and run the affected tests.
2. Check that new public surface (CLI flags, MCP tools, HTTP routes, adapter
   contracts) has tests and docs in the same pull request.
3. Flag any new dependency, new outbound network call, or new credential read
   in the conversation before approving.
4. Leave at least one line-level comment on a change over about 40 lines: a
   finding, or a note of what you ran. Queue hygiene dismisses an approval that
   carries none, for everyone equally, with a note on how to re-approve. A
   summary without one is welcome as a comment.

## Escalation

A dispute, one approval against one *changes requested*, stays open until the
requester is satisfied or seven days pass; then the thread gets
`needs-maintainer`. Disagreements on security-sensitive changes are resolved by
the maintainer, and when the outcome settles a boundary it is recorded in
`docs/decisions/`.
