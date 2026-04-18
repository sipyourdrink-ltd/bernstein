"""Tests for Dead Letter Queue integration with retry_or_fail_task (audit-019).

Verifies that when a task exhausts its retry budget, it is enqueued into the
DLQ under ``<workdir>/.sdd/runtime/dlq.jsonl`` — instead of being silently
dropped with a ``fail_task`` call.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx

from bernstein.core.tasks.dead_letter_queue import DeadLetterQueue
from bernstein.core.tasks.task_lifecycle import retry_or_fail_task


class _MockScope:
    value = "small"


class _MockComplexity:
    value = "low"


class _MockTaskType:
    value = "feature"


class _MockTask:
    """Minimal Task stand-in matching the attributes touched by retry_or_fail_task."""

    def __init__(
        self,
        task_id: str,
        *,
        retry_count: int = 0,
        max_retries: int = 3,
    ) -> None:
        self.id = task_id
        self.title = "Test Task"
        self.description = "body"
        self.role = "backend"
        self.priority = 1
        self.scope = _MockScope()
        self.complexity = _MockComplexity()
        self.estimated_minutes = 10
        self.depends_on = []
        self.owned_files = []
        self.task_type = _MockTaskType()
        self.model = "sonnet"
        self.effort = "high"
        self.max_output_tokens = None
        self.meta_messages: list[str] = []
        self.completion_signals: list[object] = []
        self.metadata: dict[str, object] = {}
        self.retry_count = retry_count
        self.max_retries = max_retries
        self.retry_delay_s = 0.0
        self.terminal_reason = None


def _make_client() -> MagicMock:
    """Create an httpx.Client mock whose POST returns a successful response."""
    client = MagicMock(spec=httpx.Client)
    ok = MagicMock()
    ok.raise_for_status = MagicMock()
    client.post.return_value = ok
    return client


def test_task_under_threshold_is_retried_not_dlq(tmp_path: Path) -> None:
    """A task below its retry ceiling is re-queued and NOT added to the DLQ."""
    client = _make_client()
    task = _MockTask("task-alpha", retry_count=0, max_retries=3)

    retry_or_fail_task(
        task_id="task-alpha",
        reason="Agent died",
        client=client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"active": [task]},
        workdir=tmp_path,
    )

    # A retry POST should have been issued.
    posted_urls = [call.args[0] for call in client.post.call_args_list]
    assert any(url.endswith("/tasks") for url in posted_urls), f"expected retry POST to /tasks but saw {posted_urls}"

    # DLQ file should NOT exist for a task below threshold.
    dlq_file = tmp_path / ".sdd" / "runtime" / "dlq.jsonl"
    assert not dlq_file.exists(), "under-threshold task must not be enqueued to DLQ"


def test_task_at_threshold_goes_to_dlq(tmp_path: Path) -> None:
    """A task at its retry ceiling is enqueued into the DLQ with rich metadata."""
    client = _make_client()
    # retry_count == max_retries — we're past the budget.
    task = _MockTask("task-beta", retry_count=3, max_retries=3)

    retry_or_fail_task(
        task_id="task-beta",
        reason="Agent died permanently",
        client=client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"active": [task]},
        workdir=tmp_path,
    )

    # DLQ file must exist and contain exactly one entry for task-beta.
    dlq = DeadLetterQueue(sdd_dir=tmp_path / ".sdd")
    entries = dlq.list_entries()
    assert len(entries) == 1, f"expected 1 DLQ entry, got {len(entries)}"
    entry = entries[0]
    assert entry.task_id == "task-beta"
    assert entry.role == "backend"
    assert entry.reason == "max_retries_exceeded"
    assert entry.retry_count == 3
    assert "Agent died permanently" in entry.original_error
    # Metadata preserves enough context for a replay decision.
    assert entry.metadata["priority"] == 1
    assert entry.metadata["scope"] == "small"
    assert entry.metadata["complexity"] == "low"

    # The task should also have been marked failed via the HTTP fail endpoint.
    posted_urls = [call.args[0] for call in client.post.call_args_list]
    assert any(url.endswith("/task-beta/fail") for url in posted_urls), f"expected fail POST but saw {posted_urls}"
    # And NO retry POST to /tasks.
    assert not any(url.endswith("/tasks") for url in posted_urls), "exhausted task must not be re-queued"


def test_dlq_entries_are_listable_and_not_rescheduled(tmp_path: Path) -> None:
    """Tasks in the DLQ are listable, pending-only, and do not auto-retry.

    We enqueue a task directly into the DLQ and verify that:
    * ``list_entries`` returns the entry
    * ``list_entries(pending_only=True)`` includes it until marked replayed
    * ``retry_or_fail_task`` with an already-exhausted task does not create
      a new open task (no second entry into the scheduler).
    """
    dlq = DeadLetterQueue(sdd_dir=tmp_path / ".sdd")
    dlq.enqueue(
        task_id="task-gamma",
        title="Repro",
        role="qa",
        reason="max_retries_exceeded",
        retry_count=3,
        original_error="upstream 500",
    )

    entries = dlq.list_entries()
    assert len(entries) == 1
    assert entries[0].task_id == "task-gamma"
    assert entries[0].replayed is False

    # pending_only filter should include an un-replayed entry.
    pending = dlq.list_entries(pending_only=True)
    assert len(pending) == 1

    # Now simulate the scheduler re-touching an exhausted task: retry_or_fail_task
    # must NOT create a new open task — it should flow into the DLQ branch and
    # issue a fail_task call only (and append to the same DLQ file).
    client = _make_client()
    task = _MockTask("task-gamma", retry_count=3, max_retries=3)
    retry_or_fail_task(
        task_id="task-gamma",
        reason="another transient spike",
        client=client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"active": [task]},
        workdir=tmp_path,
    )

    posted_urls = [call.args[0] for call in client.post.call_args_list]
    assert not any(url.endswith("/tasks") for url in posted_urls), (
        "DLQ-bound task must not be re-queued into the scheduler"
    )
    # Must have been failed via HTTP.
    assert any(url.endswith("/task-gamma/fail") for url in posted_urls)

    # And the DLQ now has a second entry for the same task id — confirming
    # the retry path appended rather than silently dropped.
    dlq2 = DeadLetterQueue(sdd_dir=tmp_path / ".sdd")
    all_entries = dlq2.list_entries()
    assert len(all_entries) == 2
    assert {e.task_id for e in all_entries} == {"task-gamma"}


def test_workdir_none_preserves_legacy_fail_path(tmp_path: Path) -> None:
    """Without a workdir, exhausted tasks still fail — just without DLQ persistence.

    This guards the opt-in shape of the API: callers that omit ``workdir``
    keep the pre-audit-019 behaviour (fail_task only, no file writes).
    """
    client = _make_client()
    task = _MockTask("task-delta", retry_count=3, max_retries=3)

    retry_or_fail_task(
        task_id="task-delta",
        reason="boom",
        client=client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"active": [task]},
        # workdir intentionally omitted
    )

    posted_urls = [call.args[0] for call in client.post.call_args_list]
    assert any(url.endswith("/task-delta/fail") for url in posted_urls)
    # No DLQ file because we passed no workdir.
    assert not (tmp_path / ".sdd" / "runtime" / "dlq.jsonl").exists()
