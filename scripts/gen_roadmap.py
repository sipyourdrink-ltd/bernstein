#!/usr/bin/env python3
"""Project the open GitHub milestones into the generated block of ROADMAP.md.

The milestone is already the single source of truth for what a release
contains: its description states the theme, the issues under it are the
work, its due date is the date. Restating any of that by hand in
ROADMAP.md would give the repo two answers to the same question and no
way to tell which one went stale, so this projects the milestones into
the file instead of asking anyone to keep the two in step.

Two deliberate omissions keep the projection quiet:

- No issue counts. They change with every issue opened or closed, and a
  roadmap that changes daily is a roadmap nobody reads. The counts are
  live on the milestone page the file links to.
- No timestamp. Identical milestones render byte-identical output, so
  the scheduled refresh opens a pull request when the roadmap actually
  changed rather than once a week forever.

    python scripts/gen_roadmap.py            # rewrite ROADMAP.md in place
    python scripts/gen_roadmap.py --check    # exit 1 if it is out of date
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

START = "<!-- roadmap:generated:start -->"
END = "<!-- roadmap:generated:end -->"
DEFAULT_REPO = "sipyourdrink-ltd/bernstein"
_PAGE_SIZE = 100


def fetch_milestones(repo: str, token: str | None) -> list[dict[str, Any]]:
    """Read the open milestones for ``repo`` from the GitHub REST API."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/milestones?state=open&per_page={_PAGE_SIZE}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "bernstein-roadmap",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload: list[dict[str, Any]] = json.load(response)
    if len(payload) >= _PAGE_SIZE:
        # Refusing beats paginating here. A truncated page would silently
        # drop milestones from the roadmap, and a repo with 100 open
        # milestones has a planning problem this script cannot fix.
        raise ValueError(
            f"{repo} has at least {_PAGE_SIZE} open milestones; this script reads one page "
            "and would silently drop the rest"
        )
    return payload


def _due_label(milestone: dict[str, Any]) -> str:
    """Render the due date, or say plainly that there is not one.

    A milestone without a date is not a scheduling oversight here: the
    breaking-change milestone is scoped by content and dating it would
    invent a commitment nobody made.
    """
    raw = milestone.get("due_on")
    if not raw:
        return "scoped by content, no date"
    # GitHub returns the Z spelling today. Pattern-matching that exact
    # spelling would make the scheduled job die with a stack trace instead
    # of a roadmap the first time an offset form comes back, so parse both.
    parsed = _parse_due(str(raw))
    return f"due {parsed.day} {parsed.strftime('%B %Y')}"


def _parse_due(raw: str) -> datetime:
    """Parse either ISO 8601 spelling GitHub may return for ``due_on``."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _sort_key(milestone: dict[str, Any]) -> tuple[int, datetime | str]:
    """Dated milestones first, in date order; undated ones last, by title.

    Ordering on the parsed instant rather than the raw string: the two
    ISO spellings sort differently as text, so a mixed set would come out
    in the wrong chronology while every individual date still rendered
    correctly - the kind of wrong that looks right.
    """
    raw = milestone.get("due_on")
    if raw:
        return (0, _parse_due(str(raw)))
    return (1, str(milestone.get("title", "")))


def render(milestones: list[dict[str, Any]]) -> str:
    """Render the generated block body from milestone records."""
    if not milestones:
        return "No open milestones."

    parts: list[str] = []
    for milestone in sorted(milestones, key=_sort_key):
        title = str(milestone.get("title", "")).strip()
        url = str(milestone.get("html_url", "")).strip()
        # A milestone description carrying a generated-block marker would
        # splice a second marker pair into ROADMAP.md, and every refresh
        # after that would refuse the file. Strip rather than raise: one
        # careless description should not permanently stop the roadmap.
        description = str(milestone.get("description") or "").replace(START, "").replace(END, "").strip()
        heading = f"### [{title}]({url}) — {_due_label(milestone)}"
        body = description or "_No description on the milestone yet._"
        parts.append(f"{heading}\n\n{body}")
    return "\n\n".join(parts)


def splice(text: str, block: str) -> str:
    """Replace the content between the generated markers with ``block``."""
    if text.count(START) != 1 or text.count(END) != 1:
        # Two marker pairs would splice between mismatched ones and delete
        # whatever sits in between, which is exactly the failure a
        # generated block is supposed to make impossible.
        raise ValueError(f"ROADMAP.md must carry exactly one {START} / {END} marker pair")
    start = text.find(START)
    end = text.find(END)
    if end < start:
        raise ValueError(f"ROADMAP.md has the {START} / {END} markers in the wrong order")
    return f"{text[: start + len(START)]}\n\n{block}\n\n{text[end:]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if ROADMAP.md is out of date")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO))
    parser.add_argument("--path", type=Path, default=Path("ROADMAP.md"))
    args = parser.parse_args(argv)

    current = args.path.read_text(encoding="utf-8")
    updated = splice(current, render(fetch_milestones(args.repo, os.environ.get("GITHUB_TOKEN"))))

    if updated == current:
        print("ROADMAP.md is up to date")
        return 0
    if args.check:
        print("ROADMAP.md is out of date; run python scripts/gen_roadmap.py", file=sys.stderr)
        return 1
    args.path.write_text(updated, encoding="utf-8")
    print(f"rewrote {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
