"""Structural assertions for the context-staleness surfaces in docs-drift.yml.

The staleness numbers are derived purely from git history, so the workflow
wiring is load-bearing in ways YAML will not enforce by itself: the checkout
must fetch full history (a shallow clone makes every churn number wrong, and
the checker refuses to run), the compute step must stay unconditional and
advisory, the PR comment must use its own marker tag (so it can never
overwrite the docs-drift report comment), and the weekly sweep must upsert a
single tracking issue instead of stacking duplicates. These tests pin each of
those properties.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs-drift.yml"

COMPUTE_STEP = "Run context-file staleness check"
COMMENT_STEP = "Comment staleness report on the pull request"
SWEEP_STEP = "Track accumulated context-file staleness (weekly)"
MARKER_TAG = "<!-- context-staleness-report -->"


def _workflow() -> dict[str, object]:
    return cast("dict[str, object]", yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))


def _steps() -> list[dict[str, object]]:
    jobs = _workflow().get("jobs")
    assert isinstance(jobs, dict), "docs-drift.yml has no jobs mapping"
    job = jobs.get("drift-check")
    assert isinstance(job, dict), "expected a drift-check job"
    steps = job.get("steps")
    assert isinstance(steps, list), "drift-check job has no steps"
    return [step for step in steps if isinstance(step, dict)]


def _step(name: str) -> dict[str, object]:
    for step in _steps():
        if step.get("name") == name:
            return step
    pytest.fail(f"docs-drift.yml has no {name!r} step")


def test_checkout_fetches_full_history() -> None:
    """History-derived churn needs the whole history, not a shallow clone."""
    for step in _steps():
        uses = step.get("uses")
        if isinstance(uses, str) and uses.startswith("actions/checkout@"):
            with_block = step.get("with")
            assert isinstance(with_block, dict), "checkout step has no with: block"
            assert with_block.get("fetch-depth") == 0, (
                "docs-drift checkout must set fetch-depth: 0 - the staleness "
                "checker refuses shallow clones rather than emit wrong numbers"
            )
            return
    pytest.fail("docs-drift.yml has no actions/checkout step")


def test_staleness_step_runs_the_script_on_every_event() -> None:
    """The compute step is unconditional and baseline-aware."""
    step = _step(COMPUTE_STEP)
    assert "if" not in step, f"{COMPUTE_STEP!r} must run on every event"
    run = step.get("run")
    assert isinstance(run, str)
    assert "scripts/check_context_staleness.py" in run
    assert "--baseline" in run, "PR runs must pass the base sha as baseline"
    assert "--strict" not in run, "the compute step is advisory; strictness belongs to the sweep's verdict"


def test_pr_comment_upsert_uses_its_own_marker_tag() -> None:
    """The comment step upserts by a tag distinct from the drift report's."""
    step = _step(COMMENT_STEP)
    condition = step.get("if")
    assert isinstance(condition, str)
    assert "pull_request" in condition
    assert "new_flags" in condition, "the comment must key on flags the PR itself introduced"
    assert step.get("continue-on-error") is True, "a 403 on the comment API must not fail the job"

    with_block = step.get("with")
    assert isinstance(with_block, dict)
    script = with_block.get("script")
    assert isinstance(script, str)
    assert MARKER_TAG in script
    assert "<!-- docs-drift-report -->" not in script, "must not upsert into the docs-drift comment"
    assert "updateComment" in script and "createComment" in script, "expected an upsert, not append-only comments"


def test_weekly_sweep_upserts_a_single_tracking_issue() -> None:
    """Scheduled runs update one tracking issue; they never stack duplicates."""
    step = _step(SWEEP_STEP)
    assert step.get("if") == "github.event_name == 'schedule'"
    run = step.get("run")
    assert isinstance(run, str)
    assert "gh issue list" in run, "must look for an existing tracking issue first"
    assert "gh issue edit" in run and "gh issue create" in run
    assert "context-file staleness" in run, "the tracking-issue title anchors the upsert search"
    assert "heredoc" not in run and "<<EOF" not in run, (
        "the issue body must be assembled with printf - the report contains "
        "backticks, which an unquoted heredoc expands as command substitutions"
    )


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
    assert "schedule" in triggers, "the weekly sweep needs the schedule trigger"
