"""Targeted tests for the outer fail-closed behavior of ``_evaluate_approval_gate``.

Defect: on ANY exception in the evaluate+create_pr flow -- including one raised
by a pre-gate step like ``_resolve_approval_workflow`` -- the outer except in
``task_lifecycle._evaluate_approval_gate`` used to default to a value that let
the merge proceed (fail OPEN). The fix must fail CLOSED: on any exception,
return ``True`` (skip_merge=True, i.e. hold for approval) and log at ERROR
with the full traceback.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from bernstein.core.models import AgentSession, ModelConfig, Task

from bernstein.core.tasks.task_lifecycle import _evaluate_approval_gate


def _task(task_id: str = "t-1") -> Task:
    return Task(id=task_id, title="do the thing", description="desc", role="engineer")


def _session(task_id: str = "t-1") -> AgentSession:
    return AgentSession(
        id="sess-1",
        role="engineer",
        task_ids=[task_id],
        model_config=ModelConfig(model="test-model", effort="medium"),
    )


def _orch_with_gate(gate: Any) -> Any:
    return SimpleNamespace(_approval_gate=gate, _config=SimpleNamespace(approval_workflow=None))


def test_exception_in_resolve_approval_workflow_holds_for_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-gate exception (in _resolve_approval_workflow) must HOLD, not auto-merge."""
    gate = MagicMock()
    orch = _orch_with_gate(gate)

    def _boom(_orch: Any, _task: Task) -> tuple[Any, float | None]:
        raise RuntimeError("boom from pre-gate step")

    monkeypatch.setattr(
        "bernstein.core.tasks.task_lifecycle._resolve_approval_workflow",
        _boom,
    )

    result = _evaluate_approval_gate(orch, _task(), _session(), None, janitor_passed=True)

    assert result is True, "exception in pre-gate step must fail CLOSED (hold for approval)"
    gate.evaluate.assert_not_called()


def test_exception_in_gate_evaluate_holds_for_approval() -> None:
    """An exception raised inside gate.evaluate() itself must also HOLD."""
    gate = MagicMock()
    gate.evaluate.side_effect = RuntimeError("boom from gate.evaluate")
    orch = _orch_with_gate(gate)

    result = _evaluate_approval_gate(orch, _task(), _session(), None, janitor_passed=True)

    assert result is True, "exception in gate.evaluate() must fail CLOSED (hold for approval)"


def test_happy_path_approved_is_unchanged() -> None:
    """No exception, gate approves -- merge proceeds (skip_merge=False)."""
    gate = MagicMock()
    gate.evaluate.return_value = SimpleNamespace(approved=True, rejected=False)
    orch = _orch_with_gate(gate)

    result = _evaluate_approval_gate(orch, _task(), _session(), None, janitor_passed=True)

    assert result is False


def test_happy_path_rejected_is_unchanged() -> None:
    """No exception, gate rejects -- merge is held (skip_merge=True), same as before."""
    gate = MagicMock()
    gate.evaluate.return_value = SimpleNamespace(approved=False, rejected=True)
    orch = _orch_with_gate(gate)

    result = _evaluate_approval_gate(orch, _task(), _session(), None, janitor_passed=True)

    assert result is True


def test_error_log_emitted_on_outer_exception(caplog: pytest.LogCaptureFixture) -> None:
    """The failure path must emit an ERROR-level log with the exception/traceback."""
    gate = MagicMock()
    gate.evaluate.side_effect = RuntimeError("boom for logging check")
    orch = _orch_with_gate(gate)

    with caplog.at_level(logging.ERROR, logger="bernstein.core.tasks.task_lifecycle"):
        result = _evaluate_approval_gate(orch, _task(), _session(), None, janitor_passed=True)

    assert result is True
    assert any(record.levelno >= logging.ERROR for record in caplog.records), "expected an ERROR-level log record"
    error_text = "\n".join(record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR)
    assert "FAIL-CLOSED" in error_text
    assert any(record.exc_info for record in caplog.records if record.levelno >= logging.ERROR), (
        "expected the ERROR log to carry exception info (logger.exception / traceback)"
    )
