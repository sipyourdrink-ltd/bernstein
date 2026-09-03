"""Unit tests for approval gate behavior."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bernstein.core.approval import ApprovalGate, ApprovalMode


def test_auto_mode_immediately_approves(tmp_path: Path, make_task: Any) -> None:
    gate = ApprovalGate(ApprovalMode.AUTO, tmp_path)
    task = make_task(id="T-1")

    result = gate.evaluate(task, session_id="S-1")

    assert result.approved is True
    assert result.rejected is False
    assert result.pr_url == ""


def test_review_mode_writes_pending_and_rejects(tmp_path: Path, make_task: Any) -> None:
    gate = ApprovalGate(
        ApprovalMode.REVIEW,
        tmp_path,
        _poll_decision=lambda task_id, approvals_dir: "rejected",
    )
    task = make_task(id="T-2", title="Review me")

    result = gate.evaluate(task, session_id="S-2", diff="diff", test_summary="3 passed")

    pending_file = tmp_path / ".sdd" / "runtime" / "pending_approvals" / "T-2.json"
    payload = json.loads(pending_file.read_text(encoding="utf-8"))
    assert payload["task_id"] == "T-2"
    assert payload["session_id"] == "S-2"
    assert result.approved is False
    assert result.rejected is True


def test_review_mode_approves_on_non_rejected_decision(tmp_path: Path, make_task: Any) -> None:
    gate = ApprovalGate(
        ApprovalMode.REVIEW,
        tmp_path,
        _poll_decision=lambda task_id, approvals_dir: "approved",
    )
    task = make_task(id="T-timeout")
    result = gate.evaluate(task, session_id="S-timeout")

    assert result.approved is True
    assert result.rejected is False


def test_pr_mode_returns_neutral_result(tmp_path: Path, make_task: Any) -> None:
    gate = ApprovalGate(ApprovalMode.PR, tmp_path)
    task = make_task(id="T-3")

    result = gate.evaluate(task, session_id="S-3")

    assert result.approved is False
    assert result.rejected is False
    assert result.pr_url == ""


def test_create_pr_uses_injected_push_and_create(tmp_path: Path, make_task: Any) -> None:
    created: dict[str, str] = {}

    def _push(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(ok=True, stderr="")

    def _create(*args: object, **kwargs: object) -> object:
        created["body"] = str(kwargs["body"])
        return SimpleNamespace(success=True, pr_url="https://example/pr/1", error="")

    gate = ApprovalGate(
        ApprovalMode.PR,
        tmp_path,
        _push_branch_fn=_push,
        _create_pr_fn=_create,
    )
    task = make_task(id="T-4", title="Ship this", description="Implement and validate.")

    pr_url = gate.create_pr(
        task,
        worktree_path=tmp_path,
        session_id="S-4",
        _role="backend",
        model="sonnet",
        cost_usd=0.5,
        test_summary="12 passed",
    )

    assert pr_url == "https://example/pr/1"
    assert "## Summary" in created["body"]
    assert "12 passed" in created["body"]


def test_approval_gate_with_override_mode(tmp_path: Path, make_task: Any) -> None:
    gate = ApprovalGate(ApprovalMode.AUTO, tmp_path)
    task = make_task(id="T-override")

    # Should evaluate as PR mode because of override
    result = gate.evaluate(task, session_id="S-override", override_mode=ApprovalMode.PR)
    assert result.approved is False
    assert result.rejected is False
    assert result.pr_url == ""


def test_approval_gate_forwards_timeout_s_and_fails_closed_on_expiry(tmp_path: Path, make_task: Any) -> None:
    task = make_task(id="T-timeout-reject")

    # An overriding timeout is forwarded to the poller as max_wait_s; an expiry
    # (the poller reporting "timed_out") fails closed to rejected without the
    # caller having to ask for it.
    def _mock_poll(task_id: str, approvals_dir: Path, max_wait_s: float = 0) -> str:
        assert max_wait_s == pytest.approx(0.01)
        return "timed_out"

    gate_mocked = ApprovalGate(ApprovalMode.REVIEW, tmp_path, _poll_decision=_mock_poll)
    result = gate_mocked.evaluate(
        task,
        session_id="S-timeout-reject",
        timeout_s=0.01,
    )
    assert result.rejected is True
    assert result.approved is False
    assert result.resolution == "timed_out"


def test_approval_gate_fails_closed_on_timeout_with_no_timeout_configured(tmp_path: Path, make_task: Any) -> None:
    task = make_task(id="T-timeout-default")

    # No timeout_s configured -- the historically fail-open path. The gate must
    # still refuse to approve a review nobody performed.
    gate = ApprovalGate(
        ApprovalMode.REVIEW,
        tmp_path,
        _poll_decision=lambda task_id, approvals_dir: "timed_out",
    )
    result = gate.evaluate(task, session_id="S-timeout-default")

    assert result.approved is False
    assert result.rejected is True
    assert result.resolution == "timed_out"


def test_approval_gate_approve_on_timeout_opt_in_records_the_expiry(tmp_path: Path, make_task: Any) -> None:
    task = make_task(id="T-timeout-optout")

    gate = ApprovalGate(
        ApprovalMode.REVIEW,
        tmp_path,
        _poll_decision=lambda task_id, approvals_dir: "timed_out",
    )
    result = gate.evaluate(task, session_id="S-timeout-optout", approve_on_timeout=True)

    # The opt-in makes the expiry resolve to approved, but the record still
    # says the approval expired rather than that a person granted it.
    assert result.approved is True
    assert result.rejected is False
    assert result.resolution == "timed_out"
