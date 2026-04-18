"""Unit tests for the context degradation detector."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from bernstein.core.context_degradation_detector import (
    ContextDegradationCheckpoint,
    ContextDegradationConfig,
    ContextDegradationDetector,
)
from bernstein.core.cross_model_verifier import CrossModelVerdict, CrossModelVerifierConfig
from bernstein.core.models import AgentSession, ModelConfig
from bernstein.core.task_lifecycle import evict_degraded_sessions

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _approve(reviewer: str = "gemini-flash") -> CrossModelVerdict:
    return CrossModelVerdict(verdict="approve", feedback="LGTM", reviewer_model=reviewer)


def _reject(feedback: str = "Bug found") -> CrossModelVerdict:
    return CrossModelVerdict(
        verdict="request_changes",
        feedback=feedback,
        issues=["Issue 1"],
        reviewer_model="gemini-flash",
    )


def _session(session_id: str = "backend-abc123", task_ids: list[str] | None = None) -> AgentSession:
    return AgentSession(
        id=session_id,
        role="backend",
        task_ids=task_ids or ["T-001"],
        model_config=ModelConfig("sonnet", "high"),
        tokens_used=100,
    )


def _detector(
    *,
    threshold: int = 2,
    min_tasks: int = 1,
    max_tokens: int = 0,
    workdir: Path | None = None,
    tmp_path: Path | None = None,
) -> ContextDegradationDetector:
    config = ContextDegradationConfig(
        enabled=True,
        consecutive_reject_threshold=threshold,
        min_tasks_before_detection=min_tasks,
        max_tokens_before_restart=max_tokens,
    )
    root = workdir or tmp_path or Path("/tmp")
    return ContextDegradationDetector(config, root)


# ---------------------------------------------------------------------------
# record_verdict / disabled
# ---------------------------------------------------------------------------


class TestRecordVerdictDisabled:
    def test_disabled_ignores_verdicts(self, tmp_path: Path) -> None:
        config = ContextDegradationConfig(enabled=False)
        det = ContextDegradationDetector(config, tmp_path)
        det.record_verdict("s1", "T-001", _reject())
        det.record_verdict("s1", "T-001", _reject())
        assert not det.degraded_sessions()

    def test_should_restart_disabled_always_false(self, tmp_path: Path) -> None:
        config = ContextDegradationConfig(enabled=False)
        det = ContextDegradationDetector(config, tmp_path)
        det.record_verdict("s1", "T-001", _reject())
        assert not det.should_restart("s1")


# ---------------------------------------------------------------------------
# Consecutive reject threshold
# ---------------------------------------------------------------------------


class TestConsecutiveRejectThreshold:
    def test_single_reject_below_threshold(self, tmp_path: Path) -> None:
        det = _detector(threshold=2, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _reject())
        assert "s1" not in det.degraded_sessions()

    def test_two_rejects_triggers_degradation(self, tmp_path: Path) -> None:
        det = _detector(threshold=2, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _reject())
        det.record_verdict("s1", "T-002", _reject())
        assert "s1" in det.degraded_sessions()

    def test_approve_resets_consecutive_count(self, tmp_path: Path) -> None:
        det = _detector(threshold=2, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _reject())
        det.record_verdict("s1", "T-002", _approve())  # resets count
        det.record_verdict("s1", "T-003", _reject())
        # Only 1 consecutive reject after the approval — not degraded
        assert "s1" not in det.degraded_sessions()

    def test_threshold_of_three(self, tmp_path: Path) -> None:
        det = _detector(threshold=3, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _reject())
        det.record_verdict("s1", "T-002", _reject())
        assert "s1" not in det.degraded_sessions()
        det.record_verdict("s1", "T-003", _reject())
        assert "s1" in det.degraded_sessions()

    def test_multiple_sessions_isolated(self, tmp_path: Path) -> None:
        det = _detector(threshold=2, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _reject())
        det.record_verdict("s1", "T-002", _reject())
        det.record_verdict("s2", "T-003", _approve())
        assert "s1" in det.degraded_sessions()
        assert "s2" not in det.degraded_sessions()


# ---------------------------------------------------------------------------
# min_tasks_before_detection
# ---------------------------------------------------------------------------


class TestMinTasksGuard:
    def test_min_tasks_prevents_early_trigger(self, tmp_path: Path) -> None:
        det = _detector(threshold=1, min_tasks=2, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _reject())
        # threshold=1, consecutive=1, but min_tasks=2 says not enough history
        assert "s1" not in det.degraded_sessions()

    def test_triggers_after_min_tasks_reached(self, tmp_path: Path) -> None:
        det = _detector(threshold=1, min_tasks=2, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _approve())  # first verdict (approve)
        det.record_verdict("s1", "T-002", _reject())  # second verdict, threshold met
        assert "s1" in det.degraded_sessions()


# ---------------------------------------------------------------------------
# should_restart
# ---------------------------------------------------------------------------


class TestShouldRestart:
    def test_true_when_degraded(self, tmp_path: Path) -> None:
        det = _detector(threshold=1, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _reject())
        assert det.should_restart("s1")

    def test_false_when_not_degraded(self, tmp_path: Path) -> None:
        det = _detector(threshold=2, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _reject())
        assert not det.should_restart("s1")

    def test_token_ceiling_triggers_restart(self, tmp_path: Path) -> None:
        det = _detector(threshold=99, max_tokens=1000, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _approve())  # no reject
        assert det.should_restart("s1", tokens_used=1000)

    def test_token_below_ceiling_no_restart(self, tmp_path: Path) -> None:
        det = _detector(threshold=99, max_tokens=1000, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _approve())
        assert not det.should_restart("s1", tokens_used=999)

    def test_token_ceiling_zero_disabled(self, tmp_path: Path) -> None:
        det = _detector(threshold=99, max_tokens=0, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _approve())
        assert not det.should_restart("s1", tokens_used=999_999)

    def test_unknown_session_returns_false(self, tmp_path: Path) -> None:
        det = _detector(tmp_path=tmp_path)
        assert not det.should_restart("unknown-session")


# ---------------------------------------------------------------------------
# degraded_sessions
# ---------------------------------------------------------------------------


class TestDegradedSessions:
    def test_returns_snapshot_not_reference(self, tmp_path: Path) -> None:
        det = _detector(threshold=1, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _reject())
        snap = det.degraded_sessions()
        det.clear("s1")
        # Original snapshot should be unaffected
        assert "s1" in snap
        assert "s1" not in det.degraded_sessions()


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_removes_history_and_flag(self, tmp_path: Path) -> None:
        det = _detector(threshold=1, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _reject())
        assert det.should_restart("s1")
        det.clear("s1")
        assert not det.should_restart("s1")
        assert "s1" not in det.degraded_sessions()

    def test_clear_nonexistent_is_noop(self, tmp_path: Path) -> None:
        det = _detector(tmp_path=tmp_path)
        det.clear("ghost-session")  # should not raise


# ---------------------------------------------------------------------------
# build_recovery_context
# ---------------------------------------------------------------------------


class TestBuildRecoveryContext:
    def test_contains_session_summary(self, tmp_path: Path) -> None:
        det = _detector(threshold=2, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _approve())
        det.record_verdict("s1", "T-002", _reject())
        det.record_verdict("s1", "T-003", _reject())
        session = _session("s1", ["T-001", "T-002", "T-003"])
        context = det.build_recovery_context(session)
        assert "Context transfer" in context
        assert "T-001" in context
        assert "T-002" in context
        assert "T-003" in context

    def test_contains_guidance(self, tmp_path: Path) -> None:
        det = _detector(tmp_path=tmp_path)
        session = _session()
        context = det.build_recovery_context(session)
        assert "Guidance" in context

    def test_empty_history_still_returns_string(self, tmp_path: Path) -> None:
        det = _detector(tmp_path=tmp_path)
        session = _session()
        context = det.build_recovery_context(session)
        assert isinstance(context, str)
        assert len(context) > 0


# ---------------------------------------------------------------------------
# checkpoint
# ---------------------------------------------------------------------------


class TestCheckpoint:
    def test_checkpoint_fields(self, tmp_path: Path) -> None:
        det = _detector(threshold=2, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _reject())
        det.record_verdict("s1", "T-002", _reject())
        session = _session("s1", ["T-001", "T-002"])
        cp = det.checkpoint(session)
        assert isinstance(cp, ContextDegradationCheckpoint)
        assert cp.session_id == "s1"
        assert cp.task_ids == ["T-001", "T-002"]
        assert cp.verdict_count == 2
        assert cp.consecutive_rejects == 2
        assert cp.tokens_used == 100
        assert cp.timestamp > 0
        assert isinstance(cp.recovery_context, str)

    def test_checkpoint_persisted_to_disk(self, tmp_path: Path) -> None:
        det = _detector(threshold=1, tmp_path=tmp_path)
        det.record_verdict("s1", "T-001", _reject())
        session = _session("s1", ["T-001"])
        det.checkpoint(session)
        cp_path = tmp_path / ".sdd" / "runtime" / "context_checkpoints" / "s1.json"
        assert cp_path.exists()
        data = json.loads(cp_path.read_text())
        assert data["session_id"] == "s1"
        assert data["consecutive_rejects"] == 1

    def test_checkpoint_os_error_does_not_raise(self, tmp_path: Path) -> None:
        config = ContextDegradationConfig(
            enabled=True,
            checkpoint_dir="/nonexistent_root_dir/checkpoints",
        )
        det = ContextDegradationDetector(config, Path("/"))
        det.record_verdict("s1", "T-001", _reject())
        session = _session("s1", ["T-001"])
        # Should not raise even if directory creation fails
        cp = det.checkpoint(session)
        assert cp.session_id == "s1"


# ---------------------------------------------------------------------------
# Integration: approve then reject sequence
# ---------------------------------------------------------------------------


class TestApproveRejectSequence:
    def test_mixed_sequence_below_threshold(self, tmp_path: Path) -> None:
        det = _detector(threshold=3, tmp_path=tmp_path)
        for verdict in [_approve(), _reject(), _approve(), _reject(), _approve()]:
            det.record_verdict("s1", f"T-{id(verdict)}", verdict)
        assert "s1" not in det.degraded_sessions()

    def test_three_rejects_at_end_triggers(self, tmp_path: Path) -> None:
        det = _detector(threshold=3, tmp_path=tmp_path)
        det.record_verdict("s1", "T-1", _approve())
        det.record_verdict("s1", "T-2", _reject())
        det.record_verdict("s1", "T-3", _reject())
        det.record_verdict("s1", "T-4", _reject())
        assert "s1" in det.degraded_sessions()


# ---------------------------------------------------------------------------
# Wiring: cross-model verifier -> record_verdict
# ---------------------------------------------------------------------------


def _orch_with_detector(
    tmp_path: Path,
    session: AgentSession,
    detector: ContextDegradationDetector,
    cmv_config: CrossModelVerifierConfig,
) -> Any:
    """Build a lightweight orchestrator stub for task_lifecycle wiring tests."""
    spawner = MagicMock()
    spawner.get_worktree_path.return_value = tmp_path / "worktree"

    signal_mgr = MagicMock()

    return SimpleNamespace(
        _spawner=spawner,
        _workdir=tmp_path,
        _client=MagicMock(),
        _config=SimpleNamespace(
            server_url="http://server",
            cross_model_verify=cmv_config,
        ),
        _context_degradation=detector,
        _context_recovery={},
        _signal_mgr=signal_mgr,
        _agents={session.id: session},
    )


class TestCrossModelRecordVerdictWiring:
    def test_record_verdict_called_after_cross_model_review(self, tmp_path: Path, make_task: Any) -> None:
        """_run_cross_model_check feeds every verdict into the detector."""
        from bernstein.core.task_lifecycle import _run_cross_model_check

        task = make_task(id="T-rev", title="Review me")
        session = _session("s-active", [task.id])
        detector = _detector(threshold=1, tmp_path=tmp_path)
        cmv_config = CrossModelVerifierConfig(enabled=True, block_on_issues=False)
        orch = _orch_with_detector(tmp_path, session, detector, cmv_config)

        verdict = _reject()
        with patch(
            "bernstein.core.tasks.task_lifecycle.run_cross_model_verification_sync",
            return_value=verdict,
        ):
            _run_cross_model_check(orch, task, session, SimpleNamespace(verified=[], verification_failures=[]))

        # Single rejection at threshold=1 → detector flags the session.
        assert session.id in detector.degraded_sessions()

    def test_record_verdict_approve_does_not_flag(self, tmp_path: Path, make_task: Any) -> None:
        """Approvals are recorded but don't flag the session."""
        from bernstein.core.task_lifecycle import _run_cross_model_check

        task = make_task(id="T-ok", title="Approve me")
        session = _session("s-ok", [task.id])
        detector = _detector(threshold=1, tmp_path=tmp_path)
        cmv_config = CrossModelVerifierConfig(enabled=True, block_on_issues=False)
        orch = _orch_with_detector(tmp_path, session, detector, cmv_config)

        with patch(
            "bernstein.core.tasks.task_lifecycle.run_cross_model_verification_sync",
            return_value=_approve(),
        ):
            _run_cross_model_check(orch, task, session, SimpleNamespace(verified=[], verification_failures=[]))

        assert session.id not in detector.degraded_sessions()


# ---------------------------------------------------------------------------
# Wiring: evict_degraded_sessions helper
# ---------------------------------------------------------------------------


class TestEvictDegradedSessions:
    def test_no_detector_is_noop(self, tmp_path: Path) -> None:
        orch = SimpleNamespace(_agents={}, _context_recovery={}, _signal_mgr=MagicMock())
        assert evict_degraded_sessions(orch) == []

    def test_no_flagged_sessions_is_noop(self, tmp_path: Path) -> None:
        detector = _detector(threshold=2, tmp_path=tmp_path)
        session = _session("s-clean")
        orch = _orch_with_detector(tmp_path, session, detector, CrossModelVerifierConfig(enabled=True))
        assert evict_degraded_sessions(orch) == []
        orch._signal_mgr.write_shutdown.assert_not_called()

    def test_evicts_flagged_session_writes_shutdown(self, tmp_path: Path) -> None:
        """A degraded session gets checkpointed, SHUTDOWN-signalled, and cleared."""
        detector = _detector(threshold=1, tmp_path=tmp_path)
        session = _session("s-bad", ["T-9", "T-10"])
        orch = _orch_with_detector(tmp_path, session, detector, CrossModelVerifierConfig(enabled=True))
        detector.record_verdict(session.id, "T-9", _reject())
        assert session.id in detector.degraded_sessions()

        evicted = evict_degraded_sessions(orch)

        assert evicted == [session.id]
        # SHUTDOWN signal sent with a degradation-specific reason.
        orch._signal_mgr.write_shutdown.assert_called_once()
        call_kwargs = orch._signal_mgr.write_shutdown.call_args
        assert call_kwargs.kwargs.get("reason") == "context_degradation"
        # Recovery context stashed for every task id the session owned.
        assert "T-9" in orch._context_recovery
        assert "T-10" in orch._context_recovery
        assert "Context transfer" in orch._context_recovery["T-9"]
        # Detector state cleared so the same session isn't evicted twice.
        assert session.id not in detector.degraded_sessions()

    def test_evict_skips_dead_agent_but_clears_state(self, tmp_path: Path) -> None:
        """A session whose agent already died shouldn't get a SHUTDOWN, but state is cleared."""
        detector = _detector(threshold=1, tmp_path=tmp_path)
        session = _session("s-dead", ["T-x"])
        session.status = "dead"
        orch = _orch_with_detector(tmp_path, session, detector, CrossModelVerifierConfig(enabled=True))
        detector.record_verdict(session.id, "T-x", _reject())

        evicted = evict_degraded_sessions(orch)

        assert evicted == []
        orch._signal_mgr.write_shutdown.assert_not_called()
        # But internal state is cleared so memory doesn't leak.
        assert session.id not in detector.degraded_sessions()

    def test_evict_handles_checkpoint_failure(self, tmp_path: Path) -> None:
        """Checkpoint failure doesn't raise; state is still cleared."""
        detector = _detector(threshold=1, tmp_path=tmp_path)
        session = _session("s-boom", ["T-boom"])
        orch = _orch_with_detector(tmp_path, session, detector, CrossModelVerifierConfig(enabled=True))
        detector.record_verdict(session.id, "T-boom", _reject())

        with patch.object(detector, "checkpoint", side_effect=RuntimeError("boom")):
            evicted = evict_degraded_sessions(orch)

        assert evicted == []
        orch._signal_mgr.write_shutdown.assert_not_called()
        assert session.id not in detector.degraded_sessions()
