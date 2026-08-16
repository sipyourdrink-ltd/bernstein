"""Structural assertions for the observability snapshot workflow.

Covers ``.github/workflows/docs-observability-snapshot.yml``. The
schedule trigger was deliberately removed in #2856 - the snapshot doc
was not linked from the docs nav and several auto-opened PRs went
unmerged - and #3954 confirmed that decision stands. This file pins
two things so they cannot silently drift apart again:

- the workflow stays ``workflow_dispatch``-only (no schedule trigger
  sneaks back in without an explicit decision to restore it),
- the auto-PR body and the regression-gate step comment describe a
  sparse, gap-aware cadence rather than the "daily" / "last 30 days"
  framing that #3954 found stale against the actual manual-only
  trigger.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs-observability-snapshot.yml"


def _load() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{WORKFLOW.name} is not a mapping"
    return cast("dict[str, Any]", data)


def test_workflow_file_exists() -> None:
    assert WORKFLOW.is_file()


def test_trigger_stays_manual_only() -> None:
    """#2856 removed the schedule on purpose; #3954 confirmed it stays removed."""
    doc = _load()
    triggers = doc.get(True, doc.get("on"))
    assert isinstance(triggers, dict), "workflow must declare a mapping of triggers"
    assert set(triggers.keys()) == {"workflow_dispatch"}, (
        "a schedule trigger reappeared - restoring cadence is a maintainer "
        "decision (#3954 kept this manual-only), not a side effect of an "
        "unrelated edit"
    )


def test_auto_pr_body_does_not_claim_a_daily_calendar_window() -> None:
    raw = " ".join(WORKFLOW.read_text(encoding="utf-8").split())
    assert "covering the last 30 days" not in raw
    assert "30 captured snapshots" in raw


def test_regression_gate_comment_notes_the_gap_may_not_be_small() -> None:
    doc = _load()
    jobs = doc.get("jobs", {})
    assert isinstance(jobs, dict)
    snapshot_job = jobs.get("snapshot", {})
    assert isinstance(snapshot_job, dict)
    steps = snapshot_job.get("steps", [])
    assert isinstance(steps, list)
    gate_step = next(
        (s for s in steps if isinstance(s, dict) and s.get("name") == "Detect metric regressions"),
        None,
    )
    assert gate_step is not None
    # yaml.safe_load drops comments, so read the raw text around the step
    # name instead of relying on the parsed mapping for this assertion.
    raw = WORKFLOW.read_text(encoding="utf-8")
    idx = raw.index("Detect metric regressions")
    window = raw[idx : idx + 600]
    assert "sporadic" in window or "gap" in window
