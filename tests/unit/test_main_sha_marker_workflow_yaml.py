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


def test_ci_concurrency_collapses_main_pushes_and_exempts_release_bumps() -> None:
    """Superseded main pushes collapse; release bumps and merge groups do not.

    A merge-queue batch landing several PRs stacks one full-matrix main run
    per merged commit against the account-wide concurrent-job ceiling; the
    stacked runs starve the merge_group runs the queue itself waits on. The
    newest main push therefore supersedes the queued/running ones (branch-wide
    group, cancel-in-progress) — except a release bump commit, whose own CI
    run is what the auto-release gate keys on, so it keeps a per-SHA group
    that nothing cancels. The per-commit signal for superseded commits is
    carried by the exact-SHA marker workflow, asserted separately below.
    """
    ci_doc = _load(CI)
    concurrency = _mapping(ci_doc.get("concurrency"), "CI workflow must define concurrency")
    group = concurrency.get("group")
    cancel = concurrency.get("cancel-in-progress")
    assert isinstance(group, str)
    assert isinstance(cancel, str)

    # PR runs keep their per-PR group and stay cancellable.
    assert "pr-" in group
    assert "pull_request" in cancel

    # Non-release main pushes share one branch-wide group and supersede each
    # other; both halves of the policy must name the same release exemption.
    for expr in (group, cancel):
        assert "github.event_name == 'push'" in expr
        assert "chore(release)" in expr
        assert "release:" in expr

    # merge_group / dispatch / release bumps fall through to the per-SHA
    # group, which nothing cancels.
    assert "github.sha" in group
    assert "merge_group" not in cancel

    text = CI.read_text(encoding="utf-8")
    # The comment must keep naming the marker workflow as the per-commit
    # signal and the queue-starvation incident that motivated the collapse.
    assert "main-sha-marker" in text
    assert "concurrent-job ceiling" in text


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
