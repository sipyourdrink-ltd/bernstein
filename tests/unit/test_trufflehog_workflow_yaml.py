"""Structural assertions for the secret-scanning gate.

The gate is only worth having if a red run means something. Two settings
decide that, and both are the kind of thing that gets widened under time
pressure rather than by decision, so they are pinned here:

* it reports verified results only - the unverified stream is dominated by
  test-fixture connection strings, and gating on it turned main red without
  a leak;
* it excludes exactly one detector. ``lob`` is excluded because its verifier
  returns a positive for any ``test_``-prefixed string of exactly 35 further
  characters, so it fires on ordinary Python test names and on test
  filenames quoted in prose. A detector that cannot separate a credential
  from a docstring contributes nothing, but the argument stops there - it
  does not extend to detectors for services this project actually uses.

The second point is why this file exists. ``--exclude-detectors`` takes a
comma-separated list, so silencing a genuine finding is a one-word edit that
reads exactly like the justified exclusion already present. Adding a name
here has to be a deliberate act with a reason attached, not a way to get a
build green.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "trufflehog.yml"

#: The only detector this project may silence, and the reason it may.
#:
#: ``lob`` verifies any ``test_`` + 35 characters as a live key, a shape
#: ordinary test names hit. There is no Lob integration here, so nothing is
#: lost. Extending this set means a real detector is being turned off.
ALLOWED_EXCLUDED_DETECTORS = frozenset({"lob"})


def _doc() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{WORKFLOW.name} is not a mapping"
    return cast("dict[str, Any]", data)


def _scan_step() -> dict[str, Any]:
    jobs = _doc().get("jobs")
    assert isinstance(jobs, dict), "workflow must declare jobs"
    steps = [
        step
        for job in jobs.values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if isinstance(step, dict) and "trufflehog" in str(step.get("uses", ""))
    ]
    assert len(steps) == 1, f"expected exactly one trufflehog step, found {len(steps)}"
    return steps[0]


def _extra_args() -> str:
    return str(_scan_step().get("with", {}).get("extra_args", ""))


def _excluded_detectors() -> frozenset[str]:
    for arg in _extra_args().split():
        if arg.startswith("--exclude-detectors="):
            _, _, value = arg.partition("=")
            return frozenset(part.strip().lower() for part in value.split(",") if part.strip())
    return frozenset()


def test_gate_reports_verified_results_only() -> None:
    """Unverified results are noise here; gating on them is a red main."""
    assert "--results=verified" in _extra_args(), "the gate must report verified results only, or it fails on fixtures"


def test_no_detector_is_excluded_without_a_recorded_reason() -> None:
    """Silencing a detector is a one-word edit - it should not be a quiet one."""
    excluded = _excluded_detectors()
    unexpected = excluded - ALLOWED_EXCLUDED_DETECTORS
    assert not unexpected, (
        f"{sorted(unexpected)} excluded from secret scanning without a reason "
        "recorded here. If the detector genuinely cannot distinguish a "
        "credential from this repository's own source, say so in "
        "ALLOWED_EXCLUDED_DETECTORS and in the workflow comment. If it is "
        "excluded to make a build green, fix the finding instead."
    )


def test_the_known_false_positive_detector_stays_excluded() -> None:
    """Removing it turns any 35-character test name into a red main."""
    assert "lob" in _excluded_detectors(), (
        "`lob` verifies any `test_` + 35 characters as a live key; without "
        "this exclusion a test name of that length fails the scan"
    )


def test_the_scan_still_runs_on_main_and_pull_requests() -> None:
    """An exclusion is only defensible while the gate itself still runs."""
    doc = _doc()
    # PyYAML 1.1 parses a bare ``on:`` key as the boolean True.
    triggers = doc.get(True, doc.get("on"))
    assert isinstance(triggers, dict), "workflow must declare a mapping of triggers"
    assert "pull_request" in triggers, "the gate must run before a merge"
    assert "push" in triggers, "the gate must run on what actually landed"
