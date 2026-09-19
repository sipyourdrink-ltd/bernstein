"""A run whose tasks were all archived must still be able to end.

Step-8b quiescence only self-stops once at least one task has reached a terminal
state -- a guard against ending a brand-new run that reads `open=0 agents=0`
because nothing has been scheduled yet. That check read `done` and `failed` and
nothing else, and a verified task is archived out of `done` into `closed`. On a
run whose tasks all completed and were archived it therefore answered "nothing
has run" on every tick: `open=0 agents=0`, `done 0->0, failed 0->0`, no
self-stop, and no `run_completed` or `run_quiescence` in the journal. A finished
run with no ending (#5968).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from bernstein.core.tick_pipeline import fetch_all_tasks

from bernstein.core.orchestration.orchestrator import has_terminal_task


def _raw(task_id: str, status: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "description": "d",
        "role": "backend",
        "status": status,
        "scope": "medium",
        "complexity": "medium",
        "task_type": "standard",
    }


def _client(rows: list[dict[str, Any]]) -> MagicMock:
    client = MagicMock()
    client.get.return_value.json.return_value = rows
    client.get.return_value.raise_for_status.return_value = None
    return client


class TestFetchAllTasksExposesClosed:
    def test_closed_is_a_default_bucket(self) -> None:
        """The key exists even when nothing is archived yet.

        A caller deciding whether any task reached a terminal state cannot tell
        an absent key from an empty one, so the guarantee has to be the key.
        """
        by_status = fetch_all_tasks(_client([_raw("T-open", "open")]), "http://server")

        assert by_status["closed"] == []

    def test_closed_tasks_land_in_it(self) -> None:
        by_status = fetch_all_tasks(_client([_raw("T-archived", "closed")]), "http://server")

        assert [task.id for task in by_status["closed"]] == ["T-archived"]
        assert by_status["done"] == []
        assert by_status["failed"] == []

    def test_an_explicit_status_list_is_still_honoured(self) -> None:
        """The default changed; the parameter did not."""
        by_status = fetch_all_tasks(_client([_raw("T-open", "open")]), "http://server", statuses=["open"])

        assert "closed" not in by_status


class TestQuiescenceSelfStopRecognizesClosedTasks:
    """The orchestrator's own predicate, not a copy of it."""

    def test_quiescence_self_stop_recognizes_closed_tasks(self) -> None:
        """An archived task is work that ran, so the run is eligible to stop."""
        by_status = fetch_all_tasks(_client([_raw("T-archived", "closed")]), "http://server")

        assert has_terminal_task(by_status) is True

    def test_an_empty_backlog_is_still_not_eligible(self) -> None:
        """The guard this predicate exists for must survive the change.

        `open=0 agents=0` on tick #1 of an empty backlog is "nothing has been
        scheduled", not "the run finished", and self-stopping there would end
        the orchestrator before it ever did anything.
        """
        by_status = fetch_all_tasks(_client([]), "http://server")

        assert has_terminal_task(by_status) is False

    def test_an_open_backlog_is_not_eligible_either(self) -> None:
        by_status = fetch_all_tasks(_client([_raw("T-open", "open")]), "http://server")

        assert has_terminal_task(by_status) is False
