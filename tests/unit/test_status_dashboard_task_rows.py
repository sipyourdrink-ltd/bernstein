"""The /status task-row contract that progress consumers count on.

The demo and quickstart polls compare progress against a seeded total,
which only works if each row names its retry-lineage root: a failed
task's retry is a NEW task with a fresh id, so rows alone double-count
retried work (issue #3433, finding 3723321829). ``lineage_id`` is that
root - ``metadata.original_task_id`` for a retry, the task's own id
otherwise.
"""

from __future__ import annotations

from bernstein.core.models import Task

from bernstein.core.routes.status_dashboard import _status_task_items


def test_status_rows_carry_the_retry_lineage_root() -> None:
    original = Task(id="orig-1", title="Fix off-by-one", description="d", role="backend")
    retry = Task(
        id="retry-1",
        title="Fix off-by-one",
        description="d",
        role="backend",
        metadata={"original_task_id": "orig-1"},
    )

    rows = _status_task_items([original, retry], now=0.0)

    assert [row["lineage_id"] for row in rows] == ["orig-1", "orig-1"]


def test_a_task_without_retry_metadata_is_its_own_lineage_root() -> None:
    task = Task(id="solo-1", title="t", description="d", role="qa")
    (row,) = _status_task_items([task], now=0.0)
    assert row["lineage_id"] == "solo-1"
