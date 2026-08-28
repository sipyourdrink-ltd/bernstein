"""Unit tests for orchestrator tick-path decision methods.

Each method under test is bound onto a minimal :class:`SimpleNamespace`
stub via :func:`types.MethodType` so the *real* implementation runs (genuine
coverage + regression catching) while only the attributes the method touches
need to be supplied.

Covered methods:

* :meth:`Orchestrator._should_trigger_manager_review` - completion / failure /
  stall trigger logic.
* :meth:`Orchestrator._check_file_overlap` - in-memory + persistent lock
  conflict detection, dead-agent filtering.
* :meth:`Orchestrator._evaluate_budget_policy` - budget gating, policy
  transitions, notification emission.
* :meth:`Orchestrator._notify` - notifier fan-out + no-op when unset.
* :meth:`Orchestrator._check_task_deadlines` - exceeded / warning / clear paths.
* :meth:`Orchestrator._check_for_incidents` - error-budget counting from
  terminal task-board state, incident blast-radius auditability.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from bernstein.core.cost_tracker import CostTracker
from bernstein.core.incident import IncidentManager
from bernstein.core.models import AgentSession, Task
from bernstein.core.slo import SLOTracker

from bernstein.core.cost.budget_actions import BudgetAction, BudgetPolicy
from bernstein.core.orchestration.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# _should_trigger_manager_review
# ---------------------------------------------------------------------------


def _review_stub(
    *,
    completions: int = 0,
    last_review_ts: float = 0.0,
) -> SimpleNamespace:
    stub = SimpleNamespace(
        _completions_since_review=completions,
        _last_review_ts=last_review_ts,
        _MANAGER_REVIEW_COMPLETION_THRESHOLD=Orchestrator._MANAGER_REVIEW_COMPLETION_THRESHOLD,
        _MANAGER_REVIEW_STALL_S=Orchestrator._MANAGER_REVIEW_STALL_S,
    )
    stub._should_trigger_manager_review = MethodType(
        Orchestrator._should_trigger_manager_review,  # type: ignore[arg-type]
        stub,
    )
    return stub


def test_should_trigger_review_on_completion_threshold() -> None:
    stub = _review_stub(completions=Orchestrator._MANAGER_REVIEW_COMPLETION_THRESHOLD)
    assert stub._should_trigger_manager_review(failed_count=0) is True


def test_should_trigger_review_below_threshold_no_failures_no_stall() -> None:
    stub = _review_stub(completions=1, last_review_ts=0.0)
    # No completions threshold, no failures, last_review_ts==0 disables stall.
    assert stub._should_trigger_manager_review(failed_count=0) is False


def test_should_trigger_review_on_any_failure() -> None:
    stub = _review_stub(completions=0)
    assert stub._should_trigger_manager_review(failed_count=1) is True


def test_should_trigger_review_on_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    # last_review_ts > 0 and now - last_review_ts >= stall threshold.
    now = 10_000.0
    stub = _review_stub(completions=0, last_review_ts=now - Orchestrator._MANAGER_REVIEW_STALL_S - 1)
    monkeypatch.setattr("bernstein.core.orchestration.orchestrator.time.time", lambda: now)
    assert stub._should_trigger_manager_review(failed_count=0) is True


def test_should_not_trigger_review_when_just_reviewed(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 10_000.0
    stub = _review_stub(completions=0, last_review_ts=now - 5.0)  # 5s ago, well inside stall window
    monkeypatch.setattr("bernstein.core.orchestration.orchestrator.time.time", lambda: now)
    assert stub._should_trigger_manager_review(failed_count=0) is False


# ---------------------------------------------------------------------------
# _check_file_overlap
# ---------------------------------------------------------------------------


def _overlap_stub(
    *,
    file_ownership: dict[str, str] | None = None,
    agents: dict[str, AgentSession] | None = None,
    lock_conflicts: list[Any] | None = None,
) -> SimpleNamespace:
    lock_manager = MagicMock()
    lock_manager.check_conflicts = MagicMock(return_value=lock_conflicts or [])
    stub = SimpleNamespace(
        _file_ownership=file_ownership or {},
        _agents=agents or {},
        _batch_sessions={},
        _task_to_session={},
        _lock_manager=lock_manager,
        _loop_detector=MagicMock(),
    )
    stub._check_file_overlap = MethodType(
        Orchestrator._check_file_overlap,  # type: ignore[arg-type]
        stub,
    )
    # The real resolver, not a stand-in: recording a wait under one key and
    # clearing it under another is exactly the bug this attribution guards
    # against, and a fake here would agree with whatever it was handed.
    stub.resolve_waiting_agent = MethodType(
        Orchestrator.resolve_waiting_agent,  # type: ignore[arg-type]
        stub,
    )
    return stub


def _task_with_files(task_id: str, files: list[str]) -> Task:
    return Task(id=task_id, title=task_id, description="", role="backend", owned_files=files)


def test_check_file_overlap_no_files_returns_false() -> None:
    stub = _overlap_stub()
    assert stub._check_file_overlap([_task_with_files("T-1", [])]) is False
    # No conflict lookup is needed when there are no owned files.
    stub._lock_manager.check_conflicts.assert_not_called()


def test_check_file_overlap_active_agent_owns_file_returns_true() -> None:
    session = AgentSession(id="A-1", role="backend")
    session.status = "working"
    stub = _overlap_stub(
        file_ownership={"src/a.py": "A-1"},
        agents={"A-1": session},
    )
    assert stub._check_file_overlap([_task_with_files("T-1", ["src/a.py"])]) is True


def test_check_file_overlap_dead_agent_does_not_block() -> None:
    session = AgentSession(id="A-1", role="backend")
    session.status = "dead"
    stub = _overlap_stub(
        file_ownership={"src/a.py": "A-1"},
        agents={"A-1": session},
    )
    # Dead agent's ownership entry must not block a new batch; falls through
    # to the persistent lock check (which returns no conflicts here).
    assert stub._check_file_overlap([_task_with_files("T-1", ["src/a.py"])]) is False


def test_check_file_overlap_persistent_lock_returns_true() -> None:
    lock = SimpleNamespace(agent_id="ghost", task_id="T-old", locked_at=100.0)
    stub = _overlap_stub(lock_conflicts=[("src/locked.py", lock)])
    assert stub._check_file_overlap([_task_with_files("T-1", ["src/locked.py"])]) is True
    stub._lock_manager.check_conflicts.assert_called_once_with(["src/locked.py"])


def test_check_file_overlap_no_owner_no_lock_returns_false() -> None:
    stub = _overlap_stub(file_ownership={}, agents={})
    assert stub._check_file_overlap([_task_with_files("T-1", ["src/free.py"])]) is False


def test_check_file_overlap_records_wait_with_real_holder_ids() -> None:
    session = AgentSession(id="A-1", role="backend")
    session.status = "working"

    # We create an agent A-2 which requests this task.
    session_waiting = AgentSession(id="A-2", role="backend", task_ids=["T-parent"])
    session_waiting.status = "working"

    lock = SimpleNamespace(agent_id="ghost", task_id="T-old", locked_at=100.0)
    stub = _overlap_stub(
        file_ownership={"src/a.py": "A-1"},
        agents={"A-1": session, "A-2": session_waiting},
        lock_conflicts=[("src/locked.py", lock)],
    )

    stub._loop_detector = MagicMock()

    # Task has parent_task_id which belongs to A-2
    task = _task_with_files("T-1", ["src/a.py", "src/locked.py"])
    task.parent_task_id = "T-parent"

    assert stub._check_file_overlap([task]) is True

    stub._loop_detector.record_lock_wait.assert_called_once_with(
        waiting_agent_id="A-2",
        wanted_files=["src/a.py", "src/locked.py"],
        held_by={"src/a.py": "A-1", "src/locked.py": "ghost"},
        lock_timestamps={"ghost": 100.0},
    )


# ---------------------------------------------------------------------------
# _evaluate_budget_policy
# ---------------------------------------------------------------------------


def _budget_stub(*, budget_usd: float, spent: float = 0.0) -> SimpleNamespace:
    tracker = CostTracker(run_id="test-budget-eval", budget_usd=budget_usd)
    if spent > 0:
        tracker.record("A-0", "T-0", "opus", 0, 0, cost_usd=spent)
    stub = SimpleNamespace(
        _cost_tracker=tracker,
        _budget_policy=BudgetPolicy.default(),
        _last_budget_action=BudgetAction.CONTINUE,
        _notify=MagicMock(),
    )
    stub._evaluate_budget_policy = MethodType(
        Orchestrator._evaluate_budget_policy,  # type: ignore[arg-type]
        stub,
    )
    return stub


def test_evaluate_budget_policy_unlimited_returns_none() -> None:
    stub = _budget_stub(budget_usd=0.0)
    assert stub._evaluate_budget_policy([]) is None
    stub._notify.assert_not_called()


def test_evaluate_budget_policy_under_threshold_continue_no_notify() -> None:
    stub = _budget_stub(budget_usd=10.0, spent=1.0)  # 10% used, well under 80%
    result = stub._evaluate_budget_policy([])
    assert result is not None
    assert result.action is BudgetAction.CONTINUE
    # No transition (CONTINUE == CONTINUE) so no notification fires.
    stub._notify.assert_not_called()


def test_evaluate_budget_policy_pause_transition_notifies() -> None:
    stub = _budget_stub(budget_usd=10.0, spent=8.5)  # 85% used => PAUSE at 0.8
    result = stub._evaluate_budget_policy([])
    assert result is not None
    assert result.action is BudgetAction.PAUSE
    # Transition CONTINUE -> PAUSE fires exactly one notification.
    assert stub._notify.call_count == 1
    event = stub._notify.call_args.args[0]
    assert event == "budget.policy.pause"
    # State is advanced so a repeat tick at the same level would not re-notify.
    assert stub._last_budget_action is BudgetAction.PAUSE


def test_evaluate_budget_policy_abort_transition_notifies() -> None:
    stub = _budget_stub(budget_usd=10.0, spent=10.0)  # 100% => ABORT at 1.0
    result = stub._evaluate_budget_policy([])
    assert result is not None
    assert result.action is BudgetAction.ABORT
    assert stub._notify.call_count == 1
    assert stub._notify.call_args.args[0] == "budget.policy.abort"


def test_evaluate_budget_policy_no_renotify_when_action_unchanged() -> None:
    stub = _budget_stub(budget_usd=10.0, spent=8.5)
    stub._last_budget_action = BudgetAction.PAUSE  # already at PAUSE
    stub._evaluate_budget_policy([])
    # Action did not change (still PAUSE) so no fresh notification.
    stub._notify.assert_not_called()


# ---------------------------------------------------------------------------
# _notify
# ---------------------------------------------------------------------------


def test_notify_no_notifier_is_silent() -> None:
    stub = SimpleNamespace(_notifier=None)
    stub._notify = MethodType(Orchestrator._notify, stub)  # type: ignore[arg-type]
    # Must not raise even though there is no notifier configured.
    stub._notify("evt", "title", "body", task_id="T-1")


def test_notify_forwards_payload_to_notifier() -> None:
    notifier = MagicMock()
    stub = SimpleNamespace(_notifier=notifier)
    stub._notify = MethodType(Orchestrator._notify, stub)  # type: ignore[arg-type]

    stub._notify("task.failed", "Task failed", "It broke", task_id="T-9", role="backend")

    assert notifier.notify.call_count == 1
    event_arg, payload = notifier.notify.call_args.args
    assert event_arg == "task.failed"
    assert payload.event == "task.failed"
    assert payload.title == "Task failed"
    assert payload.body == "It broke"
    assert payload.metadata == {"task_id": "T-9", "role": "backend"}


# ---------------------------------------------------------------------------
# _check_task_deadlines
# ---------------------------------------------------------------------------


def _deadline_stub() -> SimpleNamespace:
    client = MagicMock()
    stub = SimpleNamespace(
        _client=client,
        _config=SimpleNamespace(server_url="http://127.0.0.1:8052"),
        _notify=MagicMock(),
    )
    stub._check_task_deadlines = MethodType(
        Orchestrator._check_task_deadlines,  # type: ignore[arg-type]
        stub,
    )
    return stub


def _task_with_deadline(task_id: str, deadline: float | None) -> Task:
    t = Task(id=task_id, title=task_id, description="", role="backend")
    t.deadline = deadline
    return t


def test_check_task_deadlines_no_deadline_is_noop() -> None:
    stub = _deadline_stub()
    stub._check_task_deadlines([_task_with_deadline("T-1", None)])
    stub._client.post.assert_not_called()
    stub._notify.assert_not_called()


def test_check_task_deadlines_exceeded_fails_task_and_notifies(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000.0
    monkeypatch.setattr("bernstein.core.orchestration.orchestrator.time.time", lambda: now)
    stub = _deadline_stub()
    # Deadline 100s in the past => exceeded.
    stub._check_task_deadlines([_task_with_deadline("T-late", now - 100.0)])

    # Task is failed via the server with a deadline-aware reason.
    assert stub._client.post.call_count == 1
    url = stub._client.post.call_args.args[0]
    assert url.endswith("/tasks/T-late/fail")
    reason = stub._client.post.call_args.kwargs["json"]["reason"]
    assert "Deadline exceeded" in reason
    # And an exceeded notification fires.
    events = [c.args[0] for c in stub._notify.call_args_list]
    assert "task.deadline_exceeded" in events


def test_check_task_deadlines_warning_window_notifies_without_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.defaults import ORCHESTRATOR

    now = 1_000.0
    monkeypatch.setattr("bernstein.core.orchestration.orchestrator.time.time", lambda: now)
    stub = _deadline_stub()
    # Deadline within the warning window (a few seconds in the future).
    warn_window = ORCHESTRATOR.deadline_warning_window_s
    stub._check_task_deadlines([_task_with_deadline("T-soon", now + min(5.0, warn_window / 2))])

    # Warning path does NOT fail the task.
    stub._client.post.assert_not_called()
    events = [c.args[0] for c in stub._notify.call_args_list]
    assert "task.deadline_warning" in events


def test_check_task_deadlines_far_future_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000.0
    monkeypatch.setattr("bernstein.core.orchestration.orchestrator.time.time", lambda: now)
    stub = _deadline_stub()
    # Deadline far beyond the warning window: neither fail nor warn.
    stub._check_task_deadlines([_task_with_deadline("T-far", now + 100_000.0)])
    stub._client.post.assert_not_called()
    stub._notify.assert_not_called()


def test_check_task_deadlines_swallows_fail_post_error(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000.0
    monkeypatch.setattr("bernstein.core.orchestration.orchestrator.time.time", lambda: now)
    stub = _deadline_stub()
    stub._client.post.side_effect = RuntimeError("server down")
    # Even when the fail POST raises, the method must still emit the
    # notification and not propagate the error.
    stub._check_task_deadlines([_task_with_deadline("T-late", now - 50.0)])
    events = [c.args[0] for c in stub._notify.call_args_list]
    assert "task.deadline_exceeded" in events


# ---------------------------------------------------------------------------
# _check_for_incidents (#4310)
# ---------------------------------------------------------------------------


def _incident_check_stub(workdir: Any) -> SimpleNamespace:
    stub = SimpleNamespace(
        _slo_tracker=SLOTracker(),
        _incident_manager=IncidentManager(),
        _consecutive_failures=0,
        _workdir=workdir,
        _notify=MagicMock(),
    )
    stub._check_for_incidents = MethodType(
        Orchestrator._check_for_incidents,  # type: ignore[arg-type]
        stub,
    )
    return stub


def _board_task(task_id: str) -> Task:
    return Task(id=task_id, title=task_id, description="", role="backend")


def test_check_for_incidents_agent_death_after_done_is_not_a_failure(tmp_path: Any) -> None:
    """Reproduces #4310: complete a task, then SIGTERM its agent.

    The task board is the only input -- a task that reached `done` is
    counted as done no matter what happened to the agent process that
    was working it, so a lone completed task must never raise an
    incident.
    """
    stub = _incident_check_stub(tmp_path)
    tasks_by_status = {"open": [], "claimed": [], "done": [_board_task("T-1")], "failed": []}

    stub._check_for_incidents(tasks_by_status)

    assert stub._incident_manager.incidents == []
    stub._notify.assert_not_called()


def test_check_for_incidents_genuine_failures_still_raise_sev2_with_task_ids(tmp_path: Any) -> None:
    """Real failures must still deplete the budget and fire, now carrying
    the contributing task ids in the incident's blast radius for audit."""
    stub = _incident_check_stub(tmp_path)
    failed = [_board_task(f"T-{i}") for i in range(5)]
    tasks_by_status = {"open": [], "claimed": [], "done": [], "failed": failed}

    stub._check_for_incidents(tasks_by_status)

    assert len(stub._incident_manager.incidents) == 1
    incident = stub._incident_manager.incidents[0]
    assert incident.severity.value == "sev2"
    assert incident.blast_radius == [t.id for t in failed]
    assert stub._incident_manager.should_pause is True
    stub._notify.assert_called_once()


def test_check_for_incidents_mixed_board_uses_terminal_states_only(tmp_path: Any) -> None:
    """9 open/claimed tasks alongside 2 done + 1 failed must count as
    3 total / 1 failed, matching the task board, not the larger board size
    or any agent-process-derived number."""
    stub = _incident_check_stub(tmp_path)
    tasks_by_status = {
        "open": [_board_task(f"T-open-{i}") for i in range(6)],
        "claimed": [_board_task(f"T-claimed-{i}") for i in range(3)],
        "done": [_board_task("T-done-1"), _board_task("T-done-2")],
        "failed": [_board_task("T-failed-1")],
    }

    stub._check_for_incidents(tasks_by_status)

    # 1 failure out of 3 terminal tasks is well within the default floor
    # of 3 tolerated failures, so no incident should fire.
    assert stub._incident_manager.incidents == []
    stub._notify.assert_not_called()
