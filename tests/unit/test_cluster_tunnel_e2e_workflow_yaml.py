"""Cadence assertions for the cluster tunnel end-to-end workflow.

Covers ``.github/workflows/cluster-tunnel-e2e.yml``.

This lane makes real outbound calls to Cloudflare's edge and had 0 failures
in 81 runs, so it moved off a daily cron to a weekly Sunday fire and stopped
competing for the 20-slot runner pool that pull-request verdicts queue
behind. Two things can undo that:

* the day-of-week field goes back to ``*`` and the lane is daily again,
  which reads as a formatting change in a diff rather than a 7x change in
  scheduled runs;
* the cron changes and the header keeps calling the run "Nightly", so the
  file's own description of its cadence is wrong - which is the state this
  test was written against.

Both are pinned here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "cluster-tunnel-e2e.yml"

#: Position of the day-of-week field in a five-field cron expression.
DAY_OF_WEEK = 4

#: A header bullet, e.g. ``#   * On-demand: workflow_dispatch.``
BULLET = re.compile(r"^#\s+\*\s+(?P<text>.*)$")


def _doc() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{WORKFLOW.name} is not a mapping"
    return cast("dict[str, Any]", data)


def _triggers() -> dict[str, Any]:
    doc = _doc()
    # PyYAML 1.1 parses a bare ``on:`` key as the boolean True.
    triggers = doc.get(True, doc.get("on"))
    assert isinstance(triggers, dict), "workflow must declare a mapping of triggers"
    return cast("dict[str, Any]", triggers)


def _crons() -> list[str]:
    schedule = _triggers().get("schedule")
    assert isinstance(schedule, list) and schedule, "workflow must declare a schedule"
    crons = [entry["cron"] for entry in schedule if isinstance(entry, dict) and "cron" in entry]
    assert crons, "every schedule entry must carry a `cron:` expression"
    return [str(cron).strip() for cron in crons]


def _header_bullets() -> list[str]:
    """The comment bullets above the ``on:`` key, continuations folded in."""
    bullets: list[str] = []
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        if line.startswith("on:"):
            break
        match = BULLET.match(line)
        if match is not None:
            bullets.append(match.group("text").strip())
            continue
        continuation = line.lstrip("#").strip() if line.startswith("#") else ""
        if bullets and continuation:
            bullets[-1] = f"{bullets[-1]} {continuation}"
    assert bullets, "the header must describe when this workflow runs"
    return bullets


def test_workflow_file_exists() -> None:
    assert WORKFLOW.is_file()


def test_the_schedule_is_not_daily() -> None:
    """A ``*`` day-of-week is a 7x change in scheduled runs, in one character."""
    for cron in _crons():
        fields = cron.split()
        assert len(fields) == 5, f"`cron: {cron}` is not a five-field expression"
        assert fields[DAY_OF_WEEK] != "*", (
            f"`cron: {cron}` fires every day. This lane makes real outbound "
            "calls to Cloudflare's edge and had 0 failures in 81 runs, which "
            "is why it moved to a weekly Sunday fire. Restoring a daily "
            "cadence is a decision to state, not a field to widen - and the "
            "header comment has to change with it"
        )


def test_the_header_calls_the_cadence_weekly() -> None:
    """The header outlived the cadence once already."""
    bullets = _header_bullets()
    assert any(bullet.startswith("Weekly:") for bullet in bullets), (
        f"no header bullet describes a weekly run: {bullets}. The cron fires "
        "on Sundays; the header is where an operator reads that without "
        "parsing a cron expression"
    )


def test_the_header_no_longer_calls_the_run_nightly() -> None:
    """A nightly label beside a Sunday-only cron is the pinned mismatch."""
    offenders = [bullet for bullet in _header_bullets() if "nightly" in bullet.lower()]
    assert not offenders, (
        f"the header still calls this a nightly run: {offenders}. It fires "
        "weekly on Sundays - a header that says otherwise sends someone "
        "looking for yesterday's run"
    )
