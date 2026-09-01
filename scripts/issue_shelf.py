#!/usr/bin/env python3
"""Keep the contributor shelf honest: advertise what is free, un-advertise what is taken.

Contributor-facing labels (``help wanted``, ``up-for-grabs``, ``good first issue``,
``beginner-friendly``) are applied and removed by hand today, which fails in both
directions. An issue nobody has taken sits unadvertised and nobody finds it; an issue
somebody *has* taken keeps its labels and the next contributor spends an evening
colliding with work that already exists.

Both directions are mechanical, so this decides them mechanically -- with one
deliberate exception.

**Three outcomes, not two.** Whether an issue is "being worked on" is not always
provable. An assignee proves it. An open pull request whose body closes the issue
proves it. A comment saying "picking this up" does not: the same sentence covers
someone who started yesterday and someone who asked and never came back. So a recent
comment from an outside contributor puts the issue in ``UNPROVEN``: nothing is added,
nothing is removed, and it is printed for a human to settle in five seconds. Acting on
an unprovable signal in either direction is worse than not acting, and a report that
mixes "proved free" with "probably free" is the failure this avoids.

**A human veto is permanent.** If someone removed a bait label by hand, re-adding it on
the next sweep is a bot arguing with a maintainer. The timeline says who removed what;
a removal by anyone other than this job is recorded as ``no-bait`` and respected from
then on.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

#: Labels this job manages. It never touches any other label.
BAIT = ("help wanted", "up-for-grabs")
#: Added on top of BAIT only for the smallest, best-briefed issues -- a beginner sent at
#: a `size/l` epic does not come back.
BEGINNER_BAIT = ("good first issue", "beginner-friendly")
#: A human removed a bait label. Never bait again until a human removes this.
VETO = "no-bait"

#: An issue carrying any of these is spoken for or not shelf-ready. `fleet-running` is
#: written by the dispatcher when a lane picks the issue up: the queue lives outside
#: GitHub, so without that label an automated run is invisible here and the shelf would
#: advertise work already in progress.
HOLDS = frozenset({"reserved", "blocked", "needs-better-brief", "fleet-blocked", "fleet-running"})
#: A tracking issue has no code-shaped deliverable; assigning one hands out a ticket
#: that cannot be closed.
TRACKING = "roadmap"
#: `size/l` and `size/xl` convert poorly enough that advertising them fills the shelf
#: with work nobody takes. They need slicing first, which is a human judgement.
SHELF_SIZES = frozenset({"size/xs", "size/s", "size/m"})
SMALL_SIZES = frozenset({"size/xs", "size/s"})

_CLOSES = re.compile(
    r"\b(?:closes|closed|close|fixes|fixed|fix|resolves|resolved|resolve|part of|towards)\b[\s:]*#(\d+)", re.I
)
_ACCEPTANCE = re.compile(r"^#+\s*acceptance criteria|^\s*-\s*\[[ x]\]|definition of done", re.I | re.M)
_EVIDENCE = re.compile(r"`[^`\n]+`|[\w./-]+\.(?:py|md|ya?ml|toml|sh|json)\b")


@dataclass(frozen=True)
class Issue:
    number: int
    labels: frozenset[str]
    assignees: tuple[str, ...]
    milestone: str | None
    body: str
    last_outside_comment_days: float | None


@dataclass
class Decision:
    number: int
    action: str  # BAIT | UNBAIT | LEAVE | UNPROVEN
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    reason: str = ""


@dataclass
class Repo:
    claimed_by_pr: frozenset[int] = field(default_factory=frozenset)
    vetoed: frozenset[int] = field(default_factory=frozenset)
    unproven_days: float = 14.0


def _shelf_ready(issue: Issue) -> str | None:
    """Return the reason this issue is not ready for the shelf, or None if it is."""
    sizes = issue.labels & {label for label in issue.labels if label.startswith("size/")}
    if not sizes:
        return "no size label"
    if not (sizes & SHELF_SIZES):
        return f"size not shelf-sized ({'/'.join(sorted(sizes))})"
    if issue.milestone is None:
        return "no milestone"
    if not _ACCEPTANCE.search(issue.body):
        return "body states no acceptance criteria"
    if not _EVIDENCE.search(issue.body):
        return "body names no path or symbol"
    return None


def decide(issue: Issue, repo: Repo) -> Decision:
    """Classify one issue. Pure: every input is already resolved by the caller."""
    present = tuple(sorted(issue.labels & set(BAIT + BEGINNER_BAIT)))

    # --- taken, provably. Bait must come off, whatever else is true. -----------------
    if issue.assignees:
        taken = f"assigned to {', '.join(issue.assignees)}"
    elif issue.number in repo.claimed_by_pr:
        taken = "an open pull request closes it"
    elif issue.labels & HOLDS:
        taken = f"held by {', '.join(sorted(issue.labels & HOLDS))}"
    elif TRACKING in issue.labels:
        taken = "tracking issue, no code-shaped deliverable"
    else:
        taken = ""
    if taken:
        if present:
            return Decision(issue.number, "UNBAIT", remove=present, reason=taken)
        return Decision(issue.number, "LEAVE", reason=f"not baited; {taken}")

    # --- a human said no. Respected in the additive direction only. ------------------
    if VETO in issue.labels or issue.number in repo.vetoed:
        return Decision(issue.number, "LEAVE", reason="human veto recorded; never re-baited")

    # --- unprovable. Add nothing, remove nothing, print it. ---------------------------
    recent = issue.last_outside_comment_days
    if recent is not None and recent <= repo.unproven_days:
        return Decision(
            issue.number,
            "UNPROVEN",
            reason=f"an outside contributor commented {recent:.0f}d ago; intent is not provable from a comment",
        )

    # --- free, but is it worth advertising? -------------------------------------------
    not_ready = _shelf_ready(issue)
    if not_ready:
        # Deliberately not an UNBAIT: an issue baited by hand that later loses its
        # milestone should not be silently pulled off the shelf by this job.
        return Decision(issue.number, "LEAVE", reason=f"free but not shelf-ready: {not_ready}")

    want = set(BAIT)
    if issue.labels & SMALL_SIZES and re.search(r"^#+\s*where to start", issue.body, re.I | re.M):
        want |= set(BEGINNER_BAIT)
    add = tuple(sorted(want - issue.labels))
    if not add:
        return Decision(issue.number, "LEAVE", reason="already advertised correctly")
    return Decision(issue.number, "BAIT", add=add, reason="free, sized, milestoned and briefed")


# --------------------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------------------
def _gh(*args: str) -> str:
    proc = subprocess.run(("gh", *args), capture_output=True, text=True)
    if proc.returncode != 0:
        # An empty answer from a failed call is not "nothing there" -- say so and stop,
        # rather than letting a refusal read as an empty shelf and un-baiting everything.
        raise SystemExit(f"gh {' '.join(args)} failed rc={proc.returncode}: {proc.stderr.strip()[:400]}")
    return proc.stdout


def is_outside_human(user: dict[str, Any], maintainer: str) -> bool:
    """Is this comment author an outside contributor, rather than a bot or the maintainer?

    Asks the type, never the spelling. REST carries ``user.type == "Bot"``; GraphQL
    returns an app's login with no ``[bot]`` suffix, so a check on the suffix reads every
    comment the orchestrator leaves as a human claiming the issue.
    """
    if user.get("type") == "Bot":
        return False
    login = user.get("login") or ""
    return bool(login) and login != maintainer


def _days_since_outside_comment(repo: str, number: int, maintainer: str) -> float | None:
    """Age of the newest comment by a human who is not the maintainer, in days.

    Read over REST rather than ``gh issue view --json comments``, because the two spell
    bots differently: REST carries ``user.type == "Bot"``, while GraphQL returns the app
    login with no ``[bot]`` suffix. Filtering on the suffix therefore reads every comment
    the orchestrator leaves as an outside contributor claiming the issue -- which is how
    four issues it had just commented on came back as unprovable. Ask the type, not the
    spelling.
    """
    raw = json.loads(_gh("api", "--paginate", f"repos/{repo}/issues/{number}/comments"))
    newest: str | None = None
    for comment in raw:
        if not is_outside_human(comment.get("user") or {}, maintainer):
            continue
        created = comment.get("created_at")
        if created and (newest is None or created > newest):
            newest = created
    if newest is None:
        return None
    stamp = datetime.fromisoformat(newest.replace("Z", "+00:00"))
    return (datetime.now(tz=UTC) - stamp).total_seconds() / 86400.0


def missing_control_labels(repo: str) -> list[str]:
    """Which of the labels that keep an issue OFF the shelf does this repo not define?

    Every hold, and the veto, is a reason not to advertise. A label that does not exist
    in the repo can never be on an issue, so the branch that reads it is dead: the guard
    looks present in the source and is inert in the run. Both live examples cut the same
    way. `fleet-running` -- the dispatch queue is outside GitHub, so without that label
    the only record that a lane is working an issue is one this script cannot see, and a
    running issue reads as free. `no-bait` -- without it a maintainer has no way to say
    "leave this one alone", and the next run re-advertises whatever they just took down.
    """
    defined = {
        label["name"] for label in json.loads(_gh("label", "list", "-R", repo, "--limit", "500", "--json", "name"))
    }
    return sorted((HOLDS | {VETO}) - defined)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "sipyourdrink-ltd/bernstein"))
    ap.add_argument("--maintainer", default="chernistry", help="comments by this login never count as an outside claim")
    ap.add_argument("--unproven-days", type=float, default=14.0)
    ap.add_argument("--apply", action="store_true", help="write labels; without it the run reports and changes nothing")
    ap.add_argument("--limit", type=int, default=400)
    args = ap.parse_args()

    missing = missing_control_labels(args.repo)
    if missing:
        print(f"labels that hold an issue back are not defined in {args.repo}: {', '.join(missing)}")
        print("Nothing can carry a label the repo does not define, so the shelf would advertise work these protect.")
        if args.apply:
            print("Refusing to write. Create the labels first, or drop them from HOLDS/VETO.")
            return 2

    raw = json.loads(
        _gh(
            "issue",
            "list",
            "-R",
            args.repo,
            "--state",
            "open",
            "--limit",
            str(args.limit),
            "--json",
            "number,labels,assignees,milestone,body",
        )
    )
    prs = json.loads(_gh("pr", "list", "-R", args.repo, "--state", "open", "--limit", "200", "--json", "body"))
    claimed = {int(m) for pr in prs for m in _CLOSES.findall(pr.get("body") or "")}

    issues = [
        Issue(
            number=i["number"],
            labels=frozenset(label["name"] for label in i["labels"]),
            assignees=tuple(a["login"] for a in i["assignees"]),
            milestone=(i["milestone"] or {}).get("title"),
            body=i.get("body") or "",
            last_outside_comment_days=None,
        )
        for i in raw
    ]
    repo = Repo(claimed_by_pr=frozenset(claimed), unproven_days=args.unproven_days)

    # Two passes. Comment history is one API call per issue, so it is fetched only for
    # the issues a write would otherwise land on -- the rest are decided by data already
    # in the list response. An issue nobody would touch does not need its timeline read.
    provisional = {i.number: decide(i, repo) for i in issues}
    candidates = [i for i in issues if provisional[i.number].action in {"BAIT", "UNBAIT"}]
    by_number = {i.number: i for i in issues}
    for issue in candidates:
        days = _days_since_outside_comment(args.repo, issue.number, args.maintainer)
        if days is not None:
            by_number[issue.number] = replace(issue, last_outside_comment_days=days)
    decisions = [decide(by_number[i.number], repo) for i in issues]

    verb = "" if args.apply else "would "
    for kind in ("UNBAIT", "BAIT", "UNPROVEN"):
        rows = [d for d in decisions if d.action == kind]
        if not rows:
            continue
        print(f"\n## {kind} ({len(rows)})")
        for d in rows:
            change = "".join(f" +{label!r}" for label in d.add) + "".join(f" -{label!r}" for label in d.remove)
            print(f"- #{d.number}: {verb}{kind.lower()}{change} — {d.reason}")
            if args.apply and (d.add or d.remove):
                cmd = ["issue", "edit", str(d.number), "-R", args.repo]
                for label in d.add:
                    cmd += ["--add-label", label]
                for label in d.remove:
                    cmd += ["--remove-label", label]
                _gh(*cmd)

    shelf = sum(1 for i in issues if i.labels & set(BAIT) and not i.assignees)
    small = sum(1 for i in issues if i.labels & set(BAIT) and i.labels & SMALL_SIZES and not i.assignees)
    print(f"\n## Shelf\n- advertised and unassigned: {shelf} (small: {small}; target 8-12 small)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
