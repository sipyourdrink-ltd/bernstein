"""Structural assertions for the CodeQL lane's triggers.

CodeQL analysed every pull request 517 times in one week for 2 165
job-minutes and gated nothing: branch protection on `main` requires `CI gate`
and `shipped bundle matches the lockfile`, and CodeQL is neither. Every commit
that lands is analysed again by the push run minutes later, so the
pull-request run was a second analysis of the same code whose only output was
an advisory annotation (#5795).

The trigger set is the whole of that decision and nothing red appears when it
regresses -- re-adding `pull_request` restores 2 165 job-minutes a week and
still gates nothing. It is decided by static structure, so it is asserted
statically here, in the shape `cifuzz-weekly` uses for the same reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "codeql.yml"


def _doc() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{WORKFLOW.name} is not a mapping"
    return cast("dict[str, Any]", data)


def _triggers() -> dict[str, Any]:
    doc = _doc()
    # PyYAML resolves a bare `on:` key to the boolean True.
    triggers = doc.get(True, doc.get("on"))
    assert isinstance(triggers, dict), "the `on:` block is not a mapping"
    return cast("dict[str, Any]", triggers)


def test_workflow_file_exists() -> None:
    assert WORKFLOW.is_file()


def test_there_is_no_pull_request_trigger() -> None:
    """The advisory pull-request run was dropped; re-adding it costs the same again."""
    assert "pull_request" not in _triggers()
    assert "pull_request_target" not in _triggers()


def test_the_trunk_and_the_schedule_both_still_analyse() -> None:
    """Dropping the PR run is only defensible while these two remain.

    The trunk run is what makes "surfaces after the merge rather than before
    it" a latency cost rather than a coverage loss; the weekly run catches
    findings that come from a new query version rather than from a new commit.
    """
    triggers = _triggers()
    assert "schedule" in triggers
    push = triggers.get("push")
    assert isinstance(push, dict), "the push trigger is not a mapping"
    assert push.get("branches") == ["main"]


def test_analysis_is_never_cancelled_mid_run() -> None:
    """A cancelled CodeQL run leaves code scanning in a configuration-error state.

    It was previously safe to cancel a superseded *pull request* analysis, and
    the guard was written as `github.event_name == 'pull_request'`. With that
    trigger gone the expression can only be false, and leaving it in place
    would read as if some run were still cancellable.
    """
    concurrency = _doc().get("concurrency")
    assert isinstance(concurrency, dict), "concurrency is not a mapping"
    assert concurrency.get("cancel-in-progress") is False


def test_no_step_branches_on_a_pull_request_event() -> None:
    """A leftover `event_name == 'pull_request'` guard is dead and misleading.

    Comments are excluded: the file explains in prose why the trigger is gone,
    and that sentence has to be allowed to name the thing it removed.
    """
    body = WORKFLOW.read_text(encoding="utf-8").split("concurrency:", 1)[1]
    code = [line for line in body.splitlines() if not line.lstrip().startswith("#")]
    offenders = [line.strip() for line in code if "pull_request" in line]
    assert offenders == []
