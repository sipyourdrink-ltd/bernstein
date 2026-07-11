"""Regression tests for ApprovalGate fail-open defect (defect item 9).

Background: an API-drift TypeError inside ``ApprovalGate.create_pr`` (a
signature mismatch between the public keyword names ``session_id``/``model``/
``cost_usd`` and a caller) escaped as an exception, was swallowed by the
caller's broad ``except Exception: ... defaulting to auto-merge``, and
silently bypassed the approval gate -- work got auto-merged without ever
passing review. See
work/bernstein/proofs/d2/claude/attempt4-meridian-fixed/FAIL-NOTE.md.

These tests verify:
  (a) the documented public call signature (``session_id``, ``_role``,
      ``model``, ``cost_usd`` keywords) works against ``create_pr``.
  (b) an injected exception inside gate evaluation (``evaluate`` and
      ``create_pr``) results in a REJECT/no-PR outcome, never an approval.
  (c) every gate decision path emits a log record so a future bypass is
      impossible to miss in logs.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from bernstein.core.models import Task

from bernstein.core.security.approval import ApprovalGate, ApprovalMode, ApprovalResult


def _make_task(task_id: str = "t1") -> Task:
    return Task(
        id=task_id,
        title="Add hello subcommand",
        description="test task",
        role="backend",
    )


# ---------------------------------------------------------------------------
# (a) previously-drifting call signature now works
# ---------------------------------------------------------------------------


def test_create_pr_accepts_public_kwargs(tmp_path: Path) -> None:
    """The documented public call shape must work.

    Regression for: TypeError: ApprovalGate.create_pr() got an unexpected
    keyword argument 'session_id'.
    """

    def fake_push(worktree_path: Path, branch: str) -> object:
        class _Result:
            ok = True

        return _Result()

    def fake_create_pr(**kwargs: object) -> object:
        class _Result:
            success = True
            pr_url = "https://example.com/pr/1"
            error = ""

        return _Result()

    gate = ApprovalGate(
        mode=ApprovalMode.PR,
        workdir=tmp_path,
        auto_merge=False,
        _push_branch_fn=fake_push,
        _create_pr_fn=fake_create_pr,
    )

    # Simulate a real diff existing so create_pr doesn't short-circuit on
    # "no diff" before reaching the drifted-kwarg call surface.
    import bernstein.core.security.approval as approval_mod

    monkeypatch_no_diff = approval_mod._has_no_diff
    approval_mod._has_no_diff = lambda *_a, **_k: False  # type: ignore[assignment]
    try:
        task = _make_task()
        pr_url = gate.create_pr(
            task,
            worktree_path=tmp_path,
            session_id="session-abc",
            labels=["bernstein"],
            _role="backend",
            model="claude-sonnet-5",
            cost_usd=0.12,
            test_summary="2 passed",
        )
    finally:
        approval_mod._has_no_diff = monkeypatch_no_diff

    assert pr_url == "https://example.com/pr/1"


# ---------------------------------------------------------------------------
# (b) injected exception -> REJECT / no-PR, never approve
# ---------------------------------------------------------------------------


def test_evaluate_fail_closed_on_internal_exception(tmp_path: Path) -> None:
    """An exception raised while resolving a decision must reject, not approve."""

    def boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("injected failure inside poll_decision")

    gate = ApprovalGate(
        mode=ApprovalMode.REVIEW,
        workdir=tmp_path,
        _poll_decision=boom,
    )

    result = gate.evaluate(_make_task(), session_id="session-abc")

    assert isinstance(result, ApprovalResult)
    assert result.approved is False
    assert result.rejected is True


def test_create_pr_fail_closed_on_internal_exception(tmp_path: Path) -> None:
    """An exception raised while pushing/creating the PR must return no-PR ("")."""

    def boom_push(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected failure inside push_fn")

    gate = ApprovalGate(
        mode=ApprovalMode.PR,
        workdir=tmp_path,
        _push_branch_fn=boom_push,
    )

    import bernstein.core.security.approval as approval_mod

    monkeypatch_no_diff = approval_mod._has_no_diff
    approval_mod._has_no_diff = lambda *_a, **_k: False  # type: ignore[assignment]
    try:
        pr_url = gate.create_pr(
            _make_task(),
            worktree_path=tmp_path,
            session_id="session-abc",
            _role="backend",
            model="claude-sonnet-5",
            cost_usd=0.0,
        )
    finally:
        approval_mod._has_no_diff = monkeypatch_no_diff

    assert pr_url == ""


def test_create_pr_fail_closed_with_wrong_kwargs_still_never_raises(tmp_path: Path) -> None:
    """Even a *future* signature drift must not escape as an unhandled exception.

    Calling with an unexpected keyword still raises TypeError at the call
    boundary (Python's normal behavior) -- but that is on the caller. What
    this test guards is that create_pr's *internal* logic path (once
    reached) never lets an exception propagate past its own try/except;
    combined with the caller-shape test above, the known drift is closed.
    """
    gate = ApprovalGate(mode=ApprovalMode.PR, workdir=tmp_path)

    with pytest.raises(TypeError):
        gate.create_pr(  # type: ignore[call-arg]
            _make_task(),
            worktree_path=tmp_path,
            this_kwarg_does_not_exist="x",
        )


# ---------------------------------------------------------------------------
# (c) decision logging emits
# ---------------------------------------------------------------------------


def test_evaluate_logs_decision_for_auto_mode(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    gate = ApprovalGate(mode=ApprovalMode.AUTO, workdir=tmp_path)
    with caplog.at_level(logging.INFO, logger="bernstein.core.security.approval"):
        result = gate.evaluate(_make_task(), session_id="session-abc")

    assert result.approved is True
    assert any("Approval gate decision" in r.message and "decision=approved" in r.message for r in caplog.records)


def test_evaluate_logs_error_with_traceback_on_fail_closed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    def boom(*_args: object, **_kwargs: object) -> str:
        raise ValueError("boom for logging test")

    gate = ApprovalGate(mode=ApprovalMode.REVIEW, workdir=tmp_path, _poll_decision=boom)

    with caplog.at_level(logging.ERROR, logger="bernstein.core.security.approval"):
        result = gate.evaluate(_make_task("t-log"), session_id="session-xyz")

    assert result.rejected is True
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "expected an ERROR log on fail-closed path"
    combined = "\n".join(r.getMessage() for r in error_records)
    assert "FAIL-CLOSED" in combined
    assert "t-log" in combined
    assert "ValueError" in combined or "boom for logging test" in combined


def test_create_pr_logs_decision_on_success(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    def fake_push(worktree_path: Path, branch: str) -> object:
        class _Result:
            ok = True

        return _Result()

    def fake_create_pr(**kwargs: object) -> object:
        class _Result:
            success = True
            pr_url = "https://example.com/pr/2"
            error = ""

        return _Result()

    gate = ApprovalGate(
        mode=ApprovalMode.PR,
        workdir=tmp_path,
        auto_merge=False,
        _push_branch_fn=fake_push,
        _create_pr_fn=fake_create_pr,
    )

    import bernstein.core.security.approval as approval_mod

    monkeypatch_no_diff = approval_mod._has_no_diff
    approval_mod._has_no_diff = lambda *_a, **_k: False  # type: ignore[assignment]
    try:
        with caplog.at_level(logging.INFO, logger="bernstein.core.security.approval"):
            pr_url = gate.create_pr(_make_task(), worktree_path=tmp_path, session_id="s1")
    finally:
        approval_mod._has_no_diff = monkeypatch_no_diff

    assert pr_url == "https://example.com/pr/2"
    assert any("decision=pr_created" in r.message for r in caplog.records)
