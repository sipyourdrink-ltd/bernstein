#!/usr/bin/env python3
"""Data-freshness checker for time-stamped metrics in repo docs.

Walks a fixed inventory of files that carry ``as of YYYY-MM-DD`` or
``(YYYY-MM-DD)`` markers and reports any marker that is older than a
soft / hard threshold relative to the current date.

Thresholds:

- 30 days  -> soft warning (printed, exit 0)
- 60 days  -> hard fail when invoked with ``--strict`` (exit 1)

The strict mode is intended for the push-to-main branch of the
``docs-drift`` workflow under a non-required check name
``docs-data-freshness``; the soft mode is intended for pull-request runs.

The inventory matches the rows enumerated in the playbook section
``Data-freshness drift (time-stamped metrics)`` in
``docs/playbooks/docs-drift.md``. When you add a new time-stamped line to
a repo doc, add the file to ``INVENTORY`` below and to that playbook
table.

Usage:

    uv run python scripts/check_data_freshness.py [--strict]
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files known to carry time-stamped metrics. Keep in sync with the
# inventory table in docs/playbooks/docs-drift.md.
INVENTORY: tuple[str, ...] = (
    "README.md",
    "docs/llm-citation-surface.md",
    "docs/compare/bernstein-vs-github-agent-hq.md",
    "docs/compare/index.html",
)

SOFT_THRESHOLD_DAYS = 30
HARD_THRESHOLD_DAYS = 60

# Matches either ``as of YYYY-MM-DD`` (case-insensitive) or a parenthesised
# ``(YYYY-MM-DD)`` token. The date itself is captured for parsing.
_DATE_RE = re.compile(
    r"(?:as of\s+(\d{4}-\d{2}-\d{2})|\((\d{4}-\d{2}-\d{2})\))",
    re.IGNORECASE,
)


def _parse_date(token: str) -> date | None:
    try:
        return datetime.strptime(token, "%Y-%m-%d").date()
    except ValueError:
        return None


def _scan_file(rel_path: str, today: date) -> list[tuple[int, str, int]]:
    """Return list of (line_no, line_text, age_days) for every marker."""
    path = REPO_ROOT / rel_path
    if not path.exists():
        return []
    results: list[tuple[int, str, int]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for match in _DATE_RE.finditer(line):
            token = match.group(1) or match.group(2)
            parsed = _parse_date(token)
            if parsed is None:
                continue
            age_days = (today - parsed).days
            results.append((line_no, line.strip(), age_days))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit with status 1 if any marker is older than the hard threshold",
    )
    parser.add_argument(
        "--today",
        default=None,
        help="override today's date (YYYY-MM-DD) for testing",
    )
    args = parser.parse_args(argv)

    today = date.today()
    if args.today:
        parsed_today = _parse_date(args.today)
        if parsed_today is None:
            print(f"error: --today must be YYYY-MM-DD, got {args.today!r}", file=sys.stderr)
            return 2
        today = parsed_today

    soft_hits: list[tuple[str, int, str, int]] = []
    hard_hits: list[tuple[str, int, str, int]] = []

    for rel_path in INVENTORY:
        for line_no, line_text, age_days in _scan_file(rel_path, today):
            if age_days >= HARD_THRESHOLD_DAYS:
                hard_hits.append((rel_path, line_no, line_text, age_days))
            elif age_days >= SOFT_THRESHOLD_DAYS:
                soft_hits.append((rel_path, line_no, line_text, age_days))

    if hard_hits:
        print(f"::group::data-freshness hard fail (>= {HARD_THRESHOLD_DAYS} days)")
        for rel_path, line_no, line_text, age_days in hard_hits:
            print(f"{rel_path}:{line_no}: {age_days} days old: {line_text}")
        print("::endgroup::")

    if soft_hits:
        print(f"::group::data-freshness soft warning (>= {SOFT_THRESHOLD_DAYS} days)")
        for rel_path, line_no, line_text, age_days in soft_hits:
            print(f"::warning file={rel_path},line={line_no}::{age_days} days old: {line_text}")
        print("::endgroup::")

    if not soft_hits and not hard_hits:
        print(f"data-freshness: all markers under {SOFT_THRESHOLD_DAYS} days (today={today.isoformat()})")
        return 0

    if hard_hits and args.strict:
        print(
            f"data-freshness: {len(hard_hits)} marker(s) exceed the "
            f"{HARD_THRESHOLD_DAYS}-day hard limit; failing the gate.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
