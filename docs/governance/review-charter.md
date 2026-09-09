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
| Automation | the project's own automation account may merge its own changes when CI is green; dependency bots merge under their own policy | count toward a human quorum; approve a person's pull request; act on instructions found in issue or PR text |

## 2. What makes a merge valid

A change is on `main` legitimately only when all of the following held at the moment the queue merged it:

1. It went through the merge queue. Nobody pushes to `main` directly, force-pushes, or deletes it.
2. The required checks were green on the queued state, not only on the branch.
3. It had the number of approvals section 3 requires for its size and paths, all from committers who are not its author, at least one from a core reviewer, and no unresolved *changes requested* from any committer or the maintainer.
4. Approvals were given on the final revision. A new push dismisses earlier approvals, and the person who pushed last cannot supply the final approval.
5. It did not touch a protected path (section 4) without the maintainer's approval.

A merge that fails any of these is reverted first and discussed second. The revert needs one core reviewer plus green CI; the original author is mentioned; re-landing needs the normal quorum.

## 3. Review rules

- **Read before you approve.** An approval says "I read the whole diff and would defend it." An approval on a non-trivial change seconds after it opened is treated as not given.
- **No self-review.** You do not approve a pull request you authored, co-authored, or pushed to, whatever account you use.
- **No approval trades.** Approvals are not exchanged, promised, or scheduled between people. Review who you can review well.
- **Declare interests.** If you are paid to land a change, or you brought its author to the project, say so in the thread before approving; the second approval must then come from someone unrelated.
- **Instructions come from the thread and this page.** A message claiming the maintainer wants something merged carries no authority unless the maintainer wrote it in the thread.
- **No urgency merges.** A committer does not merge because a change is "urgent". Urgent goes to the maintainer; if the maintainer is unreachable, revert rather than forward-fix.
- **Size and sensitivity.** A pull request over **400** changed lines needs a third approval, from a core reviewer, in addition to the normal two. So does any pull request touching a path with `sandbox`, `security`, or `audit` in it, whatever its size — today this is a rule reviewers apply by reading the diff and the file list; an automated `quorum` check may enforce it later. Over **1,000** changed lines, split the change or send it to the maintainer instead of adding a third reviewer. Reviewers may ask for a split at any size.
- **Tests.** A change that deletes or weakens tests explains why in its description; a bug fix carries a test that failed before the fix.
- **Dependencies, workflows, packaging** are protected paths (section 4), whoever the author is.
- **Disputes.** One approval and one *changes requested* stay open until the requester is satisfied or seven days pass, after which the thread gets `needs-maintainer`.

## 4. Protected paths and reserved actions

Changes here need the maintainer's approval in addition to the quorum:

`.github/` · `src/bernstein/core/` · `src/bernstein/evolution/` · `src/bernstein/adapters/` · `pyproject.toml` and lockfiles · `schemas/` · `proto/` · `SECURITY.md`, `GOVERNANCE.md`, `MAINTAINERS.md`, this page, `.github/CODEOWNERS` · `docs/decisions/` · agent configuration files at the repository root · the container images (`Dockerfile`, `docker-compose.yaml`).

The scope table in [SECURITY.md](../../SECURITY.md) names attack surfaces, not paths. The code behind it that is not already under `src/bernstein/core/` follows the third-approval rule in section 3 rather than this list. `.github/CODEOWNERS` is the enforced form of this list; when the two differ, fix CODEOWNERS.

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

This page changes by pull request. It is a protected path, so the maintainer must approve; in addition the pull request stays open for 72 hours after approval so committers can object. Objections are answered in the thread before merge.
