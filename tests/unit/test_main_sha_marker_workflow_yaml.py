"""Structural assertions for main push exact-SHA marking."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict, cast

import yaml


class WorkflowJob(TypedDict, total=False):
    name: object


class WorkflowFile(TypedDict, total=False):
    name: object
    on: object
    concurrency: object
    jobs: dict[str, WorkflowJob]


CI = Path(".github/workflows/ci.yml")
MARKER = Path(".github/workflows/main-sha-marker.yml")
MARKER_WORKFLOW_NAME = "Main SHA marker"
CI_GATE_CHECK_NAME = "CI gate"


def _load(path: Path) -> WorkflowFile:
    return cast("WorkflowFile", yaml.safe_load(path.read_text(encoding="utf-8")))


def _on(workflow: WorkflowFile) -> dict[str, object]:
    raw_workflow = cast("dict[object, object]", workflow)
    on = raw_workflow.get(True, workflow.get("on"))
    assert isinstance(on, dict), "workflow must have an `on:` mapping"
    return cast("dict[str, object]", on)


def _mapping(value: object, message: str) -> dict[str, object]:
    assert isinstance(value, dict), message
    return cast("dict[str, object]", value)


def test_ci_concurrency_keys_main_by_sha_and_cancels_only_prs() -> None:
    """Heavy CI runs every merged main commit to completion (per-SHA group, no
    cancel) while still cancelling superseded PR-branch runs, so an already-merged
    commit never carries a permanent "cancelled" red."""
    ci_doc = _load(CI)
    concurrency = _mapping(ci_doc.get("concurrency"), "CI workflow must define concurrency")
    # cancel-in-progress fires only for pull_request events; pushes to main
    # (and merge_group / dispatch) are never cancelled by a later merge.
    assert concurrency.get("cancel-in-progress") == "${{ github.event_name == 'pull_request' }}"
    group = concurrency.get("group")
    assert isinstance(group, str)
    # PR runs group by PR number; everything else groups by ref + sha so each
    # commit is its own non-cancelling run.
    assert "pr-" in group
    assert "branch-" in group
    assert "github.sha" in group

    text = CI.read_text(encoding="utf-8")
    # The comment documents the per-SHA main policy and its durable follow-up.
    assert "per-SHA group" in text
    assert "cancel-in-progress=false" in text
    assert "merge queue" in text


def test_main_sha_marker_workflow_is_exact_sha_and_non_cancellable() -> None:
    """Main pushes need a cheap exact-SHA marker independent of heavy CI cancellation."""
    assert MARKER.exists(), "main-sha-marker.yml must exist"
    marker = _load(MARKER)

    assert marker.get("name") == MARKER_WORKFLOW_NAME
    assert marker.get("name") != "CI"

    on = _on(marker)
    push = _mapping(on.get("push"), "marker workflow must define push trigger")
    assert push.get("branches") == ["main"]
    assert "paths-ignore" not in push

    concurrency = _mapping(marker.get("concurrency"), "marker workflow must define concurrency")
    group = concurrency.get("group")
    assert isinstance(group, str)
    assert "github.sha" in group
    assert "github.ref" not in group
    assert concurrency.get("cancel-in-progress") is False


def test_main_sha_marker_check_name_is_distinct_from_ci_gate() -> None:
    """The marker must not satisfy or collide with the required CI gate context."""
    marker = _load(MARKER)
    jobs = marker.get("jobs", {})
    assert isinstance(jobs, dict)
    assert jobs

    for key, job in jobs.items():
        assert isinstance(job, dict), f"job `{key}` must be a mapping"
        assert job.get("name") != CI_GATE_CHECK_NAME

    assert any(job.get("name") == MARKER_WORKFLOW_NAME for job in jobs.values() if isinstance(job, dict))
