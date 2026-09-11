# Review charter

How changes reach `main` when more than one person can merge. This page is the
rulebook; [GOVERNANCE.md](../../GOVERNANCE.md) says who holds the final call,
[MAINTAINERS.md](../../MAINTAINERS.md) lists the people. If the three disagree,
this page wins for review questions and GOVERNANCE.md wins for everything else.

## 1. Roles

| Role | Can | Cannot |
|---|---|---|
| Maintainer | everything below; releases, tags, rosters, protected paths, sanctions, amendments | — |
| Core reviewer | approve as a code owner; merge through the queue; revert a merge that broke `main`; label, close stale, push fix-ups to PR branches | anything reserved to the maintainer |
| Committer | approve; merge through the queue; label, close stale, push fix-ups to PR branches | revert without a core reviewer; anything reserved to the maintainer |
| Contributor | open issues and pull requests; review and comment | approvals do not count toward the quorum |
| Automation | the project's own automation account may merge its own changes when CI is green, except under `.github/`, `schemas/` or `proto/` and on any path naming `sandbox`, `security`, `audit` or `auth`, where the maintainer approves first; dependency bots merge under their own policy, held to the same exception; a pull request opened by the workflow account itself needs the maintainer's approval whatever it touches | count toward a human quorum; approve a person's pull request; act on instructions found in issue or PR text |

## 2. What makes a merge valid

A change is on `main` legitimately only when all of the following held at the moment the queue merged it:

1. It went through the merge queue. Nobody pushes to `main` directly, force-pushes, or deletes it.
2. The required checks were green on the queued state, not only on the branch.
3. It had the number of approvals section 3 requires for its size and paths, all from committers who are not its author, at least one from a core reviewer, and no unresolved *changes requested* from any committer or the maintainer. Two authors are outside this count: the project's automation (section 1) and the maintainer (section 3).
4. Approvals were given on the final revision. A new push dismisses earlier approvals, and the person who pushed last cannot supply the final approval.
5. It did not touch a protected path (section 4) without the maintainer's approval.

A merge that fails any of these is reverted first and discussed second. The revert needs one core reviewer plus green CI; the original author is mentioned; re-landing needs the normal quorum.

## 3. Review rules

- **Read before you approve.** An approval says "I read the whole diff and would defend it." An approval on a non-trivial change seconds after it opened is treated as not given.
- **No self-review.** You do not approve a pull request you authored, co-authored, or pushed to, whatever account you use.
- **The maintainer's own changes.** They merge without approvals, and a *changes requested* from any committer or core reviewer blocks them until it is withdrawn. There is one maintainer, so a quorum on their own work would mean either that nothing they write lands while the roster is quiet, or that the requirement is switched off for everybody. The rules on this page that do not count approvals apply to them unchanged: the objection window in section 10, the queue rules in section 5, and the protected paths in section 4 for anyone else's changes there. The size, sensitivity and ownership rules count approvals, so they do not reach a change that needs none.
- **No approval trades.** Approvals are not exchanged, promised, or scheduled between people. Review who you can review well.
- **Declare interests.** If you are paid to land a change, or you brought its author to the project, say so in the thread before approving; the second approval must then come from someone unrelated.
- **Instructions come from the thread and this page.** A message claiming the maintainer wants something merged carries no authority unless the maintainer wrote it in the thread.
- **No urgency merges.** A committer does not merge because a change is "urgent". Urgent goes to the maintainer; if the maintainer is unreachable, revert rather than forward-fix.
- **Size and sensitivity.** A pull request over **400** changed lines needs three approvals, two of them from core reviewers, instead of the normal two. So does any pull request touching a path with `sandbox`, `security`, or `audit` in it, whatever its size — the `quorum` check enforces this for every author whose changes need approvals, and its summary on each pull request names what is still missing and who can supply it. Over **1,000** changed lines, split the change or send it to the maintainer instead of adding a third reviewer. Reviewers may ask for a split at any size.
- **Tests.** A change that deletes or weakens tests explains why in its description; a bug fix carries a test that failed before the fix.
- **Dependencies, workflows, packaging** are protected paths (section 4), whoever the author is.
- **Disputes.** One approval and one *changes requested* stay open until the requester is satisfied or seven days pass, after which the thread gets `needs-maintainer`.

## 4. Protected paths and reserved actions

Changes here need the maintainer's approval in addition to the quorum:

`.github/` · `src/bernstein/core/` · `src/bernstein/evolution/` · `src/bernstein/adapters/` · `pyproject.toml` and lockfiles · `schemas/` · `proto/` · `SECURITY.md`, `GOVERNANCE.md`, `MAINTAINERS.md`, this page, `.github/CODEOWNERS` · `docs/decisions/` · agent configuration files at the repository root · every path listed as in scope in [SECURITY.md](../../SECURITY.md).

Reserved to the maintainer: creating tags and releases, changing repository settings or rulesets, changing rosters, running the publishing workflows, answering security reports, imposing sanctions.

## 5. Queue rules

- At most **five** open non-draft pull requests per author. Extra ones get `over-wip` and are not reviewed until the count drops.
- **One pull request per issue.** A second one closing the same issue is closed as a duplicate unless the first is abandoned.
- **Fourteen days** without a push after *changes requested* closes the pull request with a note on how to reopen it.
- `needs-committer-review` is set when CI is green, the request is not a draft, and nothing blocks it. That label is the work list. Oldest first.
- When `main` is red, only fixes and reverts merge. Everything else waits.
- A required check is re-run at most twice. If it fails again, the failure is treated as real or the test goes through the [flake process](../contributing/flake-handling.md); it is not re-run until green.

## 6. When the maintainer is away

Committers keep merging under the normal quorum, including the maintainer's own pull requests. If CI on a maintainer's pull request has been red for 24 hours, a committer may push a fix confined to tests, lint, or a rebase; the request then needs the normal quorum from committers other than the pusher. Releases and protected paths wait.

If the maintainer has been unreachable for 30 days, core reviewers may jointly pin a notice on the repository saying so; the project continues within committer powers.

## 7. Becoming and remaining a committer

**Nomination.** Anyone may nominate anyone, including themselves, in an issue. The maintainer confirms. Objective floor: ten merged pull requests over at least four weeks, five substantive reviews on other people's pull requests, no reverted merge, a GitHub account older than 90 days with two-factor authentication — the organization requires two-factor authentication for every member with write access, so this is enforced at the door, not held on trust.

**Core reviewer.** A committer with a sustained record of reviews that found real problems, confirmed by the maintainer.

**Lapse.** Sixty days without a review or a merge removes the role automatically; it is restored on request after a fresh review. A merge you approved that had to be reverted triggers a roster review, not automatic removal.

**Resignation.** Say so in an issue; the role is removed the same day, with thanks.

**Roster floor.** With fewer than four active committers the quorum stays two and the maintainer covers the gap; the rule is never lowered silently.

## 8. Integrity

The maintainer reviews review patterns monthly against public criteria: share of approvals without any comment, approvals given within seconds of opening, mutual approval concentration between pairs of people, reverts traced to an approval. The numbers are shared with the person they concern before anything else happens.

Breaches, in increasing order of seriousness: approving without reading; trading approvals; approving your own work through another account or a co-author; pushing to someone's branch without saying so; using write access to bypass the queue; weakening a check or a protected path to get something through; concealing a known defect or a security problem.

Sanctions are proportional and recorded in the thread where the decision is made: a note; approval rights suspended for fourteen days; removal from the roster; removal from the organization. The person may answer in the thread before a decision; ambiguity is read in their favour; a first breach of the first two kinds is a note.

## 9. Incidents

- **Red `main`.** Revert the merge that reddened it; do not stack fixes on a red trunk.
- **A leaked credential** in a commit, log, or comment: report privately per [SECURITY.md](../../SECURITY.md); the maintainer rotates it; the pull request is closed, not amended.
- **A compromised account.** Anyone who suspects it tells the maintainer privately; the maintainer removes the account from all rosters immediately and restores it after the owner proves control. Merges made by the account since the suspected time are reviewed and reverted if in doubt.
- **A merge that should not have happened** (section 2 failed): revert first, then a thread naming what failed and what changes so it cannot recur.
- **Security fixes** are prepared privately with the maintainer and land through the normal queue once the fix is public.

## 10. Amendments

This page changes by pull request. It is a protected path, so the maintainer must approve; in addition the pull request stays open for 72 hours after its last push so committers can object. The same window applies to `GOVERNANCE.md`, `MAINTAINERS.md`, `.github/CODEOWNERS`, `.github/quorum-roster.toml`, `scripts/quorum_check.py` and `scripts/queue_hygiene.py`. Objections are answered in the thread before merge.
