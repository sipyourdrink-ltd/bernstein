#!/usr/bin/env python3
"""Warn about roster entries that expire soon. Always exits 0.

Reads ``.github/quorum-roster.toml`` and prints every entry whose term ends
within ``--days`` days (default 30), plus those already expired. A workflow
can adopt it later; today it only informs the maintainer, who renews or lets
an entry lapse by editing the roster (charter, "Roles and terms").
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from datetime import UTC, date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quorum_check import ROSTER_PATH, parse_entries

TIERS = ("core_reviewers", "committers", "triagers")


def expiring(raw: dict, today: date, days: int) -> list[tuple[str, str, date]]:
    """(tier, login, expiry) for entries ending on or before today + days."""
    lists = [(tier, raw.get(tier)) for tier in TIERS]
    lists += [(f"area_reviewers.{area}", v) for area, v in raw.get("area_reviewers", {}).items()]
    horizon = today + timedelta(days=days)
    return sorted(
        (tier, login, expiry)
        for tier, entries in lists
        for login, expiry in parse_entries(entries)
        if expiry is not None and expiry <= horizon
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="where to read the roster from")
    parser.add_argument("--days", type=int, default=30, help="warning window in days")
    args = parser.parse_args(argv)
    today = datetime.now(UTC).date()
    with open(os.path.join(args.root, ROSTER_PATH), "rb") as handle:
        raw = tomllib.load(handle)
    rows = expiring(raw, today, args.days)
    if not rows:
        print(f"No roster entry expires within {args.days} days.")
    for tier, login, expiry in rows:
        state = "EXPIRED" if expiry <= today else f"expires in {(expiry - today).days} days"
        print(f"{login} ({tier}): {expiry.isoformat()} - {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
