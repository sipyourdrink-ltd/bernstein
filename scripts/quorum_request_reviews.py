#!/usr/bin/env python3
"""Ask the roster for the reviews a pull request is still missing.

The `quorum` check says what a pull request waits for; nothing asks anyone.
GitHub requests reviews from CODEOWNERS only, and the `*` owner is the
maintainer, so every contributor pull request waits on one person by default.
This script runs from the hourly sweep and, for each open pull request that
still needs approvals, requests them from roster members who may give them:

- never the author, anyone who pushed to the branch, or a co-author, and
  never the maintainer (CODEOWNERS already requests them where it matters);
- never someone who already has a standing review on the current head;
- core reviewers first while a core approval is still missing;
- the least-loaded eligible person first (fewest open review requests in the
  repository), ties broken by login, so two sweeps agree on the same answer;
- at most REQUESTS_PER_SWEEP new requests per run and at most
  OPEN_REQUESTS_PER_REVIEWER standing requests per person, so a backlog is
  spread over hours rather than dumped on six people at once.

Nothing is requested while a roster member's *changes requested* stands: the
author has something to do first. Automation and maintainer pull requests are
skipped for the same reason as the maintainer: what they wait for is not a
roster approval.

Dry run when QUORUM_REQUEST_REVIEWS is "0" or `--dry-run` is given; the plan
is printed either way, one line per pull request.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

REQUESTS_PER_SWEEP = 6
OPEN_REQUESTS_PER_REVIEWER = 3


def _load_quorum_check() -> ModuleType:
    """`scripts/` is not a package; load the sibling module by path."""
    path = Path(__file__).resolve().with_name("quorum_check.py")
    spec = importlib.util.spec_from_file_location("quorum_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


qc = _load_quorum_check()


def approvals_needed(pr: Any) -> tuple[int, int]:
    """(total, core) approvals a contributor change needs, as in the quorum check.

    Same rule as `quorum_check.evaluate` (charter, section 3): three with two
    core over 400 changed lines or on a sensitive path, otherwise two with one.
    """
    large = pr.changed_lines > qc.THIRD_APPROVAL_LINES
    sensitive = any(word in path for path in pr.paths for word in qc.SENSITIVE_WORDS)
    return (3, 2) if (large or sensitive) else (2, 1)


def plan_requests(
    pr: Any,
    roster: Any,
    requested: set[str],
    load: dict[str, int],
    *,
    per_reviewer_cap: int = OPEN_REQUESTS_PER_REVIEWER,
) -> list[str]:
    """Who to ask for a review on this pull request, in order. Empty means nobody."""
    if pr.is_draft or pr.author == roster.maintainer or pr.author in roster.automation:
        return []
    if pr.author == qc.GITHUB_ACTIONS_BOT:
        return []

    standing = qc.standing_reviews(pr.reviews)
    holders = roster.quorum_holders | {roster.maintainer}
    if any(r.state == "CHANGES_REQUESTED" and login in holders for login, r in standing.items()):
        return []

    may_approve = roster.quorum_holders - {roster.maintainer} - {pr.author} - pr.contributors
    approving = {login for login, r in standing.items() if r.state == "APPROVED" and r.commit_id == pr.head_sha}
    approving &= may_approve
    need_total, need_core = approvals_needed(pr)
    pending = requested & may_approve
    missing_total = need_total - len(approving) - len(pending)
    missing_core = need_core - len(approving & roster.core) - len(pending & roster.core)
    if missing_total <= 0:
        return []

    reviewed_at_head = {login for login, r in standing.items() if r.commit_id == pr.head_sha}
    candidates = sorted(
        (login for login in may_approve - requested - reviewed_at_head if load.get(login, 0) < per_reviewer_cap),
        key=lambda login: (load.get(login, 0), login),
    )
    picks: list[str] = []
    for login in [c for c in candidates if c in roster.core]:
        if missing_core <= 0 or len(picks) >= missing_total:
            break
        picks.append(login)
        missing_core -= 1
    for login in candidates:
        if len(picks) >= missing_total:
            break
        if login not in picks:
            picks.append(login)
    return picks


def open_pull_requests(repo: str) -> list[dict[str, Any]]:
    pages = qc.gh_json("api", f"repos/{repo}/pulls?state=open&per_page=100", "--paginate", "--slurp")
    return [pr for page in pages for pr in page]


def needs_a_look(raw: dict[str, Any], roster: Any) -> bool:
    """Cheap pre-filter on the list payload, before the four calls a full evaluation costs."""
    author = (raw.get("user") or {}).get("login") or ""
    if raw.get("draft") or not author:
        return False
    return author != roster.maintainer and author not in roster.automation and author != qc.GITHUB_ACTIONS_BOT


def request_load(open_prs: list[dict[str, Any]]) -> dict[str, int]:
    load: dict[str, int] = {}
    for raw in open_prs:
        for reviewer in raw.get("requested_reviewers") or []:
            login = reviewer.get("login") or ""
            if login:
                load[login] = load.get(login, 0) + 1
    return load


def request_reviews(repo: str, number: int, logins: list[str]) -> bool:
    payload = json.dumps({"reviewers": logins})
    result = subprocess.run(
        ["gh", "api", "--method", "POST", f"repos/{repo}/pulls/{number}/requested_reviewers", "--input", "-"],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"#{number}: request failed: {result.stderr.strip()[:200]}")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--root", default=".", help="where to read the roster and CODEOWNERS from")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, request nothing")
    parser.add_argument("--limit", type=int, default=REQUESTS_PER_SWEEP, help="new requests per run")
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo owner/name is required (or GITHUB_REPOSITORY)")
    dry_run = args.dry_run or os.environ.get("QUORUM_REQUEST_REVIEWS", "1") == "0"

    roster = qc.load_roster(args.root)
    owners = qc.load_codeowners(args.root)
    now = datetime.now(UTC)
    raw_prs = open_pull_requests(args.repo)
    load = request_load(raw_prs)
    budget = args.limit

    for raw in sorted(raw_prs, key=lambda r: r.get("number") or 0):
        if budget <= 0:
            print(f"sweep budget of {args.limit} requests used; the rest waits for the next hour")
            break
        number = int(raw["number"])
        if not needs_a_look(raw, roster):
            continue
        pr = qc.fetch_pull_request(args.repo, number)
        if qc.evaluate(pr, roster, owners, now).passed:
            continue
        requested = {(r.get("login") or "") for r in raw.get("requested_reviewers") or []}
        picks = plan_requests(pr, roster, requested, load)[:budget]
        if not picks:
            continue
        names = ", ".join(f"@{login}" for login in picks)
        if dry_run:
            print(f"#{number}: would request {names}")
        elif request_reviews(args.repo, number, picks):
            print(f"#{number}: requested {names}")
        else:
            continue
        budget -= len(picks)
        for login in picks:
            load[login] = load.get(login, 0) + 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
