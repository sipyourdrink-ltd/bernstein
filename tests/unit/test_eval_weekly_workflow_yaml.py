"""Structural assertions for eval-weekly's name/cadence agreement.

The workflow file and its ``name:`` said "eval-nightly" while the cron
fired weekly (Sundays only). The schedule was deliberately moved off a
daily cadence to stop competing with PR verdicts for the same 20-slot
runner pool; the nightly branding never caught up (issue #3950).
Renamed rather than rescheduled - reverting to nightly would reintroduce
the pool contention the original move fixed.

Pinned here: the display name, concurrency group, and artifact names all
read "weekly", and the old "nightly" file is gone rather than
duplicated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS_DIR / "eval-weekly.yml"


def _doc() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{WORKFLOW.name} is not a mapping"
    return cast("dict[str, Any]", data)


def test_workflow_file_exists() -> None:
    assert WORKFLOW.is_file()


def test_the_old_nightly_filename_is_gone() -> None:
    """A duplicate under the old name would run the eval suite twice a week."""
    assert not (WORKFLOWS_DIR / "eval-nightly.yml").exists(), (
        "eval-nightly.yml must not coexist with eval-weekly.yml - rename in place, don't fork the workflow"
    )


def test_display_name_matches_the_actual_cadence() -> None:
    assert _doc().get("name") == "eval-weekly"


def test_cron_is_weekly() -> None:
    doc = _doc()
    triggers = doc.get(True, doc.get("on"))
    assert isinstance(triggers, dict), "workflow must declare a mapping of triggers"
    schedule = triggers.get("schedule")
    assert isinstance(schedule, list) and len(schedule) == 1, "expected exactly one schedule entry"
    cron = str(schedule[0].get("cron", ""))
    assert cron.split()[-1] != "*", f"cron {cron!r} fires daily, not weekly"


def test_no_workflow_level_identifier_still_says_nightly() -> None:
    """Every identifier the workflow itself owns must read 'weekly'.

    Covers the display name, the concurrency group, job and step names,
    and upload-artifact names - the surfaces an operator reads in the
    Actions UI or greps for when chasing a run.

    The `.sdd/backlog/closed/...-nightly.md` reference in the header
    comment is a historical ticket filename and is intentionally left
    alone - only the workflow's own identifiers are checked here.
    """
    doc = _doc()
    assert "nightly" not in str(doc.get("name", "")).lower()

    concurrency = doc.get("concurrency", {})
    assert isinstance(concurrency, dict)
    assert "nightly" not in str(concurrency.get("group", "")).lower()

    jobs = doc.get("jobs", {})
    assert isinstance(jobs, dict)
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        job_name = str(job.get("name", ""))
        assert "nightly" not in job_name.lower(), f"job {job_id!r} name {job_name!r} still says nightly"
        for step in job.get("steps", []):
            if not isinstance(step, dict):
                continue
            step_name = str(step.get("name", ""))
            assert "nightly" not in step_name.lower(), f"step name {step_name!r} still carries the old nightly branding"
            if "upload-artifact" not in str(step.get("uses", "")):
                continue
            artifact_name = str(step.get("with", {}).get("name", ""))
            assert "nightly" not in artifact_name.lower(), (
                f"artifact name {artifact_name!r} still carries the old nightly branding"
            )
