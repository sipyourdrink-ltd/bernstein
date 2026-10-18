"""A claim held by a dead agent is released, even when the task's retry is already queued."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from bernstein.core.models import TaskStatus

from bernstein.core.orchestration.orchestrator import Orchestrator


def _orch(retried: set[str]) -> SimpleNamespace:
    """Orchestrator stand-in: no live agents, so every claim belongs to a dead one."""
    return SimpleNamespace(
        _task_to_session={},
        _agents={},
        _config=SimpleNamespace(server_url="http://server", stale_claim_timeout_s=900.0),
        _client=MagicMock(),
        _retried_task_ids=retried,
        _retry_or_fail_task=MagicMock(),
    )


def test_dead_claim_on_a_task_whose_retry_is_queued_is_failed_to_free_its_files(make_task: Any) -> None:
    """retry_or_fail_task skips a task it has already retried; the claim (and the files it
    owns, which block every other task on them) must still be released."""
    task = make_task(id="T-reopened", title="t", description="d", status=TaskStatus.CLAIMED)
    orch = _orch({"T-reopened"})

    with patch("bernstein.core.orchestration.orchestrator.fail_task") as fail:
        released = Orchestrator._release_stale_claims(orch, [task])

    assert released == 1
    orch._retry_or_fail_task.assert_not_called()
    fail.assert_called_once()
    assert fail.call_args.args[:3] == (orch._client, "http://server", "T-reopened")


def test_dead_claim_without_a_queued_retry_goes_through_the_retry_path(make_task: Any) -> None:
    task = make_task(id="T-first", title="t", description="d", status=TaskStatus.CLAIMED)
    orch = _orch(set())

    with patch("bernstein.core.orchestration.orchestrator.fail_task") as fail:
        released = Orchestrator._release_stale_claims(orch, [task])

    assert released == 1
    orch._retry_or_fail_task.assert_called_once()
    assert orch._retry_or_fail_task.call_args.args[0] == "T-first"
    fail.assert_not_called()
