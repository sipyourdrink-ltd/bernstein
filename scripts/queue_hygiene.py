#!/usr/bin/env python3
"""Apply the review charter's queue-hygiene rules to open pull requests.

Five rules, each independent of the others:

``over-wip``
    An author with more than five open, non-draft pull requests gets the
    label on every one past their oldest five. The oldest five are never
    labelled, however many total the author has open.

``duplicate``
    A second (or later) open pull request that closes the same issue as an
    earlier one gets the label plus a comment pointing at the earlier PR.
    The earliest PR against a given issue is never the one labelled.

``needs-committer-review``
    Set when a pull request is not a draft, every check in its status
    rollup succeeded (or was neutral/skipped — nothing pending or failed),
    GitHub has confirmed it is mergeable (an unresolved ``UNKNOWN`` status is
    treated as not ready, not as vacuously clear), and nobody has requested
    changes. Removed the moment any of that stops being true.

``approval-shape``
    The charter (section 3) says an approval on a non-trivial change means
    "I read the whole diff and would defend it". A standing approval on a
    pull request over 40 changed lines whose author left no line-level
    comment anywhere on the request is dismissed, with a note on how to
    re-approve. Applies to every human approver except the maintainer,
    whose approval on a protected path is a separate requirement; bot
    reviews are never counted as approvals in the first place.

``changes-requested timeout``
    A pull request whose most recent review is "changes requested", with
    no commit pushed since, for fourteen days or more, is closed with a
    comment on how to reopen it. Exempt labels: pinned, do-not-close,
    work-in-progress — the same convention stale.yml already uses.

Dry-run by default: every rule always computes and prints what it WOULD do
for every open pull request. Nothing is labelled, commented on, or closed
unless ``--apply`` is passed. The workflow that calls this script only
passes ``--apply`` when the repository variable QUEUE_HYGIENE_ARMED is
exactly "true" — see .github/workflows/pr-queue-hygiene.yml. Run it by
hand first:

    python scripts/queue_hygiene.py                    # dry run, all PRs
    python scripts/queue_hygiene.py --apply             # live
    python scripts/queue_hygiene.py --pr 5701            # one PR, dry run

This does not touch area or size labels (.github/workflows/pr-labels.yml
already owns those) and does not touch the sixty-day generic inactivity
sweep (.github/workflows/stale.yml already owns that) — this script is
specifically the five rules above, no more.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_REPO = "sipyourdrink-ltd/bernstein"
WIP_CAP = 5
CHANGES_REQUESTED_TIMEOUT_DAYS = 14
EXEMPT_LABELS = {"pinned", "do-not-close", "work-in-progress"}
OVER_WIP_LABEL = "over-wip"
DUPLICATE_LABEL = "duplicate"
NEEDS_REVIEW_LABEL = "needs-committer-review"

# approval-shape: below this many changed lines an approval may reasonably
# carry no line comment (a one-line fix, a version bump).
APPROVAL_SHAPE_MIN_CHANGED_LINES = 40
# Approvals already standing before this rule is armed must not all be
# dismissed on the first run - only approvals given at or after this
# timestamp are ever in scope.
APPROVAL_SHAPE_EFFECTIVE_FROM = "2026-09-10T00:00:00Z"
# The maintainer's approval is a protected-path requirement in its own right
# (charter, section 4), not a quorum vote, so it is not held to the shape rule.
APPROVAL_SHAPE_EXEMPT_LOGINS = frozenset({"chernistry"})
DISMISSAL_MESSAGE = (
    "Dismissed by queue hygiene: the review charter "
    "(docs/governance/review-charter.md, section 3) asks an approval on a "
    "change of this size to carry at least one line-level comment, a finding "
    "or a note of what you ran. Re-approve with one and it stands."
)

# GitHub's own closing-keyword set (case-insensitive), singular or plural,
# with or without a colon, per
# https://docs.github.com/en/issues/tracking-your-work-with-issues/linking-a-pull-request-to-an-issue
_CLOSES_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d+)",
    re.IGNORECASE,
)


def gh_json(*args: str) -> Any:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def gh(*args: str) -> None:
    subprocess.run(["gh", *args], check=True, capture_output=True, text=True)


@dataclass
class PullRequest:
    number: int
    title: str
    author: str
    created_at: datetime
    is_draft: bool
    labels: set[str]
    review_decision: str
    body: str
    checks_pass: bool
    mergeable: str = ""  # MERGEABLE / CONFLICTING / UNKNOWN as GitHub reports it
    changed_lines: int = 0  # additions + deletions
    intents: list[str] = field(default_factory=list)


def parse_pr(raw: dict[str, Any]) -> PullRequest:
    return PullRequest(
        number=raw["number"],
        title=raw["title"],
        author=(raw.get("author") or {}).get("login", "unknown"),
        created_at=datetime.fromisoformat(raw["createdAt"].replace("Z", "+00:00")),
        is_draft=raw["isDraft"],
        labels={label["name"] for label in raw.get("labels", [])},
        review_decision=raw.get("reviewDecision") or "",
        body=raw.get("body") or "",
        checks_pass=False,  # filled in by required_checks_pass() below
        mergeable=raw.get("mergeable") or "",
        changed_lines=int(raw.get("additions") or 0) + int(raw.get("deletions") or 0),
    )


def required_checks_pass(repo: str, pr_number: int) -> bool:
    """True only when every REQUIRED check (not every job - this repo runs
    27+ jobs and only two are required contexts) has reported and passed.

    Deliberately per-PR rather than pulling statusCheckRollup in the bulk
    `gh pr list` call: with ~90 open PRs and this many workflow jobs, the
    rollup field on the list query is expensive enough on GitHub's side to
    time out (HTTP 504) - confirmed against the live repo while building
    this script, not a hypothetical. `gh pr checks --required` costs one
    call per PR instead, which is slower but does not fall over.
    """
    checks = gh_json(
        "pr",
        "checks",
        str(pr_number),
        "--repo",
        repo,
        "--required",
        "--json",
        "bucket,name",
    )
    if not checks:
        # Nothing required has reported yet - not the same as "nothing
        # required exists". Treat as not ready rather than vacuously ready.
        return False
    return all(c.get("bucket") == "pass" for c in checks)


def fetch_open_prs(repo: str) -> list[PullRequest]:
    raw = gh_json(
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        "300",
        "--json",
        "number,title,author,createdAt,isDraft,labels,reviewDecision,body,mergeable,additions,deletions",
    )
    prs = [parse_pr(r) for r in raw]
    for pr in prs:
        if pr.is_draft:
            continue  # drafts never need needs-committer-review; skip the call
        try:
            pr.checks_pass = required_checks_pass(repo, pr.number)
        except subprocess.CalledProcessError as exc:
            print(
                f"warning: could not read required checks for #{pr.number}: "
                f"{exc.stderr.strip() if exc.stderr else exc}",
                file=sys.stderr,
            )
            pr.checks_pass = False
    return prs


def rule_over_wip(prs: list[PullRequest]) -> None:
    by_author: dict[str, list[PullRequest]] = {}
    for pr in prs:
        if pr.is_draft:
            continue
        by_author.setdefault(pr.author, []).append(pr)
    for author, authored in by_author.items():
        authored.sort(key=lambda p: p.created_at)
        for pr in authored[:WIP_CAP]:
            if OVER_WIP_LABEL in pr.labels:
                pr.intents.append(f"remove:{OVER_WIP_LABEL} (within cap)")
        for pr in authored[WIP_CAP:]:
            if OVER_WIP_LABEL not in pr.labels:
                pr.intents.append(
                    f"add:{OVER_WIP_LABEL} ({author} has {len(authored)} open, this is past the oldest {WIP_CAP})"
                )


def rule_duplicate(prs: list[PullRequest]) -> None:
    by_issue: dict[int, list[PullRequest]] = {}
    for pr in prs:
        for m in _CLOSES_RE.finditer(pr.body):
            by_issue.setdefault(int(m.group(1)), []).append(pr)
    for issue_number, referring in by_issue.items():
        if len(referring) < 2:
            continue
        deduped = {p.number: p for p in referring}
        referring = sorted(deduped.values(), key=lambda p: p.created_at)
        if len(referring) < 2:
            continue
        first, rest = referring[0], referring[1:]
        for pr in rest:
            if DUPLICATE_LABEL not in pr.labels:
                pr.intents.append(
                    f"add:{DUPLICATE_LABEL} + comment (closes #{issue_number}, same as #{first.number} opened first)"
                )


def rule_needs_committer_review(prs: list[PullRequest]) -> None:
    for pr in prs:
        should_have = (
            not pr.is_draft
            and pr.checks_pass
            and pr.mergeable == "MERGEABLE"
            and pr.review_decision != "CHANGES_REQUESTED"
        )
        has = NEEDS_REVIEW_LABEL in pr.labels
        if should_have and not has:
            pr.intents.append(f"add:{NEEDS_REVIEW_LABEL}")
        elif has and not should_have:
            pr.intents.append(f"remove:{NEEDS_REVIEW_LABEL}")


def _is_bot(review: dict[str, Any]) -> bool:
    """Ask the type, never the spelling. REST carries ``user.type == "Bot"``;
    GraphQL returns an app's login with no ``[bot]`` suffix, which is why a
    suffix-only check is fragile - this endpoint is REST, so the reliable
    field is free."""
    user = review.get("user") or {}
    return user.get("type") == "Bot" or (user.get("login") or "").endswith("[bot]")


def standing_approvals(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The approvals GitHub still counts: each user's latest verdict, where a
    comment-only review does not replace an earlier verdict (GitHub keeps
    the approval standing through later COMMENTED reviews) but a later
    CHANGES_REQUESTED, DISMISSED or fresh APPROVED does."""
    latest: dict[str, dict[str, Any]] = {}
    for review in reviews:
        login = (review.get("user") or {}).get("login") or ""
        if not login or review.get("state") in ("COMMENTED", "PENDING"):
            continue
        prev = latest.get(login)
        if prev is None or (review.get("submitted_at") or "") >= (prev.get("submitted_at") or ""):
            latest[login] = review
    return [r for r in latest.values() if r.get("state") == "APPROVED"]


def _fetch_reviews(repo: str, pr_number: int, cache: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Reviews for one pull request, fetched once and shared - approval-shape
    and the changes-requested timeout both need the same endpoint, so a PR
    that qualifies for both must not pay for it twice in one sweep."""
    if pr_number not in cache:
        cache[pr_number] = gh_json("api", f"repos/{repo}/pulls/{pr_number}/reviews", "--paginate")
    return cache[pr_number]


def rule_approval_shape(
    repo: str, prs: list[PullRequest], reviews_cache: dict[int, list[dict[str, Any]]] | None = None
) -> None:
    cache = reviews_cache if reviews_cache is not None else {}
    for pr in prs:
        if pr.is_draft or pr.changed_lines <= APPROVAL_SHAPE_MIN_CHANGED_LINES:
            continue
        # Same exemption the changes-requested timeout already honors: pinned,
        # do-not-close and work-in-progress protect a PR from an automated
        # dismissal just as they protect it from an automated close.
        if pr.labels & EXEMPT_LABELS:
            continue
        reviews = _fetch_reviews(repo, pr.number, cache)
        # Once-only: dismissing a review sets its own body to DISMISSAL_MESSAGE,
        # so a login that already carries a DISMISSED review with that body has
        # had its one warning. A bare re-approval from the same person is a new
        # review id and would otherwise be re-dismissed every run.
        already_dismissed_logins = {
            (r.get("user") or {}).get("login")
            for r in reviews
            if r.get("state") == "DISMISSED" and (r.get("body") or "") == DISMISSAL_MESSAGE
        }
        approvals = [
            r
            for r in standing_approvals(reviews)
            if r["user"]["login"] not in APPROVAL_SHAPE_EXEMPT_LOGINS
            and not _is_bot(r)
            and r["user"]["login"] not in already_dismissed_logins
        ]
        if not approvals:
            continue
        # Any line comment by the approver anywhere on the request counts,
        # whichever review it was attached to: a reviewer who approves and
        # then adds a line note in a separate comment has still read it.
        comments = gh_json("api", f"repos/{repo}/pulls/{pr.number}/comments", "--paginate")
        commented_by = {(c.get("user") or {}).get("login") for c in comments}
        for review in approvals:
            if (review.get("submitted_at") or "") < APPROVAL_SHAPE_EFFECTIVE_FROM:
                continue
            login = review["user"]["login"]
            if login in commented_by or (review.get("body") or "").strip():
                continue
            pr.intents.append(
                f"dismiss:{review['id']} ({login}: approval without a line comment on {pr.changed_lines} changed lines)"
            )


def last_changes_requested_without_push(
    repo: str, pr: PullRequest, reviews_cache: dict[int, list[dict[str, Any]]] | None = None
) -> datetime | None:
    """Return the timestamp of the most recent 'changes requested' review
    if no commit has landed since, else None."""
    cache = reviews_cache if reviews_cache is not None else {}
    reviews = _fetch_reviews(repo, pr.number, cache)
    changes_requested_at: datetime | None = None
    for r in reviews:
        if r.get("state") == "CHANGES_REQUESTED":
            ts = datetime.fromisoformat(r["submitted_at"].replace("Z", "+00:00"))
            if changes_requested_at is None or ts > changes_requested_at:
                changes_requested_at = ts
        elif r.get("state") == "APPROVED":
            # An approval after the last changes-requested does not clear
            # it by itself (the rule is about a *push*), but a later
            # changes-requested from someone else supersedes an earlier one
            # regardless - handled by the max() above since we scan all.
            pass
    if changes_requested_at is None:
        return None
    commits = gh_json("api", f"repos/{repo}/pulls/{pr.number}/commits", "--paginate")
    for c in commits:
        date_str = c.get("commit", {}).get("committer", {}).get("date")
        if not date_str:
            continue
        pushed_at = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if pushed_at > changes_requested_at:
            return None  # a push happened after the request - timer reset
    return changes_requested_at


def rule_changes_requested_timeout(
    repo: str, prs: list[PullRequest], reviews_cache: dict[int, list[dict[str, Any]]] | None = None
) -> None:
    cache = reviews_cache if reviews_cache is not None else {}
    now = datetime.now(UTC)
    for pr in prs:
        if pr.review_decision != "CHANGES_REQUESTED":
            continue
        if pr.labels & EXEMPT_LABELS:
            continue
        since = last_changes_requested_without_push(repo, pr, cache)
        if since is None:
            continue
        age = now - since
        if age >= timedelta(days=CHANGES_REQUESTED_TIMEOUT_DAYS):
            pr.intents.append(f"close ({age.days}d since changes-requested, no push)")


# (name, color, description) for every label this script can apply that
# does not already exist in the repo today (checked against the live
# label list while building this script - `duplicate` already exists,
# these five do not). Created idempotently on the first --apply run, not
# by anyone running this PR's setup by hand, and not during a dry run.
_LABELS_TO_ENSURE = (
    (OVER_WIP_LABEL, "d4c5f9", "More than 5 open PRs by this author - past the oldest 5"),
    (NEEDS_REVIEW_LABEL, "0e8a16", "CI green, not draft, nothing blocking - ready for a committer"),
    # stale.yml already lists these three as exempt-pr-labels; they were
    # never created, so that exemption has been a silent no-op. Creating
    # them here makes stale.yml's existing config do what it already says.
    ("pinned", "b60205", "Exempt from stale/queue-hygiene auto-close"),
    ("do-not-close", "b60205", "Exempt from stale/queue-hygiene auto-close"),
    ("work-in-progress", "fbca04", "Exempt from stale/queue-hygiene auto-close"),
)


def ensure_labels(repo: str) -> None:
    existing = set(gh_json("label", "list", "--repo", repo, "--limit", "300", "--json", "name"))
    existing_names = {e["name"] for e in existing} if existing and isinstance(existing, list) else set()
    for name, color, description in _LABELS_TO_ENSURE:
        if name in existing_names:
            continue
        try:
            gh(
                "label",
                "create",
                name,
                "--repo",
                repo,
                "--color",
                color,
                "--description",
                description,
            )
            print(f"created missing label: {name}")
        except subprocess.CalledProcessError as exc:
            # Idempotent by design: a race with another run creating the
            # same label between our list and our create is fine to ignore.
            print(
                f"warning: could not create label {name!r}: {exc.stderr.strip() if exc.stderr else exc}",
                file=sys.stderr,
            )


def apply_intents(repo: str, pr: PullRequest) -> None:
    for intent in pr.intents:
        if intent.startswith(f"add:{OVER_WIP_LABEL}"):
            gh("pr", "edit", str(pr.number), "--repo", repo, "--add-label", OVER_WIP_LABEL)
        elif intent.startswith(f"remove:{OVER_WIP_LABEL}"):
            gh("pr", "edit", str(pr.number), "--repo", repo, "--remove-label", OVER_WIP_LABEL)
        elif intent.startswith(f"add:{DUPLICATE_LABEL}"):
            gh("pr", "edit", str(pr.number), "--repo", repo, "--add-label", DUPLICATE_LABEL)
            issue_ref = intent.split("closes #", 1)[1].split(",", 1)[0]
            first_ref = intent.rsplit("#", 1)[1].rstrip(")")
            gh(
                "pr",
                "comment",
                str(pr.number),
                "--repo",
                repo,
                "--body",
                f"This closes the same issue (#{issue_ref}) as #{first_ref}, "
                "which was opened first. Marking as a duplicate per the "
                "review charter's queue rules "
                "(docs/governance/review-charter.md#5-queue-rules). If #"
                f"{first_ref} is abandoned, say so here and a committer will "
                "remove the label.",
            )
        elif intent.startswith(f"add:{NEEDS_REVIEW_LABEL}"):
            gh("pr", "edit", str(pr.number), "--repo", repo, "--add-label", NEEDS_REVIEW_LABEL)
        elif intent.startswith(f"remove:{NEEDS_REVIEW_LABEL}"):
            gh("pr", "edit", str(pr.number), "--repo", repo, "--remove-label", NEEDS_REVIEW_LABEL)
        elif intent.startswith("dismiss:"):
            review_id = intent.split(":", 1)[1].split(" ", 1)[0]
            try:
                gh(
                    "api",
                    "-X",
                    "PUT",
                    f"repos/{repo}/pulls/{pr.number}/reviews/{review_id}/dismissals",
                    "-f",
                    f"message={DISMISSAL_MESSAGE}",
                )
            except subprocess.CalledProcessError as exc:
                # A dismissal can individually fail - branch protection
                # restricting who may dismiss reviews, or one someone already
                # dismissed by hand - and PRs are processed in order, so one
                # bad review must not block every intent queued after it, the
                # same as ensure_labels and fetch_open_prs already treat their
                # own per-item calls.
                print(
                    f"warning: could not dismiss review {review_id} on #{pr.number}: "
                    f"{exc.stderr.strip() if exc.stderr else exc}",
                    file=sys.stderr,
                )
        elif intent.startswith("close ("):
            gh(
                "pr",
                "close",
                str(pr.number),
                "--repo",
                repo,
                "--comment",
                "Closing per the review charter's queue rules: changes were "
                "requested and there has been no push for "
                f"{CHANGES_REQUESTED_TIMEOUT_DAYS} days "
                "(docs/governance/review-charter.md#5-queue-rules). Push a "
                "new commit and comment here to ask a committer to reopen.",
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--pr", type=int, default=None, help="limit to one PR number")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually label/comment/close instead of only printing intent",
    )
    args = parser.parse_args(argv)

    if args.apply:
        ensure_labels(args.repo)

    # Every rule runs over the FULL open-PR set, always - over-wip and
    # duplicate are inherently cross-PR (an author's total count, a second
    # PR against the same issue) and give a wrong, weaker answer if the
    # input is pre-filtered to one PR. --pr only narrows what gets
    # printed/acted on afterward, never what the rules can see.
    prs = fetch_open_prs(args.repo)

    # Shared across the two rules that both read pull-request reviews, so a
    # PR that qualifies for both is not fetched twice in the same run.
    reviews_cache: dict[int, list[dict[str, Any]]] = {}

    rule_over_wip(prs)
    rule_duplicate(prs)
    rule_needs_committer_review(prs)
    rule_approval_shape(args.repo, prs, reviews_cache)
    rule_changes_requested_timeout(args.repo, prs, reviews_cache)

    if args.pr is not None:
        prs = [p for p in prs if p.number == args.pr]
        if not prs:
            print(f"PR #{args.pr} not found among open PRs.", file=sys.stderr)
            return 2

    acted = 0
    for pr in sorted(prs, key=lambda p: p.number):
        if not pr.intents:
            continue
        acted += 1
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] #{pr.number} ({pr.author}) {pr.title!r}:")
        for intent in pr.intents:
            print(f"    {intent}")
        if args.apply:
            apply_intents(args.repo, pr)

    print(f"\n{acted}/{len(prs)} open pull requests have an intent this run.")
    if not args.apply:
        print("Dry run only - nothing was changed. Pass --apply to act.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
