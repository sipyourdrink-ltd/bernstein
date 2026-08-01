#!/usr/bin/env python3
"""Decide which `review-bot-ack` publisher, if any, speaks for a head SHA.

Why this exists
---------------
The required `review-bot-ack` context is published by
`.github/workflows/review-bot-ack-publish.yml`, a `workflow_run` hop that
exists because a fork's `pull_request` run holds a read-only token and
cannot write a check-run itself.

A head SHA routinely collects several gate runs - a push, two body edits,
a review from each bot - and every one of them wakes a publisher. Only one
of those publishers may write, because the check-run is upserted per head
SHA and the last writer wins outright. Publishers also queue for runners,
so the order they write in is not the order the gates concluded in: the
first gate to finish can be the last publisher to run and can overwrite a
newer, better answer.

So each publisher asks whether it is still the current one. The rule used
to be "stand down if a gate run with a higher id exists", and that rule
left heads with no writer at all (#3313). Two ways:

  * The newest gate run concluded `cancelled`. A cancelled run has no
    verdict and must not write one, so its publisher stands down - and
    every older publisher had already stood down as stale. Observed on
    PR #3287: approving a first-time contributor released three parked
    runs at 12:00:02Z, concurrency cancelled 30622786412 three seconds
    later, and the survivor 30622733829 carried the *lower* id. Runs
    released together are cancelled in an order unrelated to their ids,
    so "newest id" and "newest verdict" are simply different things.

  * The newest gate run came from `pull_request_review`, which the
    publisher's job condition did not admit. Observed on PR #3293:
    run 30655244121 passed at 18:28:48Z, its in-job publish 403'd the way
    every fork's does, and no publisher ever ran.

Both heads sat at BLOCKED with no failing check to point at, and only
closing and reopening the pull request produced the context.

The rule this module implements
-------------------------------
A gate run *can publish* when its event needs the hop and it did not end
cancelled. A publisher stands down only for a successor that can publish -
a run that never will is not a reason to stay quiet. If nothing newer can
publish and this run cannot either, the head has no writer left, and the
publisher re-dispatches the gate rather than standing down silently.

The re-dispatch is bounded to one per head SHA: it is skipped once any
gate run on the head carries `run_attempt > 1`, which is the mark a
re-dispatch (or an operator's own re-run) leaves in GitHub's own state.
No ledger, no external memory, and a second cancellation of the same head
cannot start a loop.

The decision is a pure function of the run list, so it is unit-tested
against the three recorded incidents instead of being discovered in
production a fourth time.

Usage:
    python scripts/ack_publisher_currency.py \\
        --runs runs.json \\
        --this-run 30622733829 \\
        --context-present false

`--runs` accepts either the raw `GET /repos/{repo}/actions/runs` payload
or a bare list of runs, and reads stdin when given `-`.

Outputs `decision=publish|stand-down|redispatch` on stdout and, when
`GITHUB_OUTPUT` is set, appends it there too.

Exit codes:
    0  A decision was reached and reported.
    1  The input could not be read or parsed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# The gate's `pull_request` and `pull_request_review` runs both get a
# read-only token on a fork, so both need the `workflow_run` hop. A
# `merge_group` run already holds `checks: write` and publishes in-repo;
# a second writer for it would only race the first.
PUBLISHABLE_EVENTS = frozenset({"pull_request", "pull_request_review"})

GATE_WORKFLOW_NAME = "Review-bot acknowledgement gate"

PUBLISH = "publish"
STAND_DOWN = "stand-down"
REDISPATCH = "redispatch"


def _run_id(run: dict[str, Any]) -> int:
    """Return a run's id, or 0 when it is missing or unparseable."""
    try:
        return int(run.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def can_publish(run: dict[str, Any]) -> bool:
    """Whether this gate run will ever write the required context.

    A run that is still in flight counts: it has not concluded, so it may
    yet produce a verdict, and a publisher must not overtake it.
    """
    if str(run.get("event") or "") not in PUBLISHABLE_EVENTS:
        return False
    return str(run.get("conclusion") or "") != "cancelled"


def gate_runs(payload: object, workflow_name: str = GATE_WORKFLOW_NAME) -> list[dict[str, Any]]:
    """Pull the gate's runs out of an Actions API payload or a bare list."""
    raw: object = payload
    if isinstance(payload, dict):
        raw = payload.get("workflow_runs", [])
    if not isinstance(raw, list):
        return []
    runs: list[dict[str, Any]] = [item for item in raw if isinstance(item, dict)]
    named = [run for run in runs if str(run.get("name") or workflow_name) == workflow_name]
    return sorted(named, key=_run_id)


def was_redispatched(runs: list[dict[str, Any]]) -> bool:
    """Whether some gate run on this head has already been run again.

    This is the loop guard, and it deliberately counts an operator's own
    re-run as well as an automatic one. A re-run *is* the re-dispatch; a
    second one on the same head would not add information, and refusing
    to fight a human's manual recovery is the point.
    """
    for run in runs:
        try:
            attempt = int(run.get("run_attempt") or 1)
        except (TypeError, ValueError):
            attempt = 1
        if attempt > 1:
            return True
    return False


def decide(
    runs: list[dict[str, Any]],
    this_run_id: int,
    *,
    context_present: bool = False,
) -> str:
    """Return this publisher's decision for its head SHA.

    ``publish``     this run holds the current verdict; write it.
    ``stand-down``  someone else will write, or already has.
    ``redispatch``  nothing can write for this head; re-run the gate.
    """
    # A newer run that can publish will publish. Standing down for one that
    # cannot is what left three heads with no writer at all (#3313).
    if any(_run_id(run) > this_run_id and can_publish(run) for run in runs):
        return STAND_DOWN

    mine = next((run for run in runs if _run_id(run) == this_run_id), None)
    if mine is not None and can_publish(mine):
        return PUBLISH

    # This run has no verdict to write. That only matters if nobody else has
    # one either: an older run that can publish is the head's writer, and
    # rules 1 and 2 have already told it so.
    if any(can_publish(run) for run in runs):
        return STAND_DOWN

    # No gate run on this head can ever publish. Without intervention the
    # required context stays absent, which reads as BLOCKED with nothing on
    # the page to point at, for the life of the commit.
    if context_present:
        return STAND_DOWN
    if was_redispatched(runs):
        return STAND_DOWN

    # Exactly one run re-dispatches, and it is the newest. Every cancelled
    # run on the head reaches this point, so without the tie-break they
    # would all re-dispatch and race each other.
    newest = max((_run_id(run) for run in runs), default=0)
    if this_run_id != newest:
        return STAND_DOWN
    return REDISPATCH


def _load(source: str) -> object:
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    return json.loads(text)


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", required=True, help="Actions API runs payload, or - for stdin")
    parser.add_argument("--this-run", required=True, type=int, help="id of the gate run that woke this publisher")
    parser.add_argument("--context-present", default="false", help="whether a terminal review-bot-ack already exists")
    parser.add_argument("--workflow-name", default=GATE_WORKFLOW_NAME)
    args = parser.parse_args(argv)

    try:
        payload = _load(str(args.runs))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read the gate runs for this head: {exc}", file=sys.stderr)
        return 1

    runs = gate_runs(payload, str(args.workflow_name))
    decision = decide(runs, int(args.this_run), context_present=_as_bool(str(args.context_present)))

    seen = ", ".join(f"{_run_id(r)}:{r.get('event')}/{r.get('conclusion') or r.get('status')}" for r in runs)
    print(f"gate runs on this head: [{seen}]")
    print(f"this run: {args.this_run}")
    print(f"decision={decision}")

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"decision={decision}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
