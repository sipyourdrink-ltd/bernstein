"""Tests for alive-exit /complete -> janitor pass enqueueing (defect item 30).

Before the fix the alive-exit path only ran ``verify_task`` via the
orchestrator tick's ``process_completed_tasks`` -> ``_process_single_completed_task``
chain. When the orchestrator self-stopped before the next tick, no janitor
row ever landed in ``.sdd/metrics/tasks.jsonl`` and no
``janitor_verdict_action`` log line was emitted. Attempt 83808a8a is the
canonical evidence: ``tasks.jsonl`` contained exactly one row (the manager,
which went through the dead-agent path).

After the fix:

* ``_enqueue_alive_exit_janitor_pass`` is a clearly-named standalone
  entry point that emits ``janitor: enqueued pass task=... role=...`` at
  INFO and submits ``verify_task`` (or ``run_janitor`` for ``llm_judge``
  signals) to ``orch._executor`` (or returns a sync-Future if no executor
  exists).
* ``process_completed_tasks`` calls the helper, so every tick sees the
  alive-exit janitor log line.
* ``_process_single_completed_task`` logs ``janitor: alive-exit pass
  starting`` at INFO so a silent no-op is impossible.
* The dead-exit ``handle_orphaned_task`` path is untouched.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bernstein.core.models import CompletionSignal, Task, TaskType

from bernstein.core.tasks import task_lifecycle
from bernstein.core.tasks.task_lifecycle import (
    _enqueue_alive_exit_janitor_pass,
    _process_single_completed_task,
    process_completed_tasks,
)


def _make_task(
    task_id: str = "alive-task-1",
    *,
    signals: list[CompletionSignal] | None = None,
    role: str = "backend",
) -> Task:
    task = Task(
        id=task_id,
        title="Implement cli.py hello",
        description="Add a hello subcommand to cli.py",
        role=role,
        task_type=TaskType.STANDARD,
        status=task_lifecycle.TaskStatus.DONE,
    )
    if signals is not None:
        task.completion_signals = signals
    return task


class _FakeExecutor:
    """Mimics ``concurrent.futures.ThreadPoolExecutor.submit`` for tests."""

    def __init__(self) -> None:
        self.submitted: list[tuple[Any, tuple[Any, ...]]] = []

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        self.submitted.append((fn, args, kwargs))
        return SimpleNamespace(_fn=fn, _args=args, _kwargs=kwargs)


def test_enqueue_alive_exit_janitor_logs_and_submits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A janitor pass IS enqueued when ``/complete`` is processed for an alive task.

    This is the core property for defect 30: the alive-exit path MUST
    schedule a janitor pass just like the dead-exit path schedules
    ``verify_task`` synchronously in ``handle_orphaned_task``.
    """
    executor = _FakeExecutor()
    orch: Any = SimpleNamespace(
        _executor=executor,
        _processed_done_tasks={},
        _config=SimpleNamespace(server_url="http://127.0.0.1:8052"),
        _task_to_session={},
        _workdir=Path("/tmp"),
    )
    signals = [
        CompletionSignal(type="path_exists", value="src/bernstein/cli.py"),
    ]
    task = _make_task(signals=signals)

    with caplog.at_level(logging.INFO, logger="bernstein.core.tasks.task_lifecycle"):
        future = _enqueue_alive_exit_janitor_pass(orch, task, reason="alive_exit_tick")

    assert future is not None, "janitor future must be returned for tasks with completion_signals"
    # Executor got a verify_task or run_janitor submission.
    assert len(executor.submitted) == 1
    fn, args, _ = executor.submitted[0]
    # Either verify_task (sync) or _verify_via_janitor (async) is acceptable.
    from bernstein.core.janitor import verify_task

    assert fn in (verify_task, task_lifecycle._verify_via_janitor)
    if fn is verify_task:
        # verify_task(task, workdir)
        assert args[0] is task
    else:
        # _verify_via_janitor(task, workdir, server_url)
        assert args[0] is task
    # Log line must mention task, role, reason -- visible evidence the
    # alive-exit janitor pass was scheduled.
    assert any(
        "janitor: enqueued pass" in rec.message
        and "task=alive-task-1" in rec.message
        and "role=backend" in rec.message
        and "reason=alive_exit_tick" in rec.message
        for rec in caplog.records
    ), caplog.text


def test_enqueue_alive_exit_no_signals_returns_none_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tasks without completion_signals return None and log a no_signals marker.

    Matches the existing pre-fix behavior in ``process_completed_tasks``
    (``if not task.completion_signals: continue``). The new helper returns
    None so the caller can take a different path (auto-verify) and still
    has a log line for the alive-exit enqueue observation.
    """
    executor = _FakeExecutor()
    orch: Any = SimpleNamespace(
        _executor=executor,
        _processed_done_tasks={},
        _config=SimpleNamespace(server_url="http://127.0.0.1:8052"),
        _task_to_session={},
    )
    task = _make_task(signals=[])

    with caplog.at_level(logging.INFO, logger="bernstein.core.tasks.task_lifecycle"):
        future = _enqueue_alive_exit_janitor_pass(orch, task, reason="alive_exit_drain")

    assert future is None
    assert len(executor.submitted) == 0
    assert any("no_completion_signals=true" in rec.message for rec in caplog.records), caplog.text


def test_enqueue_alive_exit_without_executor_returns_sync_future() -> None:
    """If ``orch._executor`` is missing, run verify_task inline and wrap it.

    This protects against the orchestrator self-stopping before the
    tick has scheduled a future -- the janitor still gets a synchronous
    pass and a Future-shaped result back so the caller can treat it
    identically to the executor path.
    """
    orch: Any = SimpleNamespace(
        _executor=None,
        _processed_done_tasks={},
        _config=SimpleNamespace(server_url="http://127.0.0.1:8052"),
        _task_to_session={},
        _workdir=Path("/tmp"),
    )
    task = _make_task(
        signals=[CompletionSignal(type="path_exists", value="nonexistent_path_xyz123")],
    )

    future = _enqueue_alive_exit_janitor_pass(orch, task, reason="alive_exit_no_executor")
    assert future is not None
    passed, failed = future.result()
    assert passed is False
    assert any("path_exists" in s for s in failed)


def test_process_completed_tasks_invokes_helper_for_alive_exits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``process_completed_tasks`` MUST route through the helper for each alive-exit task.

    Defends against a future refactor accidentally bypassing the new
    helper and reintroducing the silent skip.
    """
    executor = _FakeExecutor()
    orch: Any = SimpleNamespace(
        _executor=executor,
        _processed_done_tasks={},
        _config=SimpleNamespace(server_url="http://127.0.0.1:8052"),
        _task_to_session={},
        _workdir=Path("/tmp"),
        # Stub the post-enqueue hooks so the test focuses only on the
        # helper routing through `process_completed_tasks`.
        _find_session_for_task=lambda _: None,
        _wal_writer=None,
        _spawner=SimpleNamespace(
            get_worktree_path=lambda _: None,
            reap_completed_agent=lambda *a, **k: None,
            cleanup_worktree=lambda _: None,
        ),
        _record_provider_health=lambda *a, **k: None,
        _cost_tracker=SimpleNamespace(spent_usd=0.0),
        _evolution=None,
    )
    task = _make_task(
        task_id="alive-process-task",
        signals=[CompletionSignal(type="path_exists", value="irrelevant")],
    )
    result = SimpleNamespace(
        verified=[],
        verification_failures=[],
        spawned=[],
        reaped=[],
        retried=[],
        errors=[],
        dry_run_planned=[],
        open_tasks=0,
    )

    with caplog.at_level(logging.INFO, logger="bernstein.core.tasks.task_lifecycle"):
        # The post-enqueue path may fail on the minimal mock; the test
        # asserts the helper was invoked, not that it ran cleanly.
        with contextlib.suppress(Exception):
            process_completed_tasks(orch, [task], result)

    # _enqueue_alive_exit_janitor_pass ran via the helper path; the
    # helper itself submits to the executor exactly once.
    assert len(executor.submitted) == 1, executor.submitted


def test_process_single_completed_task_logs_alive_exit_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_process_single_completed_task`` must log the alive-exit pass START line.

    This is the top-of-function INFO log that makes the alive-exit
    janitor pass observable even if every downstream step (WAL write,
    metrics, reap) is skipped.
    """
    executor = _FakeExecutor()
    orch: Any = SimpleNamespace(
        _executor=executor,
        _config=SimpleNamespace(server_url="http://127.0.0.1:8052"),
        _task_to_session={},
        _find_session_for_task=lambda _: None,  # no live session
        _wal_writer=None,  # skip WAL writes -- out of scope for this test
        _spawner=SimpleNamespace(
            get_worktree_path=lambda _: None,
            reap_completed_agent=lambda *a, **k: None,
            cleanup_worktree=lambda _: None,
        ),
        _record_provider_health=lambda *a, **k: None,
        _cost_tracker=SimpleNamespace(spent_usd=0.0),
        _workdir=Path("/tmp"),
        _evolution=None,  # skip evolution
        _processed_done_tasks={},
    )
    task = _make_task(
        task_id="alive-start-task",
        signals=[CompletionSignal(type="path_exists", value="never")],
    )

    # Pre-submit a verify_task future so _resolve_janitor_result can use it.
    from bernstein.core.janitor import verify_task

    verify_future = executor.submit(verify_task, task, "/tmp")
    verify_futures = {task.id: verify_future}

    result = SimpleNamespace(
        verified=[],
        verification_failures=[],
        spawned=[],
        reaped=[],
        retried=[],
        errors=[],
        dry_run_planned=[],
        open_tasks=0,
    )

    with caplog.at_level(logging.INFO, logger="bernstein.core.tasks.task_lifecycle"):
        # After the top-of-function log any unrelated failures (no orch
        # subclasses hooked up) should not affect the pass test.
        with contextlib.suppress(Exception):
            _process_single_completed_task(orch, task, verify_futures, result)

    assert any(
        "janitor: alive-exit pass starting" in rec.message
        and "task=alive-start-task" in rec.message
        and "role=backend" in rec.message
        for rec in caplog.records
    ), caplog.text


def test_dead_path_janitor_pass_unchanged() -> None:
    """Sanity: the dead-exit ``verify_task`` call site is untouched.

    Regresses the safe carve-out: item 30 must NOT modify
    ``handle_orphaned_task`` (read-only territory).
    """
    import inspect

    from bernstein.core.agents import agent_lifecycle

    src = inspect.getsource(agent_lifecycle.handle_orphaned_task)
    # The synchronous verify_task call must still be present.
    assert "passed, failed_signals = verify_task(task, orch._workdir)" in src
