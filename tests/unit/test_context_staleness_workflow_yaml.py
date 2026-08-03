"""Structural assertions for the context-staleness surfaces in docs-drift.yml.

The staleness numbers are derived purely from git history, so the workflow
wiring is load-bearing in ways YAML will not enforce by itself: the checkout
must fetch full history (a shallow clone makes every churn number wrong, and
the checker refuses to run), the compute step must stay unconditional and
advisory, the PR comment must use its own marker tag (so it can never
overwrite the docs-drift report comment), and the weekly sweep must upsert a
single tracking issue instead of stacking duplicates.

The job split is equally load-bearing: the compute job executes checker
scripts from the event's ref - PR-controlled code on pull_request events -
so it must hold contents: read only, while the publish job holds the write
permissions and must never execute repository code. These tests pin each of
those properties.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs-drift.yml"

COMPUTE_JOB = "drift-check"
PUBLISH_JOB = "drift-publish"
COMPUTE_STEP = "Run context-file staleness check"
COMMENT_STEP = "Comment staleness report on the pull request"
SWEEP_STEP = "Track accumulated context-file staleness (weekly)"
FRESHNESS_SWEEP_STEP = "Track stale data-freshness markers (weekly)"
MARKER_TAG = "<!-- context-staleness-report -->"


def _workflow() -> dict[str, object]:
    return cast("dict[str, object]", yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))


def _job(job_id: str) -> dict[str, object]:
    jobs = _workflow().get("jobs")
    assert isinstance(jobs, dict), "docs-drift.yml has no jobs mapping"
    job = jobs.get(job_id)
    assert isinstance(job, dict), f"expected a {job_id} job"
    return job


def _steps(job_id: str) -> list[dict[str, object]]:
    steps = _job(job_id).get("steps")
    assert isinstance(steps, list), f"{job_id} job has no steps"
    return [step for step in steps if isinstance(step, dict)]


def _step(job_id: str, name: str) -> dict[str, object]:
    for step in _steps(job_id):
        if step.get("name") == name:
            return step
    pytest.fail(f"docs-drift.yml job {job_id!r} has no {name!r} step")


def test_checkout_fetches_full_history() -> None:
    """History-derived churn needs the whole history, not a shallow clone."""
    for step in _steps(COMPUTE_JOB):
        uses = step.get("uses")
        if isinstance(uses, str) and uses.startswith("actions/checkout@"):
            with_block = step.get("with")
            assert isinstance(with_block, dict), "checkout step has no with: block"
            assert with_block.get("fetch-depth") == 0, (
                "docs-drift checkout must set fetch-depth: 0 - the staleness "
                "checker refuses shallow clones rather than emit wrong numbers"
            )
            return
    pytest.fail(f"docs-drift.yml job {COMPUTE_JOB!r} has no actions/checkout step")


def test_staleness_step_runs_the_script_on_every_event() -> None:
    """The compute step is unconditional and baseline-aware."""
    step = _step(COMPUTE_JOB, COMPUTE_STEP)
    assert "if" not in step, f"{COMPUTE_STEP!r} must run on every event"
    run = step.get("run")
    assert isinstance(run, str)
    assert "scripts/check_context_staleness.py" in run
    assert "--baseline" in run, "PR runs must pass the base sha as baseline"
    assert "--strict" not in run, "the compute step is advisory; strictness belongs to the sweep's verdict"


def test_compute_job_is_read_only_and_publish_runs_no_repository_code() -> None:
    """PR-controlled checker code and the write token must never share a job.

    On pull_request events the checked-out scripts come from the PR, so
    the job that executes them holds contents: read only; the job that
    comments and edits issues downloads report artifacts and runs no
    checkout and no scripts.
    """
    compute = _job(COMPUTE_JOB)
    assert compute.get("permissions") == {"contents": "read"}, (
        f"{COMPUTE_JOB} executes PR-controlled scripts and must hold contents: read only"
    )

    publish = _job(PUBLISH_JOB)
    permissions = publish.get("permissions")
    assert isinstance(permissions, dict)
    assert permissions.get("pull-requests") == "write"
    assert permissions.get("issues") == "write"
    assert publish.get("needs") == COMPUTE_JOB

    for step in _steps(PUBLISH_JOB):
        uses = step.get("uses")
        if isinstance(uses, str):
            assert not uses.startswith("actions/checkout@"), (
                f"{PUBLISH_JOB} must not check out repository code next to its write token"
            )
        run = step.get("run")
        if isinstance(run, str):
            assert "scripts/" not in run, f"{PUBLISH_JOB} must not execute repository scripts: {step.get('name')!r}"


def test_pr_comment_upsert_uses_its_own_marker_tag() -> None:
    """The comment step upserts by a tag distinct from the drift report's."""
    step = _step(PUBLISH_JOB, COMMENT_STEP)
    condition = step.get("if")
    assert isinstance(condition, str)
    assert "pull_request" in condition
    assert "staleness_new_flags" in condition, "the comment must key on flags the PR itself introduced"
    assert step.get("continue-on-error") is True, "a 403 on the comment API must not fail the job"

    with_block = step.get("with")
    assert isinstance(with_block, dict)
    script = with_block.get("script")
    assert isinstance(script, str)
    assert MARKER_TAG in script
    assert "<!-- docs-drift-report -->" not in script, "must not upsert into the docs-drift comment"
    assert "updateComment" in script and "createComment" in script, "expected an upsert, not append-only comments"


@pytest.mark.parametrize("step_name", [SWEEP_STEP, FRESHNESS_SWEEP_STEP])
def test_weekly_sweeps_upsert_only_exact_bot_owned_tracking_issues(step_name: str) -> None:
    """Scheduled runs update one tracking issue; they never stack duplicates
    and never edit a near-miss issue that a broad title search returned."""
    step = _step(PUBLISH_JOB, step_name)
    condition = step.get("if")
    assert isinstance(condition, str)
    assert "github.event_name == 'schedule'" in condition
    run = step.get("run")
    assert isinstance(run, str)
    assert "gh issue list" in run, "must look for an existing tracking issue first"
    assert "gh issue edit" in run and "gh issue create" in run
    # The lookup must not trust result order of a broad in:title search:
    # constrain to the bot-owned tracker and require an exact title match.
    assert '--author "app/github-actions"' in run, "lookup must be constrained to bot-authored issues"
    assert "--label bot" in run, "lookup must be constrained to the bot-labeled tracker"
    assert "$2 == title" in run, "lookup must require an exact title match, not the first search hit"
    assert "<<EOF" not in run, (
        "the issue body must be assembled with printf - the report contains "
        "backticks, which an unquoted heredoc expands as command substitutions"
    )


def test_sweep_step_is_gated_on_an_explicit_staleness_verdict() -> None:
    """A crashed compute step ('unknown'/'') must never file an empty issue."""
    step = _step(PUBLISH_JOB, SWEEP_STEP)
    condition = cast("str", step.get("if"))
    assert "staleness_clean == 'False'" in condition


def test_workflow_triggers_cover_the_staleness_inputs() -> None:
    """Edits to the checker or to covered subtrees must trigger the workflow."""
    workflow = _workflow()
    # PyYAML parses the bare `on:` key as boolean True (YAML 1.1).
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict), "docs-drift.yml has no on: mapping"
    for event in ("pull_request", "push"):
        block = triggers.get(event)
        assert isinstance(block, dict), f"expected an on.{event} mapping"
        paths = block.get("paths")
        assert isinstance(paths, list), f"expected on.{event}.paths"
        assert "scripts/check_context_staleness.py" in paths
        assert "tests/**" in paths, "tests/ holds a covered context file; churn there must trigger the check"
        assert "src/**" in paths, (
            "the checker enumerates nested AGENTS.md under all of src/**; "
            "a narrower filter would let a covered subtree churn without triggering the check"
        )
    assert "schedule" in triggers, "the weekly sweep needs the schedule trigger"


def test_trigger_paths_cover_every_enumerable_context_location() -> None:
    """Every DIRECTORY_CONTEXT_ROOTS entry the checker scans is a trigger path.

    list_context_files enumerates nested AGENTS.md under a fixed allowlist of
    roots; if a root is scanned but not watched, a change under it is only
    noticed by the weekly schedule. This pins filter scope to checker scope.
    """
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "check_context_staleness_trigger_pin", REPO_ROOT / "scripts" / "check_context_staleness.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)

    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    for event in ("pull_request", "push"):
        block = cast("dict[str, object]", triggers[event])
        paths = cast("list[str]", block["paths"])
        for root in module.DIRECTORY_CONTEXT_ROOTS:
            assert f"{root}/**" in paths, f"checker scans {root}/** but on.{event}.paths does not watch it"


def test_compute_failure_still_uploads_the_report_artifact() -> None:
    """A failed compute step must never suppress the report hand-off.

    drift-publish's first action is download-artifact; without always() on
    the upload, any earlier non-zero step (e.g. a crashing checker) would
    skip the upload and take all publishing down with it.
    """
    step = _step(COMPUTE_JOB, "Upload drift report")
    assert step.get("if") == "always()", "the report upload must run even when an earlier step failed"


def test_soft_freshness_path_cannot_kill_the_step() -> None:
    """The PR/push freshness run is advisory: its exit status is captured.

    Under plain set -e a non-zero exit from the checker would terminate the
    step before rc=0 is recorded and before the artifact upload, which is
    exactly the suppression the always() guard and this capture exist to
    prevent - strictness stays confined to the schedule branch.
    """
    step = _step(COMPUTE_JOB, "Run data-freshness check (soft on PR + push, strict weekly)")
    run = cast("str", step.get("run"))
    soft_branch = run.split('"schedule"', 1)[1].split("fi", 1)[0]
    assert "set +e" in soft_branch, "the soft branch must not run the checker under set -e"
    assert "RC=$?" in soft_branch, "the soft branch must capture the checker's exit status"
