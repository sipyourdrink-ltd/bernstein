"""Structural assertions on the trunk andon mechanism.

The red-trunk merge hold spans two workflows: ``trunk-health-slo.yml``
computes the main-branch red-rate and toggles a marker issue labeled
``trunk-unstable``; the andon gate step in ``pr-policy.yml`` holds merges
while that marker is open. These tests pin the properties that let the
mechanism actually fire, learned from its first design not having them.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml

SLO_WORKFLOW = Path(".github/workflows/trunk-health-slo.yml")
POLICY_WORKFLOW = Path(".github/workflows/pr-policy.yml")


@pytest.fixture(scope="module")
def slo_text() -> str:
    return SLO_WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def policy_text() -> str:
    return POLICY_WORKFLOW.read_text(encoding="utf-8")


def test_the_flag_needs_no_token_beyond_the_job_scoped_one(slo_text: str) -> None:
    """The andon flag must be writable with plain GITHUB_TOKEN.

    The first design stored the flag in an Actions repo variable, which
    GITHUB_TOKEN cannot write under any ``permissions:`` grant. The write
    silently depended on a PAT that was never provisioned, every write
    failed with a swallowed warning, and the gate spent its whole life
    inert. Anchoring the flag on an issue keeps the mechanism inside what
    the job-scoped token can do, so there is no second credential to
    provision, rotate, or silently lose.
    """
    assert "BOT_PAT" not in slo_text, (
        "the andon write side must not depend on a separately provisioned PAT; "
        "that dependency is how the gate was inert the first time"
    )
    assert "actions/variables" not in slo_text, (
        "the flag must not live in an Actions variable -- GITHUB_TOKEN cannot write those"
    )
    assert "trunk-unstable" in slo_text, "the marker issue label is the flag; the writer must reference it"


def test_a_failed_flag_write_is_loud(slo_text: str) -> None:
    """A write failure must redden the SLO job, not degrade to a warning.

    The inert-gate failure mode survived for months because the write
    wrapped its failure in ``::warning`` + ``exit 0``. With ``set -e`` and
    no swallow, a broken write turns the scheduled run red, which is the
    only surface an unattended repository actually checks.
    """
    loaded = yaml.safe_load(slo_text)
    jobs = cast(dict[str, object], loaded["jobs"])
    compute = cast(dict[str, object], jobs["compute"])

    permissions = cast(dict[str, object], compute.get("permissions", {}))
    assert permissions.get("issues") == "write", "toggling the marker issue needs issues: write"

    steps = cast(list[dict[str, object]], compute.get("steps", []))
    toggle = next(
        (step for step in steps if "marker" in str(step.get("name", "")).lower()),
        None,
    )
    assert toggle is not None, "expected a step that toggles the trunk-unstable marker issue"
    run = str(toggle.get("run", ""))
    assert "set -euo pipefail" in run
    assert "exit 0" not in run, (
        "the toggle step must not swallow API failures; an inert andon gate looks exactly like a healthy trunk"
    )


def test_the_gate_reads_the_marker_issue(policy_text: str) -> None:
    """pr-policy's andon gate must key off the marker issue, not a variable."""
    assert "labels=trunk-unstable" in policy_text, (
        "the gate must query open trunk-unstable issues; that is where the flag lives now"
    )
    assert "actions/variables/TRUNK_UNSTABLE" not in policy_text, (
        "the gate must not read the retired Actions variable -- nothing writes it"
    )
    assert "hotfix-cleared" in policy_text, "the hotfix carve-out label must survive the mechanism change"
