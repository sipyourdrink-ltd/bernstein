"""Tests for approval gate refusal recording in non-interactive mode."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType


def _make_task(*, id: str = "T-001", title: str = "Add auth") -> Task:
    return Task(
        id=id,
        title=title,
        description="Implement auth.",
        role="backend",
        priority=2,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus.DONE,
        task_type=TaskType.STANDARD,
    )


def test_approval_gate_records_refusal_on_timeout(tmp_path: Path) -> None:
    """Review mode records a chain-anchored refusal when approval times out."""
    from bernstein.core.approval import ApprovalGate, ApprovalMode
    from bernstein.core.identity.grants import GrantLedger

    # Mock the poll decision to return "timed_out" immediately
    gate = ApprovalGate(
        mode=ApprovalMode.REVIEW,
        workdir=tmp_path,
        _poll_decision=lambda task_id, approvals_dir, **kwargs: "timed_out",
    )
    task = _make_task(id="T-timeout")

    # Mock the grant ledger to capture the refusal recording
    mock_ledger = MagicMock(spec=GrantLedger)
    mock_ledger.record_refusal.return_value = MagicMock()

    with patch("bernstein.core.security.approval.GrantLedger", return_value=mock_ledger):
        result = gate.evaluate(task, session_id="agent-timeout", timeout_s=0.1)

    # Should fail closed (rejected)
    assert result.approved is False
    assert result.rejected is True
    assert result.resolution == "timed_out"

    # Should have recorded a refusal
    mock_ledger.record_refusal.assert_called_once()
    call_args = mock_ledger.record_refusal.call_args[1]
    assert call_args["task_id"] == "T-timeout"
    assert call_args["secret_name"] == "approval:agent-timeout"
    assert call_args["reason"] == "approval_timeout: Approval gate timed out after 0s with no decision"


def test_approval_gate_records_refusal_on_explicit_rejection(tmp_path: Path) -> None:
    """Review mode records a chain-anchored refusal when explicitly rejected."""
    from bernstein.core.approval import ApprovalGate, ApprovalMode
    from bernstein.core.identity.grants import GrantLedger

    # Mock the poll decision to return "rejected"
    gate = ApprovalGate(
        mode=ApprovalMode.REVIEW,
        workdir=tmp_path,
        _poll_decision=lambda task_id, approvals_dir, **kwargs: "rejected",
    )
    task = _make_task(id="T-reject")

    # Mock the grant ledger to capture the refusal recording
    mock_ledger = MagicMock(spec=GrantLedger)
    mock_ledger.record_refusal.return_value = MagicMock()

    with patch("bernstein.core.security.approval.GrantLedger", return_value=mock_ledger):
        result = gate.evaluate(task, session_id="agent-reject")

    # Should be rejected
    assert result.approved is False
    assert result.rejected is True

    # Should have recorded a refusal
    mock_ledger.record_refusal.assert_called_once()
    call_args = mock_ledger.record_refusal.call_args[1]
    assert call_args["task_id"] == "T-reject"
    assert call_args["secret_name"] == "approval:agent-reject"
    assert call_args["reason"] == "explicit_rejection: Approval explicitly rejected via decision file"


def test_approval_gate_no_refusal_when_approved(tmp_path: Path) -> None:
    """Review mode does not record a refusal when approved."""
    from bernstein.core.approval import ApprovalGate, ApprovalMode
    from bernstein.core.identity.grants import GrantLedger

    # Mock the poll decision to return "approved"
    gate = ApprovalGate(
        mode=ApprovalMode.REVIEW,
        workdir=tmp_path,
        _poll_decision=lambda task_id, approvals_dir: "approved",
    )
    task = _make_task(id="T-approve")

    # Mock the grant ledger to verify no refusal is recorded
    mock_ledger = MagicMock(spec=GrantLedger)

    with patch("bernstein.core.security.approval.GrantLedger", return_value=mock_ledger):
        result = gate.evaluate(task, session_id="agent-approve")

    # Should be approved
    assert result.approved is True
    assert result.rejected is False

    # Should NOT have recorded a refusal
    mock_ledger.record_refusal.assert_not_called()


def test_approval_gate_records_refusal_on_timeout_with_approve_on_timeout(tmp_path: Path) -> None:
    """Review mode with approve_on_timeout still records a refusal even when resolving to approved."""
    from bernstein.core.approval import ApprovalGate, ApprovalMode
    from bernstein.core.identity.grants import GrantLedger

    # Mock the poll decision to return "timed_out"
    gate = ApprovalGate(
        mode=ApprovalMode.REVIEW,
        workdir=tmp_path,
        _poll_decision=lambda task_id, approvals_dir, **kwargs: "timed_out",
    )
    task = _make_task(id="T-timeout-approve")

    # Mock the grant ledger to capture the refusal recording
    mock_ledger = MagicMock(spec=GrantLedger)
    mock_ledger.record_refusal.return_value = MagicMock()

    with patch("bernstein.core.security.approval.GrantLedger", return_value=mock_ledger):
        result = gate.evaluate(
            task,
            session_id="agent-timeout-approve",
            timeout_s=0.1,
            approve_on_timeout=True,  # This should still record a refusal
        )

    # Should resolve to approved despite timeout
    assert result.approved is True
    assert result.rejected is False
    assert result.resolution == "timed_out"

    # Should still have recorded a refusal (for audit trail)
    mock_ledger.record_refusal.assert_called_once()
    call_args = mock_ledger.record_refusal.call_args[1]
    assert call_args["task_id"] == "T-timeout-approve"
    assert call_args["secret_name"] == "approval:agent-timeout-approve"
    assert call_args["reason"] == "approval_timeout: Approval gate timed out after 0s with no decision"
