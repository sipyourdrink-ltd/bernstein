"""Regression tests for issue #3254.

Defect: ``task_lifecycle._evaluate_approval_gate`` returned ``False``
(skip_merge=False, i.e. proceed to merge) whenever ``janitor_passed`` was
``False``. The function's contract is "return whether to skip merge", so a
failed required quality gate must return ``True`` and hold the merge instead.
The prior code only distinguished "no approval gate configured" (correctly
``False``) from "janitor failed" (incorrectly also ``False``) with a single
``or`` condition.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

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


def test_failed_janitor_skips_merge_with_no_approval_gate_configured() -> None:
    """A failed required gate must hold the merge even with no approval gate wired up."""
    orch = _orch_with_gate(None)

    result = _evaluate_approval_gate(orch, _task(), _session(), None, janitor_passed=False)

    assert result is True, "failed quality gate must skip_merge=True regardless of approval gate config"


def test_failed_janitor_skips_merge_with_approval_gate_configured() -> None:
    """A failed required gate must hold the merge, and must not even reach gate.evaluate()."""
    gate = MagicMock()
    orch = _orch_with_gate(gate)

    result = _evaluate_approval_gate(orch, _task(), _session(), None, janitor_passed=False)

    assert result is True
    gate.evaluate.assert_not_called()


def test_passed_janitor_with_no_approval_gate_does_not_skip_merge() -> None:
    """No quality-gate failure and no approval gate configured -- merge proceeds as before."""
    orch = _orch_with_gate(None)

    result = _evaluate_approval_gate(orch, _task(), _session(), None, janitor_passed=True)

    assert result is False


def test_passed_janitor_with_approval_gate_approved_does_not_skip_merge() -> None:
    """Quality gate passed and the approval gate approves -- merge proceeds (unchanged behavior)."""
    gate = MagicMock()
    gate.evaluate.return_value = SimpleNamespace(approved=True, rejected=False)
    orch = _orch_with_gate(gate)

    result = _evaluate_approval_gate(orch, _task(), _session(), None, janitor_passed=True)

    assert result is False
