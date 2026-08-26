"""Compute the main-branch red-rate for the Trunk Health SLO gate.

Ports the inline shell logic from `.github/workflows/trunk-health-slo.yml`
into a testable Python script. Fixes the population sampling by querying
the CI workflow's own runs endpoint, excludes cancelled/skipped/null runs,
and enforces a minimum sample size before toggling the marker.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MIN_SAMPLE_SIZE = 10

# A marker needs more than one red run behind it.
#
# The lookback window produces samples in the 10-30 range, so at the default
# 5% threshold a single failure already crosses it: 1/19 is 5.26%. That makes
# the gate zero-tolerance while reading like a rate, and one infra flake -
# a timed-out docker probe, a runner that never picked the job up - holds
# every merge in the repo until the run ages out of the window. The gate is
# meant to catch a trunk that is actually red, which shows up as a second
# failure, not as one.
MIN_RED_RUNS = 2

# A scheduled gate that blocks on a socket holds the runner until the job
# timeout kills it, which reads as an infrastructure outage rather than as
# the network call it actually is.
_HTTP_TIMEOUT_S = 30


def fetch_ci_runs(repo: str, token: str, since: datetime) -> list[dict]:
    """Fetch CI workflow runs on main since the given timestamp."""
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    runs: list[dict] = []
    page = 1

    while True:
        # Use the API's created parameter to avoid truncating the time window
        params = {
            "branch": "main",
            "per_page": "100",
            "created": f">={since_iso}",
            "page": str(page),
        }
        url = f"https://api.github.com/repos/{repo}/actions/workflows/ci.yml/runs?{urlencode(params)}"
        req = Request(url, headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"})

        with urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            data = json.load(resp)

        page_runs = data.get("workflow_runs", [])
        if not page_runs:
            break

        runs.extend(page_runs)

        # If we got fewer than 100 runs, we've reached the end
        if len(page_runs) < 100:
            break

        page += 1

    return runs


def score_runs(runs: list[dict]) -> tuple[int, int, int]:
    """Score the runs, returning (total, red, red_pct).

    Excludes cancelled, skipped, and null (in-progress) runs from both
    numerator and denominator. An in-progress run has no verdict yet,
    so it is not a data point.
    """
    valid_runs = [r for r in runs if r.get("conclusion") not in ("cancelled", "skipped", None)]
    total = len(valid_runs)
    red = sum(1 for r in valid_runs if r.get("conclusion") in ("failure", "timed_out"))
    red_pct = (red * 100) // total if total > 0 else 0
    return total, red, red_pct


def marker_should_open(total: int, red: int, red_pct: int, threshold_pct: int) -> bool:
    """Whether this sample justifies holding every merge in the repo.

    Kept as its own function because the andon decision is the only thing
    this script does that anyone has to trust, and an expression inlined in
    ``main`` is reachable only by driving argv and the network.
    """
    if total < MIN_SAMPLE_SIZE:
        return False
    return red >= MIN_RED_RUNS and red_pct >= threshold_pct


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--threshold-pct", type=int, default=5)
    parser.add_argument("--lookback-hours", type=int, default=24)
    args = parser.parse_args()

    # Read token from environment to avoid exposing it in argv
    token = os.environ.get("GH_TOKEN")
    if not token:
        print("Error: GH_TOKEN environment variable not set.", file=sys.stderr)
        sys.exit(1)

    since = datetime.now(UTC) - timedelta(hours=args.lookback_hours)
    runs = fetch_ci_runs(args.repo, token, since)
    total, red, red_pct = score_runs(runs)

    insufficient = total < MIN_SAMPLE_SIZE
    unstable = marker_should_open(total, red, red_pct, args.threshold_pct)

    # Output for GitHub Actions
    github_output = os.environ.get("GITHUB_OUTPUT")
    output_lines = [
        f"total={total}",
        f"red={red}",
        f"red_pct={red_pct}",
        f"unstable={str(unstable).lower()}",
        f"insufficient_sample={str(insufficient).lower()}",
    ]

    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write("\n".join(output_lines) + "\n")
    else:
        print("\n".join(output_lines))


if __name__ == "__main__":
    main()
