"""The merge queue runs required checks; these lanes are not among them.

The only context the ``main`` merge queue requires is ``CI gate``. Three
workflows nevertheless triggered on ``merge_group``:

- ``static-analysis-extended.yml`` (Semgrep, Trivy fs, Trivy IaC)
- ``mutation-fixed.yml`` (mutmut over the fixed critical-path modules)
- ``adapter-contract-drift.yml`` (upstream CLI capability checks)

None of them could gate a queue merge. What they did instead was compete
with ``ci.yml`` for the runner pool on every queue entry and every queue
re-shuffle -- and a re-shuffle re-creates every entry's branch, so one
dequeue near the head could fan out dozens of runs. On 2026-08-16 the
queue sat for half an hour with ~50 runs queued against ~7 executing,
while the check it was actually waiting for could not start.

static-analysis had a second failure mode on top: Code Scanning attaches
alerts to the ref the run was for, and a merge-queue ref
(``gh-readonly-queue/main/pr-<n>-<sha>``) is deleted the moment its entry
merges or the queue re-shuffles. The scanners take minutes, so the SARIF
upload regularly landed after the branch was gone and the API answered
``ref ... not found`` -- failing the job over a report with nowhere to
go, after the scan itself had passed (#4002, #4012, #4014, minutes
apart, unrelated diffs).

So: none of the three triggers on ``merge_group``. Coverage moves to
where each lane's signal actually lives -- push-to-main for the
scanners (alerts against a ref that persists), the weekly cron for
mutation, the daily cron for upstream drift.

Invariants exercised here:

1. None of the three workflows triggers on ``merge_group``.
2. static-analysis still runs on push to ``main`` -- dropping the queue
   trigger without keeping this one would silence the scanners
   entirely.
3. Each lane keeps the schedule its coverage argument rests on.
4. static-analysis uploads to Code Scanning under ``always()``, so a
   failing scanner still reports what it found.
5. Every job that uploads SARIF also keeps the raw file as a workflow
   artifact, under ``always()``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

STATIC_ANALYSIS = "static-analysis-extended.yml"
NEVER_ON_MERGE_GROUP = (
    STATIC_ANALYSIS,
    "mutation-fixed.yml",
    "adapter-contract-drift.yml",
)

CODE_SCANNING_ACTION = "github/codeql-action/upload-sarif"
ARTIFACT_ACTION = "actions/upload-artifact"


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(name: str) -> dict[str, Any]:
    workflow = _load(name)
    # PyYAML 1.1 parses a bare `on:` key as the boolean True.
    triggers = workflow.get(True, workflow.get("on"))
    assert isinstance(triggers, dict), f"{name} must declare a mapping of triggers"
    return triggers


def _steps_using(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if action in str(step.get("uses", ""))]


@pytest.mark.parametrize("name", NEVER_ON_MERGE_GROUP)
def test_lane_does_not_run_on_the_merge_queue(name: str) -> None:
    """Invariant 1: non-required lanes stay out of the queue's runner pool."""
    assert "merge_group" not in _triggers(name), (
        f"{name} is not a required context on main, so a merge_group trigger "
        "cannot gate anything -- it can only starve `CI gate` of runners"
    )


def test_static_analysis_still_covers_main() -> None:
    """Invariant 2."""
    triggers = _triggers(STATIC_ANALYSIS)

    assert "push" in triggers
    assert "main" in triggers["push"]["branches"]


@pytest.mark.parametrize("name", NEVER_ON_MERGE_GROUP)
def test_lane_keeps_its_schedule(name: str) -> None:
    """Invariant 3: the cadence each lane's coverage argument rests on."""
    assert "schedule" in _triggers(name), (
        f"{name} lost its schedule; without it, dropping merge_group leaves the lane running never"
    )


def _upload_steps() -> list[tuple[str, dict[str, Any]]]:
    jobs = _load(STATIC_ANALYSIS)["jobs"]
    return [(name, step) for name, job in jobs.items() for step in _steps_using(job, CODE_SCANNING_ACTION)]


def test_code_scanning_uploads_exist() -> None:
    """Guard the guards: a rename would make the rest vacuously pass."""
    assert len(_upload_steps()) == 6


@pytest.mark.parametrize(
    ("job_name", "step"),
    _upload_steps(),
    ids=[f"{name}:{step.get('with', {}).get('category')}" for name, step in _upload_steps()],
)
def test_upload_reports_even_when_the_scan_fails(job_name: str, step: dict[str, Any]) -> None:
    """Invariant 4."""
    assert "always()" in str(step.get("if", "")), (
        f"{job_name}: without always(), a scanner that exits non-zero on a push or schedule run reports nothing"
    )


@pytest.mark.parametrize("job_name", sorted({name for name, _ in _upload_steps()}))
def test_raw_sarif_survives_as_an_artifact(job_name: str) -> None:
    """Invariant 5."""
    artifacts = _steps_using(_load(STATIC_ANALYSIS)["jobs"][job_name], ARTIFACT_ACTION)

    assert artifacts, f"{job_name} uploads SARIF to Code Scanning but keeps no artifact"
    for step in artifacts:
        assert "always()" in str(step.get("if", "")), f"{job_name}: the artifact is most needed when the scan failed"
