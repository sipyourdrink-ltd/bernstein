"""Structural assertions on ``static-analysis (extended)``.

``.github/workflows/static-analysis-extended.yml`` runs the full-width
scanner surface (Semgrep, Trivy filesystem, Trivy IaC) on the merge
queue's ephemeral branch, so the whole suite gates a merge without
running on every pull-request push.

The failure this module exists to prevent
-----------------------------------------
Code Scanning attaches alerts to the ref a run was for. A merge-queue
run's ref is ``gh-readonly-queue/main/pr-<n>-<sha>``, which GitHub
deletes the moment the entry merges or the queue re-shuffles. These
scanners take minutes; the upload regularly landed *after* the branch
was gone, and the API answered::

    ref 'refs/heads/gh-readonly-queue/main/pr-4012-1c50b8e...' not found

That failed the job over a report with nowhere to go while the scan
itself had passed. Because the lane is a required context, the false
failure ejected the pull request from the queue and jammed every entry
behind it -- observed on #4002 and #4012 on 2026-08-16, minutes apart,
which is what identified the cause as the ref lifetime rather than
anything in either diff.

Uploading on ``push`` to ``main`` covers the alerts that matter, against
a ref that persists.

Invariants exercised here:

1. Every Code Scanning upload is skipped on ``merge_group``.
2. Those uploads keep ``always()``, so a scanner that exits non-zero on
   a push or schedule run still reports what it found.
3. The scanner steps themselves carry no event condition. Skipping the
   scan on the merge queue -- rather than only its reporting -- would
   turn the gate into a formality.
4. Every job that uploads SARIF to Code Scanning also keeps the raw
   SARIF as a workflow artifact, so a merge-queue run's results are
   still recoverable with no Security tab entry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "static-analysis-extended.yml"
)

CODE_SCANNING_ACTION = "github/codeql-action/upload-sarif"
ARTIFACT_ACTION = "actions/upload-artifact"


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _jobs() -> dict[str, Any]:
    return _workflow()["jobs"]


def _steps_using(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if action in str(step.get("uses", ""))]


def _upload_steps() -> list[tuple[str, dict[str, Any]]]:
    return [
        (name, step)
        for name, job in _jobs().items()
        for step in _steps_using(job, CODE_SCANNING_ACTION)
    ]


def test_workflow_runs_on_the_merge_queue() -> None:
    """The premise of every other assertion here."""
    triggers = _workflow()[True]  # PyYAML reads a bare `on:` key as True.

    assert "merge_group" in triggers
    assert "main" in triggers["push"]["branches"]


def test_code_scanning_uploads_exist() -> None:
    """Guard the guards: a rename would make the rest vacuously pass."""
    assert len(_upload_steps()) == 6


@pytest.mark.parametrize(
    ("job_name", "step"),
    [(name, step) for name, step in _upload_steps()],
    ids=[f"{name}:{step.get('with', {}).get('category')}" for name, step in _upload_steps()],
)
def test_code_scanning_upload_is_skipped_on_the_merge_queue(
    job_name: str, step: dict[str, Any]
) -> None:
    """Invariants 1 and 2, per upload step."""
    condition = str(step.get("if", ""))

    assert "github.event_name != 'merge_group'" in condition, (
        f"{job_name}: uploading Code Scanning results from a merge-queue run "
        "targets a ref GitHub deletes on merge, so the upload fails the job "
        "after the scan already passed"
    )
    assert "always()" in condition, (
        f"{job_name}: dropping always() would stop a failing scanner from "
        "reporting what it found on push and schedule runs"
    )


@pytest.mark.parametrize("job_name", ["semgrep", "trivy-fs", "trivy-iac"])
def test_scanners_themselves_are_unconditional(job_name: str) -> None:
    """Invariant 3.

    These three are the jobs with no job-level ``if:``, which is what
    makes them run on the merge queue. The fix above is allowed to
    silence the *reporting* on that event and nothing else.
    """
    job = _jobs()[job_name]

    assert "if" not in job, (
        f"{job_name} must run on every trigger including merge_group; "
        "gating the job on the event would let a merge through unscanned"
    )

    scanner_steps = [
        step
        for step in job["steps"]
        if "run" in step and "merge_group" in str(step.get("if", ""))
    ]
    assert scanner_steps == [], (
        f"{job_name}: a scanner step is conditioned on the event. Only the "
        "Code Scanning upload may be."
    )


@pytest.mark.parametrize("job_name", sorted({name for name, _ in _upload_steps()}))
def test_raw_sarif_survives_as_an_artifact(job_name: str) -> None:
    """Invariant 4.

    With the Security tab skipped on merge-queue runs, the artifact is
    the only way back to what a queue run actually found.
    """
    artifacts = _steps_using(_jobs()[job_name], ARTIFACT_ACTION)

    assert artifacts, f"{job_name} uploads SARIF to Code Scanning but keeps no artifact"
    for step in artifacts:
        assert "always()" in str(step.get("if", "")), (
            f"{job_name}: the artifact is most needed when the scan failed"
        )
