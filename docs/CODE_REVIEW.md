# Code review

Last reviewed: 2026-09-09.

The rules for how a change reaches `main` live in one place, the
[review charter](governance/review-charter.md). This page is the reviewer's
checklist and defers to the charter for everything else, so that the OpenSSF
Scorecard, operators evaluating the project, and new contributors all read the
same answer.

## The rules, in one paragraph

Every change goes through the merge queue; nobody pushes to `main` directly. A
pull request merges with two approving reviews from committers who are not its
author, at least one of them from a core reviewer, green required checks on the
queued state, and no unresolved *changes requested*. The roster that says who is
a core reviewer is `.github/quorum-roster.toml`. Ownership is a second, separate
requirement: every path with a named owner in `.github/CODEOWNERS` needs that
owner's approval too, so a core reviewer's approval does not stand in for a code
owner's on a path the owner holds. A new push dismisses earlier approvals, and
the person who pushed last cannot supply the final one. A change over 400
lines, or one touching a path with `sandbox`, `security`, or `audit` in it,
needs three approvals, two of them from core reviewers, and over 1,000 lines
gets split or sent to the maintainer instead.
Protected paths (charter section 4, including the `.github/` directory, `core`,
`evolution`, `adapters`, dependency lockfiles, schemas, the security and
governance documents, agent configuration files) also need the maintainer's
approval. The project's own automation merges its own changes when CI is green;
dependency bots merge under their own policy. Neither counts toward a human
quorum, and neither self-merges a change whose paths carry `sandbox`,
`security`, `audit` or `auth`, or sit under `.github/`, `schemas/` or `proto/`
- those wait for the maintainer's approval like anyone else's. That carve-out
is what the `quorum` check applies today; the charter's automation row does
not carry it yet, and #5740 proposes it for section 1.

## Reviewer checklist

An approval says "I read the whole diff and would defend it". Before approving:

1. Pull the branch for any non-trivial change and run the affected tests.
2. Check that new public surface (CLI flags, MCP tools, HTTP routes, adapter
   contracts) has tests and docs in the same pull request.
3. Flag any new dependency, new outbound network call, or new credential read
   in the conversation before approving.
4. Leave at least one line-level comment on a change over about 40 lines: a
   finding, or a note of what you ran. A review that says only "looks good"
   still counts as an approval; the comment is what makes it reviewable by
   anyone reading the thread later.

Item 4 is the one rule on this page that neither the charter nor the check
carries. It is proposed for charter section 5 in #5746 with the same
threshold and the same shape; until that lands, read it as guidance rather
than a merge condition.

## Escalation

A dispute, one approval against one *changes requested*, stays open until the
requester is satisfied or seven days pass; then anyone on the thread applies
`needs-maintainer` by hand. No script sets that label today, so nothing marks
the thread if nobody does. Disagreements on security-sensitive changes are resolved by
the maintainer, and when the outcome settles a boundary it is recorded in
`docs/decisions/`.
