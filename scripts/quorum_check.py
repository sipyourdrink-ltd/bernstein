#!/usr/bin/env python3
"""Decide whether a pull request has the review the charter asks for.

`docs/governance/review-charter.md` section 3 says an automated `quorum`
check may enforce the review rules later; this is that check. It reports a
pass or a failure with a table naming what is missing and who can supply it.

Why a script and not the branch rule
------------------------------------
GitHub's "required approvals" rule applies the same number to everybody, and
its bypass list does not apply when a pull request enters the merge queue. So
the rule either blocks the maintainer's own infrastructure changes and every
change the project's automation makes, or it is switched off for everyone.
The charter does not say that: it exempts automation (section 1), it lets the
maintainer merge while committers keep their veto (section 6), and it asks
for MORE than two approvals on large or sensitive changes (section 3). All of
that fits in a check; none of it fits in the rule. The branch rule keeps the
parts GitHub does well - the queue, the required status checks, no force
pushes - and this check owns the review question.

What it reads, and from where
-----------------------------
The roster and CODEOWNERS come from the pull request's BASE branch, never
from the branch under review, so a pull request cannot promote its own author
or make itself unowned. The workflow checks out the base commit for the same
reason. Everything else (reviews, commits, files) comes from the API, which
reports the pull request as GitHub sees it.

The rules, in the order they are applied
----------------------------------------
0. A draft is neutral: the queue does not take drafts, so there is nothing to
   decide yet.
1. A change to a governance path is held open for 72 hours after its last
   push, so committers can object (section 10). This applies to every author,
   the maintainer included.
2. Automation merges its own changes on green CI (section 1), except on the
   paths where a mistake is expensive - anything with `sandbox`, `security`,
   `audit` or `auth` in it, plus `.github/`, `schemas/` and `proto/` - where
   it needs the maintainer's approval.
3. A pull request opened by `github-actions[bot]` needs the maintainer's
   approval whatever it touches: anyone who can push a branch can open one.
4. The maintainer's own pull requests merge without approvals, and any
   standing `changes requested` from a committer blocks them (section 6).
5. Everyone else needs the quorum: two approvals, at least one from a core
   reviewer, none of them from anyone who wrote or pushed the change. Over
   400 changed lines, or on a path containing `sandbox`, `security` or
   `audit`, a third approval is required and two of the three must be core.
   Over 1,000 changed lines the maintainer approves or the change is split.
   A protected path (any CODEOWNERS entry that is not `*`) needs its owner.

Approvals count only when they were given on the current head commit: a push
after an approval means nobody has read what is about to merge. A `changes
requested` survives a push, and stays counted until its author withdraws it.
A review whose state is `commented` does not replace an earlier verdict,
which is how GitHub itself treats it.

Exit codes: 0 pass or neutral, 1 fail. The failure table is written to the
job summary as well as stdout.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

ROSTER_PATH = ".github/quorum-roster.toml"
CODEOWNERS_PATH = ".github/CODEOWNERS"

# Section 3: over this many changed lines a third approval is required, and
# over the second number the change is split or goes to the maintainer.
THIRD_APPROVAL_LINES = 400
MAINTAINER_OR_SPLIT_LINES = 1000

# Section 3: these words in a path make a change sensitive whatever its size.
SENSITIVE_WORDS = ("sandbox", "security", "audit")

# Section 1 as decided for automation: the paths where automation stops being
# allowed to merge its own work. Wider than SENSITIVE_WORDS because a change
# to the workflows, the schemas or the wire protocol is not something the
# project's automation should be able to land unseen.
AUTOMATION_STOP_WORDS = (*SENSITIVE_WORDS, "auth")
AUTOMATION_STOP_PREFIXES = (".github/", "schemas/", "proto/")

# Section 10: an amendment stays open so committers can object. The charter
# anchors the window on the approval; for a change that needs no approval the
# equivalent anchor is the last push, which is what this check uses.
GOVERNANCE_WINDOW = timedelta(hours=72)
# Literal paths rather than a `.github/workflows/` prefix rule: a prefix would
# put every unrelated CI edit behind a three-day window, which is a cost the
# gate does not need to pay to protect itself. Both quorum workflows are here
# because both are invocation surface -- `quorum.yml` decides whether the check
# runs at all, and `quorum-rerun.yml` is the only thing that closes the window
# below, so a change silencing either one is a change to the gate.
GOVERNANCE_PATHS = (
    "docs/governance/review-charter.md",
    "GOVERNANCE.md",
    "MAINTAINERS.md",
    ".github/CODEOWNERS",
    ".github/quorum-roster.toml",
    ".github/workflows/quorum.yml",
    ".github/workflows/quorum-rerun.yml",
    "scripts/quorum_check.py",
    "scripts/queue_hygiene.py",
)

GITHUB_ACTIONS_BOT = "github-actions[bot]"


def gh_json(*args: str) -> Any:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@dataclass(frozen=True)
class Roster:
    maintainer: str
    core_reviewers: frozenset[str]
    committers: frozenset[str]
    automation: frozenset[str]

    @property
    def quorum_holders(self) -> frozenset[str]:
        """Everyone whose approval counts toward a human quorum."""
        return self.core_reviewers | self.committers | {self.maintainer}

    @property
    def core(self) -> frozenset[str]:
        """Everyone who can supply the core reviewer's approval."""
        return self.core_reviewers | {self.maintainer}


def load_roster(root: str = ".") -> Roster:
    with open(os.path.join(root, ROSTER_PATH), "rb") as handle:
        raw = tomllib.load(handle)
    return Roster(
        maintainer=raw["maintainer"],
        core_reviewers=frozenset(raw.get("core_reviewers", [])),
        committers=frozenset(raw.get("committers", [])),
        automation=frozenset(raw.get("automation", [])),
    )


def load_codeowners(root: str = ".") -> list[tuple[str, list[str]]]:
    """CODEOWNERS as (pattern, owners) in file order. Last match wins."""
    path = os.path.join(root, CODEOWNERS_PATH)
    if not os.path.isfile(path):
        return []
    entries: list[tuple[str, list[str]]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            pattern, *owners = line.split()
            if owners:
                entries.append((pattern, [o.lstrip("@") for o in owners]))
    return entries


def path_matches(pattern: str, path: str) -> bool:
    """CODEOWNERS matching, in the subset of the syntax this repository uses.

    `*` matches everything; a pattern ending in `/` matches everything under
    that directory; a leading `/` anchors at the repository root; anything
    else is matched as a glob against the whole path and against its
    basename, which is how GitHub treats an unanchored pattern.
    """
    if pattern == "*":
        return True
    anchored = pattern.startswith("/")
    cleaned = pattern.lstrip("/")
    if cleaned.endswith("/"):
        return path.startswith(cleaned) if anchored else f"/{path}".find(f"/{cleaned}") >= 0
    if anchored:
        return path == cleaned or fnmatch.fnmatch(path, cleaned)
    return fnmatch.fnmatch(path, cleaned) or fnmatch.fnmatch(os.path.basename(path), cleaned)


def owners_for(path: str, entries: list[tuple[str, list[str]]]) -> tuple[list[str], bool]:
    """Owners of a path and whether they come from a specific rule.

    The boolean says the path is protected in the sense of charter section 4:
    it matched a rule of its own rather than falling through to the catch-all.
    """
    owners: list[str] = []
    specific = False
    for pattern, entry_owners in entries:
        if path_matches(pattern, path):
            owners = entry_owners
            specific = pattern != "*"
    return owners, specific


@dataclass
class Review:
    login: str
    state: str
    commit_id: str
    submitted_at: str


@dataclass
class PullRequest:
    number: int
    author: str
    is_draft: bool
    head_sha: str
    changed_lines: int
    paths: list[str]
    reviews: list[Review]
    # Everyone who wrote or pushed any commit on the branch, plus anyone named
    # in a Co-authored-by trailer: none of them can approve it (section 3).
    contributors: set[str]
    last_push: datetime


def _logins_from_commit(raw: dict[str, Any]) -> set[str]:
    logins: set[str] = set()
    for role in ("author", "committer"):
        login = ((raw.get(role) or {}).get("login")) or ""
        if login:
            logins.add(login)
    message = (raw.get("commit") or {}).get("message") or ""
    for match in re.finditer(r"Co-authored-by:[^<]*<([^>]+)>", message, re.IGNORECASE):
        handle = match.group(1).split("@")[0]
        # `12345+login@users.noreply.github.com` is the form GitHub writes.
        logins.add(handle.split("+")[-1])
    return logins


def fetch_pull_request(repo: str, number: int) -> PullRequest:
    raw = gh_json("api", f"repos/{repo}/pulls/{number}")
    files = gh_json("api", f"repos/{repo}/pulls/{number}/files", "--paginate")
    raw_reviews = gh_json("api", f"repos/{repo}/pulls/{number}/reviews", "--paginate")
    commits = gh_json("api", f"repos/{repo}/pulls/{number}/commits", "--paginate")

    contributors: set[str] = set()
    pushed_at = None
    for commit in commits:
        contributors |= _logins_from_commit(commit)
        date = ((commit.get("commit") or {}).get("committer") or {}).get("date")
        if date:
            moment = datetime.fromisoformat(date.replace("Z", "+00:00"))
            pushed_at = moment if pushed_at is None or moment > pushed_at else pushed_at

    return PullRequest(
        number=number,
        author=(raw.get("user") or {}).get("login", ""),
        is_draft=bool(raw.get("draft")),
        head_sha=(raw.get("head") or {}).get("sha", ""),
        changed_lines=int(raw.get("additions") or 0) + int(raw.get("deletions") or 0),
        paths=[f["filename"] for f in files],
        reviews=[
            Review(
                login=(r.get("user") or {}).get("login", ""),
                state=r.get("state", ""),
                commit_id=r.get("commit_id") or "",
                submitted_at=r.get("submitted_at") or "",
            )
            for r in raw_reviews
        ],
        contributors=contributors,
        last_push=pushed_at or datetime.now(UTC),
    )


def standing_reviews(reviews: list[Review]) -> dict[str, Review]:
    """Each person's current verdict.

    A `commented` review does not replace an earlier approval or request for
    changes - GitHub keeps the earlier verdict standing - so it is skipped
    rather than treated as a newer state.
    """
    latest: dict[str, Review] = {}
    for review in reviews:
        if not review.login or review.state in ("COMMENTED", "PENDING", "DISMISSED"):
            continue
        current = latest.get(review.login)
        if current is None or review.submitted_at >= current.submitted_at:
            latest[review.login] = review
    return latest


@dataclass
class Requirement:
    text: str
    met: bool
    who: str


@dataclass
class Verdict:
    passed: bool
    title: str
    requirements: list[Requirement] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"### `quorum`: {self.title}", ""]
        if self.requirements:
            lines += ["| Requirement | Met | Who can satisfy it |", "|---|---|---|"]
            for req in self.requirements:
                lines.append(f"| {req.text} | {'yes' if req.met else '**no**'} | {req.who} |")
            lines.append("")
        lines += self.notes
        lines.append("")
        lines.append(
            "Rules: [`docs/governance/review-charter.md`](docs/governance/review-charter.md) "
            "sections 1, 3, 4, 6 and 10. Roster: `.github/quorum-roster.toml`."
        )
        return "\n".join(lines)


def _names(logins: set[str] | frozenset[str]) -> str:
    return ", ".join(f"@{login}" for login in sorted(logins)) or "nobody on the roster"


def evaluate(pr: PullRequest, roster: Roster, owners: list[tuple[str, list[str]]], now: datetime) -> Verdict:
    if pr.is_draft:
        return Verdict(True, "draft, nothing to decide yet")

    standing = standing_reviews(pr.reviews)
    approvals = {
        login for login, review in standing.items() if review.state == "APPROVED" and review.commit_id == pr.head_sha
    }
    stale_approvals = {
        login for login, review in standing.items() if review.state == "APPROVED" and review.commit_id != pr.head_sha
    }
    changes_requested = {login for login, review in standing.items() if review.state == "CHANGES_REQUESTED"}

    verdict = Verdict(True, "")
    blocking = changes_requested & (roster.quorum_holders | {roster.maintainer})
    if blocking:
        verdict.requirements.append(
            Requirement(
                f"no standing *changes requested* (open: {_names(blocking)})",
                False,
                "the reviewer who asked for changes, by approving or dismissing their review",
            )
        )

    # 1. The objection window on governance changes, for every author.
    governance = [p for p in pr.paths if p in GOVERNANCE_PATHS]
    if governance:
        elapsed = now - pr.last_push
        met = elapsed >= GOVERNANCE_WINDOW
        remaining = GOVERNANCE_WINDOW - elapsed
        hours = max(0, int(remaining.total_seconds() // 3600))
        verdict.requirements.append(
            Requirement(
                f"72 hours open for objections (touches {', '.join(f'`{p}`' for p in governance)})",
                met,
                "nobody - it merges once the window closes" + ("" if met else f", about {hours}h from now"),
            )
        )

    if pr.author in roster.automation:
        stops = sorted(
            p
            for p in pr.paths
            if any(word in p for word in AUTOMATION_STOP_WORDS) or p.startswith(AUTOMATION_STOP_PREFIXES)
        )
        if stops:
            met = roster.maintainer in approvals
            verdict.requirements.append(
                Requirement(
                    f"maintainer approval: automation changing {', '.join(f'`{p}`' for p in stops[:4])}"
                    + (" and others" if len(stops) > 4 else ""),
                    met,
                    f"@{roster.maintainer}",
                )
            )
        else:
            verdict.notes.append("Automation merging its own change on green CI (charter, section 1).")
    elif pr.author == GITHUB_ACTIONS_BOT:
        verdict.requirements.append(
            Requirement(
                "maintainer approval: a pull request opened by the workflow account",
                roster.maintainer in approvals,
                f"@{roster.maintainer}",
            )
        )
    elif pr.author == roster.maintainer:
        verdict.notes.append(
            "The maintainer's own change merges without approvals; a committer's "
            "*changes requested* blocks it (charter, section 6)."
        )
    else:
        eligible = roster.quorum_holders - {pr.author} - pr.contributors
        approving = approvals & eligible
        approving_core = approving & roster.core

        sensitive = sorted(p for p in pr.paths if any(word in p for word in SENSITIVE_WORDS))
        large = pr.changed_lines > THIRD_APPROVAL_LINES
        need_total, need_core = (3, 2) if (large or sensitive) else (2, 1)
        reason = (
            f"{pr.changed_lines} changed lines"
            if large
            else (f"touches `{sensitive[0]}`" if sensitive else f"{pr.changed_lines} changed lines")
        )

        verdict.requirements.append(
            Requirement(
                f"{need_total} approvals, {need_core} from a core reviewer ({reason})",
                len(approving) >= need_total and len(approving_core) >= need_core,
                _names(eligible - approving),
            )
        )

        if pr.changed_lines > MAINTAINER_OR_SPLIT_LINES:
            verdict.requirements.append(
                Requirement(
                    f"maintainer approval or a split ({pr.changed_lines} changed lines, "
                    f"over {MAINTAINER_OR_SPLIT_LINES})",
                    roster.maintainer in approvals,
                    f"@{roster.maintainer}, or split the change",
                )
            )

        protected: dict[str, list[str]] = {}
        for path in pr.paths:
            path_owners, specific = owners_for(path, owners)
            if specific:
                for owner in path_owners:
                    protected.setdefault(owner, []).append(path)
        for owner, paths in sorted(protected.items()):
            verdict.requirements.append(
                Requirement(
                    f"approval from the owner of {', '.join(f'`{p}`' for p in paths[:3])}"
                    + (" and others" if len(paths) > 3 else ""),
                    owner in approvals,
                    f"@{owner}",
                )
            )

    if stale_approvals:
        verdict.notes.append(
            f"Approvals given before the last push do not count: {_names(stale_approvals)}. "
            "A new push means nobody has read what is about to merge (charter, section 2)."
        )

    verdict.passed = all(req.met for req in verdict.requirements)
    missing = sum(1 for req in verdict.requirements if not req.met)
    verdict.title = "review requirements met" if verdict.passed else f"{missing} requirement(s) missing"
    return verdict


def pr_number_from_env(env: dict[str, str]) -> int | None:
    """The pull request under test, for each event that can trigger this.

    On `merge_group` the ref names the queued pull request, which is the one
    whose reviews still have to hold at the moment it merges.
    """
    if env.get("GITHUB_EVENT_NAME") == "merge_group":
        match = re.search(r"/pr-(\d+)-", env.get("GITHUB_REF", ""))
        return int(match.group(1)) if match else None
    event_path = env.get("GITHUB_EVENT_PATH")
    if event_path and os.path.isfile(event_path):
        with open(event_path, encoding="utf-8") as handle:
            event = json.load(handle)
        number = (event.get("pull_request") or {}).get("number")
        if number:
            return int(number)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--pr", type=int, default=None)
    parser.add_argument("--root", default=".", help="where to read the roster and CODEOWNERS from")
    parser.add_argument(
        "--governance-paths",
        action="store_true",
        help="print the paths that carry the objection window, one per line, and exit",
    )
    args = parser.parse_args(argv)

    # The hourly sweep in quorum-rerun.yml needs this list to know which pull
    # requests change verdict with the clock. It used to carry its own copy in a
    # jq filter, which is a second place to forget.
    if args.governance_paths:
        print("\n".join(GOVERNANCE_PATHS))
        return 0

    number = args.pr or pr_number_from_env(dict(os.environ))
    if number is None:
        print("quorum: no pull request in this event; nothing to check")
        return 0

    pr = fetch_pull_request(args.repo, number)
    verdict = evaluate(pr, load_roster(args.root), load_codeowners(args.root), datetime.now(UTC))

    summary = verdict.summary()
    print(summary)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(summary + "\n")
    if not verdict.passed:
        print(f"::error title=quorum::{verdict.title} - see the job summary for what is missing")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
