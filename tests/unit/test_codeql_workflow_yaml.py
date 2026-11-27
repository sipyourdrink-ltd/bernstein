"""Structural assertions for the CodeQL lane's triggers.

#5795 dropped the `pull_request` trigger to save job-minutes, on the grounds
that the push run re-analyses every merged commit. The cost of that was that a
new finding surfaced on the trunk after the merge, and that the head commit of
a merged pull request carried no code-scanning result at all. The trigger is
back, with no path filter, so every pull request into `main` is analysed on
its own head commit. The analysis stays advisory: branch protection does not
require it.

The trigger set and the cancellation rule are decided by static structure, so
they are asserted statically here, in the shape `cifuzz-weekly` uses for the
same reason.
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


def test_every_pull_request_into_main_is_analysed() -> None:
    """No path filter: a pull request outside the filtered paths would be unanalysed."""
    pull_request = _triggers().get("pull_request")
    assert isinstance(pull_request, dict), "the pull_request trigger is not a mapping"
    assert pull_request.get("branches") == ["main"]
    assert "paths" not in pull_request
    assert "paths-ignore" not in pull_request


def test_pull_request_target_is_not_used() -> None:
    """The analysis runs on untrusted head code; it must not get a privileged token."""
    assert "pull_request_target" not in _triggers()


def test_the_trunk_and_the_schedule_both_still_analyse() -> None:
    """The trunk run covers direct pushes; the weekly run catches new query versions."""
    triggers = _triggers()
    assert "schedule" in triggers
    push = triggers.get("push")
    assert isinstance(push, dict), "the push trigger is not a mapping"
    assert push.get("branches") == ["main"]


def test_only_pull_request_runs_are_cancellable() -> None:
    """A cancelled trunk run leaves code scanning in a configuration-error state.

    A superseded pull-request run may be cancelled: the run for the newer head
    commit replaces it, and that commit is the one that gets merged.
    """
    concurrency = _doc().get("concurrency")
    assert isinstance(concurrency, dict), "concurrency is not a mapping"
    assert concurrency.get("cancel-in-progress") == "${{ github.event_name == 'pull_request' }}"
