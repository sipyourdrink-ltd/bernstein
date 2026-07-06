"""Structural assertions on the SonarQube scan workflow.

These tests pin two properties of the Sonar scan design:

    * Accurate coverage: the ``workflow_run`` path consumes the coverage
      artifact from the successful CI run for the same main commit. That
      path must not start on the raw push event or on a cancelled CI run
      before the matching CI coverage artifact exists.
    * Reliability: a daily ``schedule`` backstop lands a scan even when
      main CI runs keep getting cancelled (CI on main uses
      cancel-in-progress, so rapid merges cancel most runs before
      success and the workflow_run gate then skips). The scheduled run
      has no cancellable upstream and regenerates coverage in-job when no
      CI artifact is available.

The tests below assert the workflow has:

    * a ``workflow_run`` trigger for completed main CI runs,
    * a ``schedule`` (cron) trigger as a reliability backstop,
    * a job guard that accepts successful main CI runs, the schedule, and
      manual dispatch,
    * no direct ``push`` trigger, because that races the CI artifact,
    * a workflow-run artifact download keyed to the triggering CI run,
    * a thin coverage fallback limited to the schedule/dispatch backstop
      (never the workflow_run path, which skips rather than reporting a
      partial coverage number for main).

The tests are cheap; they parse YAML only and do not call the GitHub
API.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)


SONAR_WF = Path(".github/workflows/sonar-scan.yml")


@pytest.fixture(scope="module")
def sonar_doc() -> dict[str, object]:
    """Parse the sonar-scan workflow once per module."""
    return yaml.safe_load(SONAR_WF.read_text(encoding="utf-8"))


def test_sonar_scan_workflow_file_exists() -> None:
    """The Sonar scan workflow must exist at the expected path."""
    assert SONAR_WF.is_file(), f"Missing workflow: {SONAR_WF}"


def test_sonar_scan_triggers_after_main_ci_completion(sonar_doc: dict[str, object]) -> None:
    """Sonar scan must trigger after the main CI run reaches a terminal state.

    This avoids the push-time race where Sonar starts before the
    matching ``coverage-report`` artifact has been uploaded.
    """
    on_block = sonar_doc.get(True) or sonar_doc.get("on")
    assert isinstance(on_block, dict), f"Workflow `on:` block must be a mapping, got {type(on_block)!r}"
    workflow_run = on_block.get("workflow_run")
    assert isinstance(workflow_run, dict), "Sonar scan workflow must define a `workflow_run:` trigger"
    assert workflow_run.get("workflows") == ["CI"]
    assert workflow_run.get("branches") == ["main"]
    assert workflow_run.get("types") == ["completed"]
    assert "push" not in on_block, "Sonar scan must not race CI coverage on raw push events"


def test_sonar_scan_keeps_workflow_dispatch(sonar_doc: dict[str, object]) -> None:
    """``workflow_dispatch`` must remain so operators can bootstrap."""
    on_block = sonar_doc.get(True) or sonar_doc.get("on")
    assert isinstance(on_block, dict)
    assert "workflow_dispatch" in on_block, "Sonar scan must keep its manual dispatch trigger"


def test_sonar_scan_has_scheduled_backstop_that_regenerates_coverage(sonar_doc: dict[str, object]) -> None:
    """A daily ``schedule`` trigger must land a scan even when workflow_run scans skip.

    CI on main uses cancel-in-progress, so rapid merges cancel most main
    CI runs before success and the workflow_run gate then skips this scan.
    The scheduled run has no cancellable upstream, so it must exist and it
    must be able to produce coverage in-job (the thin fallback runs on the
    schedule path) rather than depending on a CI artifact that may never
    have been uploaded.
    """
    on_block = sonar_doc.get(True) or sonar_doc.get("on")
    assert isinstance(on_block, dict)
    schedule = on_block.get("schedule")
    assert isinstance(schedule, list) and schedule, "Sonar scan must define a schedule (cron) backstop"
    crons = [entry.get("cron") for entry in schedule if isinstance(entry, dict)]
    assert any(isinstance(cron, str) and cron.strip() for cron in crons), "schedule must declare a cron expression"

    # The cron/dispatch backstop must be able to regenerate coverage in-job
    # so a scan lands even with no upstream CI artifact.
    jobs = sonar_doc.get("jobs", {})
    assert isinstance(jobs, dict)
    scan = jobs.get("scan", {})
    assert isinstance(scan, dict)
    steps = scan.get("steps", [])
    assert isinstance(steps, list) and steps

    fallback_steps = [step for step in steps if isinstance(step, dict) and step.get("name") == "Thin coverage fallback"]
    assert len(fallback_steps) == 1, "backstop must regenerate coverage via the thin coverage fallback step"
    fallback_if = str(fallback_steps[0].get("if", ""))
    assert "github.event_name == 'schedule'" in fallback_if, (
        "The scheduled backstop must be able to regenerate coverage in-job"
    )
    fallback_run = str(fallback_steps[0].get("run", ""))
    assert "coverage xml" in fallback_run and "coverage.xml" in fallback_run, (
        "The fallback must produce coverage.xml so the scheduled scan lands a coverage number"
    )


def test_sonar_scan_job_if_accepts_successful_workflow_run_and_dispatch(sonar_doc: dict[str, object]) -> None:
    """The job-level `if` must accept successful CI runs, the schedule, and manual dispatch."""
    jobs = sonar_doc.get("jobs")
    assert isinstance(jobs, dict) and jobs, "Workflow must declare a `jobs:` block"
    scan_job = jobs.get("scan")
    assert isinstance(scan_job, dict), "Workflow must define a `scan` job"
    job_if = scan_job.get("if", "")
    assert isinstance(job_if, str)
    flat = " ".join(job_if.split())
    assert "workflow_dispatch" in flat, "Job `if` must accept workflow_dispatch events"
    # The daily schedule is the reliability backstop; it must not be gated out.
    assert "schedule" in flat, "Job `if` must accept the scheduled backstop run"
    assert "workflow_run" in flat, "Job `if` must accept workflow_run events from CI"
    assert "github.event.workflow_run.conclusion == 'success'" in flat, (
        "Workflow-run scans must ignore cancelled or failed CI runs that do not produce coverage artifacts"
    )
    assert "head_branch" in flat and "main" in flat, "Workflow-run scans must stay pinned to main"


def test_sonar_scan_downloads_workflow_run_coverage_artifact(sonar_doc: dict[str, object]) -> None:
    """Workflow-run scans must download coverage from the triggering CI run."""
    jobs = sonar_doc.get("jobs", {})
    assert isinstance(jobs, dict)
    scan = jobs.get("scan", {})
    assert isinstance(scan, dict)
    steps = scan.get("steps", [])
    assert isinstance(steps, list) and steps, "scan job must declare steps"

    download_steps = [
        step
        for step in steps
        if isinstance(step, dict)
        and step.get("name") == "Download coverage artifact (workflow_run)"
        and "actions/download-artifact" in str(step.get("uses", ""))
    ]
    assert len(download_steps) == 1
    with_block = download_steps[0].get("with") or {}
    assert isinstance(with_block, dict)
    assert with_block.get("name") == "coverage-report"
    assert with_block.get("run-id") == "${{ github.event.workflow_run.id }}"


def test_sonar_scan_limits_thin_fallback_to_manual_dispatch(sonar_doc: dict[str, object]) -> None:
    """Thin fallback must not replace missing main CI coverage on the accurate path.

    A push or workflow_run scan without the matching CI artifact must
    skip the scan instead of reporting a partial coverage number. The
    fallback is allowed only on the schedule/dispatch backstop, where no
    upstream CI run exists and regenerating coverage in-job is the whole
    point of landing a daily scan.
    """
    jobs = sonar_doc.get("jobs", {})
    assert isinstance(jobs, dict)
    scan = jobs.get("scan", {})
    assert isinstance(scan, dict)
    steps = scan.get("steps", [])
    assert isinstance(steps, list) and steps, "scan job must declare steps"

    step_names = [s.get("name", "") for s in steps if isinstance(s, dict)]
    fallback_steps = [step for step in steps if isinstance(step, dict) and step.get("name") == "Thin coverage fallback"]
    assert len(fallback_steps) == 1, f"Found steps: {step_names!r}"
    fallback_if = str(fallback_steps[0].get("if", ""))
    # Backstop paths (manual dispatch or the daily schedule) may regenerate
    # coverage; the accurate workflow_run path must never fall back.
    assert "github.event_name == 'workflow_dispatch'" in fallback_if
    assert "github.event_name == 'schedule'" in fallback_if
    assert "workflow_run" not in fallback_if

    sonar_steps = [
        step
        for step in steps
        if isinstance(step, dict) and "SonarSource/sonarqube-scan-action" in str(step.get("uses", ""))
    ]
    assert len(sonar_steps) == 1
    sonar_if = str(sonar_steps[0].get("if", ""))
    assert "steps.scan_coverage.outputs.available == 'true'" in sonar_if, (
        "Sonar scan must require a usable coverage.xml after artifact download or manual fallback"
    )


def test_sonar_scan_references_coverage_xml_in_args(sonar_doc: dict[str, object]) -> None:
    """The SonarQube scan step must point at coverage.xml so Sonar reads it."""
    jobs = sonar_doc.get("jobs", {})
    scan = jobs.get("scan", {})
    steps = scan.get("steps", [])
    sonar_step = next(
        (s for s in steps if isinstance(s, dict) and "SonarSource/sonarqube-scan-action" in (s.get("uses") or "")),
        None,
    )
    assert sonar_step is not None, "Workflow must invoke SonarSource/sonarqube-scan-action"
    args = (sonar_step.get("with") or {}).get("args", "")
    assert "sonar.python.coverage.reportPaths=coverage.xml" in args, (
        "Sonar scan args must point at coverage.xml so Python coverage is ingested"
    )


def test_sonar_scan_scope_comes_from_project_properties(sonar_doc: dict[str, object]) -> None:
    """The workflow must not clobber the canonical Sonar scope config."""
    jobs = sonar_doc.get("jobs", {})
    scan = jobs.get("scan", {})
    steps = scan.get("steps", [])
    sonar_step = next(
        (s for s in steps if isinstance(s, dict) and "SonarSource/sonarqube-scan-action" in (s.get("uses") or "")),
        None,
    )
    assert sonar_step is not None, "Workflow must invoke SonarSource/sonarqube-scan-action"
    args = (sonar_step.get("with") or {}).get("args", "")

    assert "sonar.sources=" not in args
    assert "sonar.tests=" not in args
    assert "sonar.exclusions=" not in args
    assert "sonar.coverage.exclusions=" not in args


def test_sonar_scan_revision_matches_workflow_run_head_sha(sonar_doc: dict[str, object]) -> None:
    """Workflow-run scans must report the same commit that was checked out."""
    jobs = sonar_doc.get("jobs", {})
    scan = jobs.get("scan", {})
    steps = scan.get("steps", [])
    sonar_step = next(
        (s for s in steps if isinstance(s, dict) and "SonarSource/sonarqube-scan-action" in (s.get("uses") or "")),
        None,
    )
    assert sonar_step is not None, "Workflow must invoke SonarSource/sonarqube-scan-action"
    args = (sonar_step.get("with") or {}).get("args", "")

    assert "github.event.workflow_run.head_sha" in args
